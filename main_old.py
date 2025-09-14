import asyncio
import math
from app.money import calc_pnl

from app import config as C, db, memory
from app.data import exchange, fetch_ohlcv, pseudo_delta
from app.taser_rules import taser_signal, prior_day_high_low
from app.money import choose_size
from app.execution import place_bracket
from app.messenger import tg_send
from app.messaging import no_trade_message, signal_message
from app.audit import approve_with_rationale

# Optional runtime subsystems (used by messages/telemetry/state panels)
# If your build doesn't include these modules yet, comment the import lines and the calls below.
from app import telemetry, logger, state  # <-- ensure these exist in your project
from app import telemetry
telemetry.init_telemetry()
_CVD = 0.0
def cvd_get():
    return _CVD

async def heartbeat():
    await tg_send("TASER bot: starting up ✅")

async def scan_once(ex):
    # Pull data
    tf5  = fetch_ohlcv(ex, "5m", 1500)
    if not tf5["close"]:
        telemetry.log("scan", "NO_DATA", "empty OHLCV response", {"tf":"5m"})
        return None
    tf15 = fetch_ohlcv(ex, "15m", 1500)
    if not tf15["close"]:
        telemetry.log("scan", "NO_DATA", "empty OHLCV response", {"tf":"15m"})
        return None
    tf1h = fetch_ohlcv(ex, "1h", 1500)
    if not tf1h["close"]:
        telemetry.log("scan", "NO_DATA", "empty OHLCV response", {"tf":"1h"})
        return None

    now_ts = tf5["timestamp"][-1]
    price  = tf5["close"][-1]

    pdh, pdl = prior_day_high_low(tf1h, now_ts)
    delta_pos = pseudo_delta(tf5, 30) > 0
    oi_up = True  # TODO: real OI delta if/when available
    from app import db as DB

    # Block scanning-to-signal if one trade is open/partial
    if C.SINGLE_POSITION_MODE and DB.has_open_trade():
        try:
            telemetry.log("scan", "SKIP", "single-position mode (trade open)", {})
        except Exception:
            pass
        return None

    # 1) Rules engine
    draft = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos)

    # ---- ensure heatmap & avoid zones...
    try:
        from app.analytics import build_liquidity_heatmap
    except Exception:
        build_liquidity_heatmap = None

    meta = draft.meta or {}
    price = float(tf5["close"][-1])

    # (… keep the rest of your scan_once logic unchanged …)

    pdh, pdl = prior_day_high_low(tf1h, now_ts)
    delta_pos = pseudo_delta(tf5, 30) > 0
    oi_up = True  # TODO: real OI delta if available
    from app import db as DB

    # --- Block new signals if one trade is open ---
    if C.SINGLE_POSITION_MODE and DB.has_open_trade():
        telemetry.log("scan", "SKIP", "single-position mode (trade open)", {})
        return None

    # --- Run rules engine ---
    draft = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos)

    # ---- ensure heatmap & avoid in meta (so audit payload isn't empty) ----
    try:
        from app.analytics import build_liquidity_heatmap
    except Exception:
        build_liquidity_heatmap = None

    meta = draft.meta or {}

    # Heatmap (fallback fill)
    if not meta.get("heatmap_levels") and build_liquidity_heatmap:
        try:
            hm = build_liquidity_heatmap(tf5, window=120)
            meta["heatmap_levels"] = (hm.get("levels") or [])[:12]
        except Exception:
            pass

    # Avoid zones (ATR bands around VWAP/AVWAP)
    try:
        vwap5   = meta.get("vwap5")
        avhi    = meta.get("avwap_hi")
        avlo    = meta.get("avwap_lo")
        atr_val = float(meta.get("atr") or 0.0)

        zones = list(meta.get("avoid_zones") or [])
        dbg   = dict(meta.get("avoid_debug") or {})

        def band(center, w):
            return (round(center - w, 6), round(center + w, 6))

        if vwap5 is not None and atr_val > 0:
            zones.append(band(vwap5, 0.25 * atr_val))
        if avhi is not None and atr_val > 0:
            zones.append(band(avhi, 0.20 * atr_val))
        if avlo is not None and atr_val > 0:
            zones.append(band(avlo, 0.20 * atr_val))

        # Merge overlapping bands
        zones = sorted(zones, key=lambda z: z[0])
        merged = []
        for z in zones:
            if not merged: merged.append(list(z)); continue
            a = merged[-1]
            if z[0] <= a[1]:
                a[1] = max(a[1], z[1])
            else:
                merged.append(list(z))
        zones = [(float(a), float(b)) for a, b in merged]

        meta["avoid_zones"]  = zones
        dbg.update({
            "atr": atr_val,
            "confluence": bool(vwap5 and (avhi or avlo)),
            "zones_count": len(zones)
        })
        meta["avoid_debug"] = dbg
    except Exception:
        pass

    draft.meta = meta

    # --- If no edge, stop ---
    if draft.side == "NONE":
        msg = no_trade_message(price, draft.reason, draft.meta)
        logger.console(msg)
        telemetry.log("scan", "NO_TRADE", draft.reason, draft.meta)
        await tg_send(msg)
        return None

    # --- RULE APPROVED ---
    telemetry.log("scan", "RULE_APPROVED", f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps})

    # --- AUDIT with rationale ---
    try:
        verdict = await approve_with_rationale(draft, tf5, tf15, tf1h)
        telemetry.log("audit", verdict.get("decision","?"), verdict.get("why",""),
                      {"verdict": verdict})
        logger.console("[AUDIT] " + str(verdict))
        state.set_k("last_audit", verdict)
    except Exception as e:
        telemetry.log("audit", "ERROR", str(e), {})
        await tg_send(f"[AUDIT] error: {e}")
        return None

    # --- If audit blocks ---
    if verdict.get("decision") != "APPROVE":
        try:
            from app.messaging import audit_block_message
            block_msg = audit_block_message(draft, verdict)
        except Exception:
            block_msg = (f"🛑 AUDIT BLOCKED — {C.PAIR}\n"
                         f"Proposed: {draft.side} @ {draft.entry:.4f} | SL {draft.sl:.4f} | TPs {draft.tps}\n"
                         f"Reason: {verdict.get('why','Not approved')}")
        telemetry.log("exec", "AUDIT_BLOCKED", verdict.get("why", ""),
                      {"side": draft.side, "entry": draft.entry})
        await tg_send(block_msg)
        return None

    # --- APPROVED signal ---
    approved_msg = signal_message(draft)
    logger.console(approved_msg)
    telemetry.log("exec", "APPROVED", f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps})
    state.set_k("last_signal", {
        "side": draft.side, "entry": draft.entry, "sl": draft.sl,
        "tps": draft.tps, "reason": draft.reason
    })
    await tg_send(approved_msg)

    # --- Balance (fallback to paper constant) ---
    try:
        from app.data import fetch_balance_quote
        balance = fetch_balance_quote(ex, C.PAIR)
    except Exception:
        balance = 1000.0

    qty = choose_size(balance, draft.entry, draft.sl)
    if qty <= 0:
        await tg_send("Cannot size position (check balance/SL).")
        return None

    # --- Save trade and tag PAPER/LIVE ---
    tid = db.new_trade(C.PAIR, draft.side, draft.entry, draft.sl, draft.tps, qty, draft.meta)
    db.tag_trade_account(tid, "PAPER" if C.DRY_RUN else "LIVE")

    # --- Place bracket orders (or record in paper) ---
    place_bracket(ex, C.PAIR, draft, qty, tid)

    return (ex, draft, tid, qty)
