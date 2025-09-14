# app/scheduler.py
import asyncio, time
from typing import Dict, List, Optional

from app import config as C, db, memory
from app.data import fetch_ohlcv, pseudo_delta, exchange
from app.taser_rules import taser_signal, prior_day_high_low
from app.money import choose_size, calc_pnl
from app.execution import place_bracket
from app.messenger import tg_send
from app.messaging import no_trade_message, signal_message
from app.audit import approve_with_rationale

try:
    from app import telemetry, logger, state
except Exception:
    class _T:
        def init_telemetry(self): ...
        def log(self, *a, **k): print("[TELEMETRY]", a, k)
    class _L:
        def console(self, *a, **k): print(*a)
    class _S:
        _kv={}
        def set_k(self,k,v): self._kv[k]=v
        def get(self): return self._kv
    telemetry=_T(); logger=_L(); state=_S()

# optional heatmap
try:
    from app.analytics import build_liquidity_heatmap, build_liquidity_heatmap_multi
except Exception:
    build_liquidity_heatmap = None
    build_liquidity_heatmap_multi = None

# optional persistent heatmap store (if present)
try:
    from app.heatmap_store import init as hm_init, purge_old as hm_purge, save_multi as hm_save, confluence_gate
except Exception:
    hm_init = hm_purge = hm_save = confluence_gate = None

# local re-entry memory
_last_placed_ts: float = 0.0
_last_placed_entry: Optional[float] = None
_last_5m_bar_ts: Optional[int] = None

_CVD = 0.0
def cvd_get(): return _CVD

async def heartbeat():
    try: await tg_send("TASER bot: starting up ✅")
    except Exception: pass

def has_series(d: Optional[Dict[str, List[float]]], *keys: str) -> bool:
    if not isinstance(d, dict): return False
    try:
        first_len = None
        for k in keys:
            v = d.get(k)
            if not isinstance(v, list) or not v: return False
            if first_len is None: first_len = len(v)
            elif len(v) != first_len: return False
        return True
    except Exception:
        return False

def _candle_sl_hit(is_long: bool, hi: float, lo: float, sl: float) -> bool:
    return (lo <= sl) if is_long else (hi >= sl)

async def recover_open_trades(ex):
    try: opens = db.list_open_trades() if hasattr(db, "list_open_trades") else []
    except Exception: opens = []
    if not opens:
        telemetry.log("recover","NO_OPEN","no open trades to recover",{})
        return {"resume": False}

    tf1m = fetch_ohlcv(ex, "1m", 1440)
    if not has_series(tf1m,"timestamp","high","low"):
        telemetry.log("recover","NO_1M","cannot reconcile (empty/partial 1m)",{})
        return {"resume": False}

    ts, hi, lo = tf1m["timestamp"], tf1m["high"], tf1m["low"]
    recovered_any = False
    to_resume = None

    for tr in opens:
        trade_id = tr["id"]; side = str(tr["side"]).upper()
        is_long = (side == "LONG")
        entry = float(tr["entry"]); sl = float(tr["sl"]); qty = float(tr["qty"])
        created = int(tr.get("created_ts") or 0)
        idx0 = next((i for i,t in enumerate(ts) if t >= created), None)
        if idx0 is None:
            telemetry.log("recover","NO_POST_CREATE_CANDLES",f"trade {trade_id}: resume live",{"created": created})
            to_resume = tr; continue
        hit = next((i for i in range(idx0,len(ts)) if _candle_sl_hit(is_long,float(hi[i]),float(lo[i]),sl)), None)
        if hit is not None:
            exit_px = sl
            pnl = calc_pnl(side, entry, exit_px, qty)
            try:
                db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL_RECOVERED")
                db.append_event(trade_id, "RECOVERED_CLOSE", f"SL during downtime @ {exit_px:.4f} | PnL {pnl:.2f}")
            except Exception: pass
            telemetry.log("recover","CLOSED_SL",f"trade {trade_id} closed on recovery",{"exit": exit_px,"pnl": pnl})
            try: await tg_send(f"🧹 Recovered: closed trade #{trade_id} at SL while offline.\nExit {exit_px:.4f} | PnL {pnl:.2f}")
            except Exception: pass
            recovered_any = True
        else:
            to_resume = tr

    if recovered_any:
        still = None
        try: still = db.get_open_trade()
        except Exception: still = None
        if still:
            _id, sym, side, entry, sl, tp1, tp2, tp3, qty, status, cts = still
            class _Draft: pass
            d = _Draft(); d.side = side; d.entry=float(entry); d.sl=float(sl)
            d.tps=[x for x in [tp1,tp2,tp3] if x is not None]
            return {"resume": True, "trade_id": _id, "draft": d, "qty": float(qty)}
        return {"resume": False}

    if to_resume:
        class _Draft: pass
        d=_Draft(); d.side=to_resume["side"]; d.entry=float(to_resume["entry"]); d.sl=float(to_resume["sl"])
        d.tps=[x for x in [to_resume.get("tp1"),to_resume.get("tp2"),to_resume.get("tp3")] if x is not None]
        return {"resume": True, "trade_id": to_resume["id"], "draft": d, "qty": float(to_resume["qty"])}
    return {"resume": False}