# main.py
def _assert_tf_shape(tf: dict, name: str):
    ok = (
        isinstance(tf, dict)
        and all(k in tf for k in ("timestamp","open","high","low","close","volume"))
        and all(isinstance(tf[k], list) for k in ("timestamp","open","high","low","close","volume"))
    )
    if not ok or len(tf["timestamp"]) == 0:
        from app import telemetry
        try:
            telemetry.log("scan", "OHLCV_EMPTY", f"{name} empty/invalid", {
                "pair": C.PAIR, "tf": name,
                "sizes": {k: (len(tf.get(k,[])) if isinstance(tf.get(k), list) else "na")
                          for k in ("timestamp","open","high","low","close","volume")}
            })
        except Exception:
            pass
        raise RuntimeError(f"No OHLCV data for {name} (check pair='{C.PAIR}', timeframe, or API host)")
def _candle_sl_hit(is_long: bool, hi: float, lo: float, sl: float) -> bool:
    # For longs, if any candle's low <= SL; for shorts, if any candle's high >= SL.
    return (lo <= sl) if is_long else (hi >= sl)

async def recover_open_trades(ex):
    """
    On startup: if there are OPEN/PARTIAL trades in DB, check last ~1 day of 1m candles.
    If SL would have been hit after the trade was created (while bot was offline),
    close the trade immediately at SL and book realized PnL, then move on.
    If SL not hit, we'll resume managing the trade live.
    """
    open_trades = db.list_open_trades()
    if not open_trades:
        telemetry.log("recover", "NO_OPEN", "no open trades to recover", {})
        return None

    # Fetch up to ~1 day of 1m candles
    tf1m = fetch_ohlcv(ex, "1m", 1440)  # safe if empty
    if not tf1m.get("timestamp"):
        telemetry.log("recover", "NO_1M", "1m empty; cannot reconcile", {})
        return None

    ts = tf1m["timestamp"]
    hi = tf1m["high"]
    lo = tf1m["low"]
    px = tf1m["close"][-1]  # last price now (fallback if needed)

    recovered_any = False
    to_resume = None

    for tr in open_trades:
        trade_id = tr["id"]
        is_long  = (str(tr["side"]).upper() == "LONG")
        entry    = float(tr["entry"])
        sl       = float(tr["sl"])
        qty      = float(tr["qty"])
        created  = int(tr["created_ts"] or 0)

        # Filter candles since trade creation
        try:
            # ts are ms; created_ts stored as ms
            idx0 = next((i for i, t in enumerate(ts) if t >= created), None)
        except Exception:
            idx0 = None
        if idx0 is None:
            # no candles after creation; cannot decide -> resume live
            telemetry.log("recover", "NO_CANDLES_AFTER_CREATE",
                          f"trade {trade_id} — resume live", {"created": created})
            to_resume = tr
            continue

        # scan for first SL-hit candle after creation
        hit_idx = None
        for i in range(idx0, len(ts)):
            if _candle_sl_hit(is_long, float(hi[i]), float(lo[i]), sl):
                hit_idx = i
                break

        if hit_idx is not None:
            # SL was hit during downtime -> close at SL
            exit_px = sl
            pnl = calc_pnl(tr["side"], entry, exit_px, qty)
            db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL_RECOVERED")
            db.append_event(trade_id, "RECOVERED_CLOSE",
                            f"SL occurred during downtime @ {exit_px:.4f} | PnL {pnl:.2f}")
            telemetry.log("recover", "CLOSED_SL", f"trade {trade_id} closed on recovery",
                          {"exit": exit_px, "pnl": pnl})
            await tg_send(f"🧹 Recovered: closed trade #{trade_id} at SL while offline.\nExit {exit_px:.4f} | PnL {pnl:.2f}")
            recovered_any = True
        else:
            # Not hit — resume managing this trade
            telemetry.log("recover", "RESUME", f"trade {trade_id} resume live management", {})
            to_resume = tr  # if multiple, we’ll manage the latest one by SINGLE_POSITION_MODE policy

    # Return the single trade to resume (if any still open)
    if recovered_any:
        # After closing any recovered trades, check again if something remains open
        still_open = db.get_open_trade()
        if still_open:
            # build a minimal draft-like object to resume management
            _id, sym, side, entry, sl, tp1, tp2, tp3, qty, status, cts = still_open
            class _Draft: pass
            d = _Draft()
            d.side = side; d.entry=float(entry); d.sl=float(sl)
            d.tps  = [x for x in [tp1,tp2,tp3] if x is not None]
            # hand off to caller to start surveil
            return {"resume": True, "trade_id": _id, "draft": d, "qty": float(qty)}
        else:
            return {"resume": False}

    if to_resume:
        # Build minimal draft for the last (or first) open trade
        class _Draft: pass
        d = _Draft()
        d.side = to_resume["side"]; d.entry = float(to_resume["entry"]); d.sl = float(to_resume["sl"])
        d.tps  = [x for x in [to_resume["tp1"], to_resume["tp2"], to_resume["tp3"]] if x is not None]
        return {"resume": True, "trade_id": to_resume["id"], "draft": d, "qty": float(to_resume["qty"])}

    return {"resume": False}

async def run_scheduler():
    db.init()
    db.init_settings()               # paper/live mode table
    db.ensure_trades_account_column()# account tagging
    memory.init_memory_tables()
    await heartbeat()

    ex = exchange()
    try:
        telemetry.log("run", "START", "scheduler started", {
            "api": getattr(ex, "urls", {}).get("api"),
            "pair": C.PAIR,
            "mode": "PAPER" if C.DRY_RUN else "LIVE"
        })
    except Exception:
        pass

    while True:
        try:
            result = await scan_once(ex)   # <-- pass in existing ex
            if result:
                ex, draft, tid, qty = result
                from app.surveillance import surveil_loop
                fetcher = lambda ex_, tf, lim=200: fetch_ohlcv(ex, tf, lim)
                await surveil_loop(ex, C.PAIR, draft, tid, qty, fetcher, cvd_get)
                continue  # don’t sleep, go right back to next scan after trade closes

            # idle scanning delay
            await asyncio.sleep(C.SCAN_INTERVAL_SECONDS)

        except Exception as e:
            try:
                telemetry.log("run", "ERROR", str(e), {})
            except Exception:
                pass
            await tg_send(f"[RUN] Error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(run_scheduler())