# ---- local guards ----
def _too_soon_after_trade() -> bool:
    if _last_placed_ts <= 0: return False
    return (time.time() - _last_placed_ts) < max(1, C.MIN_REENTRY_SECONDS)

def _reentry_same_zone(cur_px: float) -> bool:
    global _last_placed_entry
    if _last_placed_entry is None: return False
    pct = abs(cur_px - _last_placed_entry) / max(cur_px,1e-9)
    return pct < max(1e-6, C.BLOCK_REENTRY_PCT)

async def scan_once(ex):
    global _last_5m_bar_ts
    tf5  = fetch_ohlcv(ex, "5m", 1500)
    if not has_series(tf5,"timestamp","close"):
        telemetry.log("scan","NO_DATA","empty OHLCV response",{"tf":"5m"}); return None
    tf15 = fetch_ohlcv(ex, "15m", 1500)
    if not has_series(tf15,"timestamp","close"):
        telemetry.log("scan","NO_DATA","empty OHLCV response",{"tf":"15m"}); return None
    tf1h = fetch_ohlcv(ex, "1h", 1500)
    if not has_series(tf1h,"timestamp","close"):
        telemetry.log("scan","NO_DATA","empty OHLCV response",{"tf":"1h"}); return None

    now_ts = int(tf5["timestamp"][-1])
    price  = float(tf5["close"][-1])

    # New-bar gating for spam prevention
    if C.REQUIRE_NEW_BAR:
        if _last_5m_bar_ts == now_ts:
            telemetry.log("scan","SKIP_SAME_BAR","require new 5m bar",{"bar": now_ts})
            return None
        # mark we are working this bar so we audit at most once per 5m
        _last_5m_bar_ts = now_ts

    # Single-position policy
    if getattr(C,"SINGLE_POSITION_MODE",True) and db.has_open_trade():
        telemetry.log("scan","SKIP","single-position mode (trade open)",{}); return None

    # Cooldown and re-entry zone guard
    if _too_soon_after_trade():
        telemetry.log("scan","SKIP_COOLDOWN",f"cooldown {C.MIN_REENTRY_SECONDS}s",{}); return None

    if _reentry_same_zone(price):
        telemetry.log("scan","SKIP_SAME_ZONE",f"price within {C.BLOCK_REENTRY_PCT*100:.2f}% of last entry",{"price": price}); 
        return None

    pdh, pdl = prior_day_high_low(tf1h, now_ts)
    delta_pos = pseudo_delta(tf5, 30) > 0
    oi_up = True

    draft = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos)

    # optional heatmap enrich + learning
    meta = dict(getattr(draft,"meta",{}) or {})
    try:
        tf1d  = fetch_ohlcv(ex, "1d", 200)
    except Exception:
        tf1d = {"close":[],"high":[],"low":[],"volume":[],"timestamp":[]}
    try:
        tf30d = (
            {"timestamp": tf1h.get("timestamp", [])[-720:], "close": tf1h.get("close", [])[-720:],
             "high": tf1h.get("high", [])[-720:], "low": tf1h.get("low", [])[-720:], "volume": tf1h.get("volume", [])[-720:]}
            if tf1h.get("close") else None
        )
    except Exception:
        tf30d = None

    if build_liquidity_heatmap_multi:
        try:
            hm = build_liquidity_heatmap_multi(tf5, tf15, tf1h, tf1d, tf30d)
            meta["heatmap_levels_5m"]  = (hm.get("5m",  {}).get("levels") or [])[:24]
            meta["heatmap_levels_15m"] = (hm.get("15m", {}).get("levels") or [])[:24]
            meta["heatmap_levels_1h"]  = (hm.get("1h",  {}).get("levels") or [])[:24]
            meta["heatmap_levels_1d"]  = (hm.get("1d",  {}).get("levels") or [])[:24]
            if "30d" in hm: meta["heatmap_levels_30d"] = (hm.get("30d", {}).get("levels") or [])[:24]
            # optional persist/gate
            if hm_save: 
                try: hm_save(now_ts, hm)
                except Exception: pass
            if draft.side != "NONE" and confluence_gate:
                gate = confluence_gate(hm, price, draft.side, tol_pct=0.0015, need_tfs=2, top_n=12)
                if gate.get("block"):
                    telemetry.log("scan","FILTER_HEATMAP_BLOCK",gate.get("why",""),gate)
                    msg = no_trade_message(price,f"Heatmap block: {gate.get('why','')}",meta)
                    try: await tg_send(msg)
                    except Exception: pass
                    return None
        except Exception as e:
            telemetry.log("scan","HEATMAP_ERR",str(e),{})

    # basic single-TF heatmap (back-compat)
    if build_liquidity_heatmap and not meta.get("heatmap_levels"):
        try:
            hm_simple = build_liquidity_heatmap(tf5, window=120) or {}
            meta["heatmap_levels"] = (hm_simple.get("levels") or [])[:12]
        except Exception:
            pass

    draft.meta = meta

    if draft.side == "NONE":
        msg = no_trade_message(price, draft.reason, draft.meta)
        try: logger.console(msg)
        except Exception: pass
        telemetry.log("scan","NO_TRADE",draft.reason,draft.meta)
        try: await tg_send(msg)
        except Exception: pass
        return None

    telemetry.log("scan","RULE_APPROVED",f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps})

    # AUDIT (will be cached/debounced inside approve_with_rationale)
    try:
        verdict = await approve_with_rationale(draft, tf5, tf15, tf1h)
        telemetry.log("audit", verdict.get("decision","?"), verdict.get("why",""), {"verdict": verdict})
        try: logger.console("[AUDIT] " + str(verdict))
        except Exception: pass
        state.set_k("last_audit", verdict)
    except Exception as e:
        telemetry.log("audit","ERROR",str(e),{})
        try: await tg_send(f"[AUDIT] error: {e}")
        except Exception: pass
        return None

    if verdict.get("decision") != "APPROVE":
        try:
            from app.messaging import audit_block_message
            block_msg = audit_block_message(draft, verdict)
        except Exception:
            block_msg = (f"🛑 AUDIT BLOCKED — {C.PAIR}\n"
                         f"Proposed: {draft.side} @ {draft.entry:.4f} | SL {draft.sl:.4f} | TPs {draft.tps}\n"
                         f"Reason: {verdict.get('why','Not approved')}")
        telemetry.log("exec","AUDIT_BLOCKED",verdict.get("why",""),
                      {"side": draft.side, "entry": draft.entry})
        try: await tg_send(block_msg)
        except Exception: pass
        return None

    approved_msg = signal_message(draft)
    try: logger.console(approved_msg)
    except Exception: pass
    telemetry.log("exec","APPROVED",f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps})
    state.set_k("last_signal", {
        "side": draft.side, "entry": draft.entry, "sl": draft.sl,
        "tps": draft.tps, "reason": draft.reason
    })
    try: await tg_send(approved_msg)
    except Exception: pass

    # balance → qty
    try:
        from app.data import fetch_balance_quote
        balance = fetch_balance_quote(ex, C.PAIR)
    except Exception:
        balance = 1000.0

    qty = choose_size(balance, draft.entry, draft.sl)
    if qty <= 0:
        try: await tg_send("Cannot size position (check balance/SL).")
        except Exception: pass
        return None

    # save trade + tag
    tid = db.new_trade(C.PAIR, draft.side, draft.entry, draft.sl, draft.tps, qty, draft.meta)
    if hasattr(db, "tag_trade_account"):
        db.tag_trade_account(tid, "PAPER" if C.DRY_RUN else "LIVE")
    try: db.append_event(tid, "AUDIT", f"{verdict.get('decision')} — {verdict.get('why','')}")
    except Exception: pass

    # place
    place_bracket(ex, C.PAIR, draft, qty, tid)

    # record re-entry anchors
    global _last_placed_ts, _last_placed_entry
    _last_placed_ts = time.time()
    _last_placed_entry = float(draft.entry)

    return (ex, draft, tid, qty)

def make_fetcher():
    return lambda ex_, tf, lim=200: fetch_ohlcv(ex_, tf, lim)

async def run_scheduler():
    try: telemetry.init_telemetry()
    except Exception: pass

    db.init()
    if hasattr(db,"init_settings"): db.init_settings()
    if hasattr(db,"ensure_trades_account_column"): db.ensure_trades_account_column()
    memory.init_memory_tables()
    await heartbeat()
    if hm_init: 
        try: hm_init(); hm_purge()
        except Exception: pass

    ex = exchange()
    try:
        telemetry.log("run","START","scheduler started",{
            "api": getattr(ex,"urls",{}).get("api"),
            "pair": getattr(C,"PAIR",""),
            "mode": "PAPER" if getattr(C,"DRY_RUN",False) else "LIVE"
        })
    except Exception: pass

    # recovery
    try: rec = await recover_open_trades(ex)
    except Exception as e:
        telemetry.log("recover","ERROR",str(e),{}); rec = {"resume": False}

    if rec and rec.get("resume"):
        from app.surveillance import surveil_loop
        fetcher = make_fetcher()
        tid = rec["trade_id"]; d = rec["draft"]; qty = rec["qty"]
        try: await tg_send(f"Resuming management of open trade #{tid} — {d.side} @ {d.entry:.4f}")
        except Exception: pass
        await surveil_loop(ex, C.PAIR, d, tid, qty, fetcher, cvd_get)

    scan_delay = float(getattr(C, "SCAN_INTERVAL_SECONDS", 2.0))

    while True:
        try:
            result = await scan_once(ex)
            if result:
                _ex, draft, tid, qty = result
                from app.surveillance import surveil_loop
                fetcher = make_fetcher()
                await surveil_loop(_ex, C.PAIR, draft, tid, qty, fetcher, cvd_get)
                continue
            await asyncio.sleep(scan_delay)
        except asyncio.CancelledError:
            try: telemetry.log("run","STOP","scheduler cancelled",{})
            except Exception: pass
            raise
        except Exception as e:
            telemetry.log("run","ERROR",str(e),{})
            try: await tg_send(f"[RUN] Error: {e}")
            except Exception: pass
            await asyncio.sleep(5)