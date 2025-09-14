# app/scheduler.py
# Directional-first, scalp-fallback; aggression-aware heatmap gating; audit off by default (commented path kept).
import asyncio, time, traceback, math
from typing import Dict, List, Optional, Any

from app import config as C, db, memory
from app.data import fetch_ohlcv, pseudo_delta, exchange
from app.taser_rules import taser_signal, prior_day_high_low

# TrendScalp engine (Lorentzian + Trendlines) — optional
try:
    from app.trendscalp import scalp_signal as ts_scalp_signal
    _TS_SCALP_IMPORT_OK = True
except Exception as _e:
    ts_scalp_signal = None
    _TS_SCALP_IMPORT_OK = False
# TrendFollow engine (no-gate trendline follower) — optional
try:
    from app.trendfollow import follow_signal as tf_follow_signal
    _TF_FOLLOW_IMPORT_OK = True
except Exception as _e:
    tf_follow_signal = None
    _TF_FOLLOW_IMPORT_OK = False
from app.money import choose_size, calc_pnl_net as calc_pnl
from app.execution import place_bracket
from app.messenger import tg_send
# from app.messaging import no_trade_message, signal_message
from app.messaging import no_trade_message, signal_message
from app.runners import trendscalp_runner as ts_runner
# from app.audit import approve_with_rationale  # ← kept for quick re-enable

# Heatmap infra
try:
    from app.analytics import build_liquidity_heatmap, build_liquidity_heatmap_multi
except Exception:
    build_liquidity_heatmap = None
    build_liquidity_heatmap_multi = None

try:
    from app.heatmap_store import init as hm_init, purge_old as hm_purge, save_multi as hm_save, confluence_gate
except Exception:
    def hm_init(): ...
    def hm_purge(): ...
    def hm_save(ts, hm): ...
    def confluence_gate(*a, **k): return {"block": False}

# Runtime subsystems (shims if missing)
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

# Log TrendScalp availability at import time (helps diagnose "no trade via trendscalp")
try:
    logger.console(f"[INIT] TrendScalp import: {'OK' if globals().get('_TS_SCALP_IMPORT_OK') else 'FAIL'}")
except Exception:
    pass
# Log TrendFollow availability
try:
    logger.console(f"[INIT] TrendFollow import: {'OK' if globals().get('_TF_FOLLOW_IMPORT_OK') else 'FAIL'}")
except Exception:
    pass
# ------------------------------------------
# Generic engine cooldown: if last two closed trades for engine ended at SL, pause for N minutes (per-engine knob)
# ------------------------------------------

def _engine_on_cooldown(engine: str) -> Optional[str]:
    try:
        # Per-engine knobs with sensible defaults
        key = {
            "trendfollow": "TF_COOLDOWN_AFTER_2_SL_MIN",
            "scalp": "TS_COOLDOWN_AFTER_2_SL_MIN",
            "taser": "TR_COOLDOWN_AFTER_2_SL_MIN",
        }.get(engine, "ENGINE_COOLDOWN_AFTER_2_SL_MIN")
        cool_min = int(getattr(C, key, 15))
        if cool_min <= 0:
            return None
        rows = db.query(
            "SELECT engine, status, closed_ts FROM trades WHERE engine = ? AND closed_ts IS NOT NULL ORDER BY id DESC LIMIT 2",
            (engine,)
        )
        if not rows or len(rows) < 2:
            return None
        last_two_sl = 0
        last_closed_ts = 0
        for eng, status, cts in rows:
            status_u = (status or "").upper()
            if "SL" in status_u:
                last_two_sl += 1
            last_closed_ts = max(last_closed_ts, int(cts or 0))
        if last_two_sl >= 2:
            now_ms = int(time.time() * 1000)
            if now_ms - last_closed_ts < cool_min * 60 * 1000:
                remain_s = max(0, cool_min*60 - (now_ms - last_closed_ts)//1000)
                return f"{engine} cooldown {remain_s}s remaining after 2 SLs"
    except Exception:
        return None
    return None
# ------------------------------------------
# TrendScalp attempt helper (consistent fallback + logs)
# ------------------------------------------
def _try_trendfollow(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi=None):
    if not bool(getattr(C, "TRENDFOLLOW_ENABLED", False)):
        telemetry.log("trendfollow", "DISABLED", "TRENDFOLLOW_ENABLED=false", {})
        return None
    cd = _engine_on_cooldown("trendfollow")
    if cd:
        telemetry.log("trendfollow", "COOLDOWN", cd, {})
        return None
    # prefer dedicated module if present
    if tf_follow_signal is not None:
        try:
            tf = tf_follow_signal(price, tf5, tf15, tf1h, pdh, pdl, tf1m)
        except Exception as e:
            telemetry.log("trendfollow", "ERROR", str(e), {})
            tf = None
        if tf and getattr(tf, "side", "NONE") != "NONE":
            try:
                tf.meta = (getattr(tf, "meta", {}) or {})
                tf.meta["engine"] = "trendfollow"
                tf.reason = f"TRENDFOLLOW: {tf.reason}"
            except Exception:
                pass
            telemetry.log("trendfollow", "OK", tf.reason, {"side": getattr(tf, "side", "?"), "sl": getattr(tf, "sl", None), "tps": getattr(tf, "tps", None)})
            return tf
    # fallback: no module available → skip (we do not implement TL math here to avoid duplication)
    telemetry.log("trendfollow", "IMPORT_FAIL", "app.trendfollow not available", {})
    return None

# ------------------------------------------
# Small helpers (local-only, no dependencies)
# ------------------------------------------
def has_series(d: Optional[Dict[str, List[float]]], *keys: str) -> bool:
    if not isinstance(d, dict): return False
    try:
        n0 = None
        for k in keys:
            v = d.get(k)
            if not isinstance(v, list) or not v: return False
            if n0 is None: n0 = len(v)
            elif len(v) != n0: return False
        return True
    except Exception:
        return False

def _candle_sl_hit(is_long: bool, hi: float, lo: float, sl: float) -> bool:
    return (lo <= sl) if is_long else (hi >= sl)

def _atr(highs: List[float], lows: List[float], n:int=14) -> float:
    n = min(n, len(highs), len(lows))
    if n < 1: return 0.0
    tr = [float(highs[-i]) - float(lows[-i]) for i in range(1, n+1)]
    return (sum(tr) / len(tr)) if tr else 0.0

def _pct(a: float, b: float) -> float:
    return (abs(a-b) / max(1e-9, abs(b)))

def _now_s() -> int:
    return int(time.time())

# ------------------------------------------
# Support-aware taser_signal call (tf1m optional)
# ------------------------------------------
def _supports_tf1m() -> bool:
    try:
        import inspect
        return "tf1m" in inspect.signature(taser_signal).parameters
    except Exception:
        return False

def _call_taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m):
    # Call taser_signal with or without tf1m depending on signature, then stamp engine.
    try:
        if _supports_tf1m():
            try:
                d = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m=tf1m)
            except TypeError:
                d = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos)
        else:
            d = taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos)
    except Exception:
        # If rules engine fails, create a neutral draft-like shell to avoid crashes downstream
        class _D: pass
        d = _D(); d.side = "NONE"; d.entry = 0.0; d.sl = 0.0; d.tps = []; d.reason = "ERROR"
        setattr(d, "meta", {})

    # Ensure meta exists and stamp the engine as 'taser'
    try:
        meta = dict(getattr(d, "meta", {}) or {})
        meta["engine"] = "taser"
        d.meta = meta
    except Exception:
        try:
            setattr(d, "meta", {"engine": "taser"})
        except Exception:
            pass
    return d

# ------------------------------------------
# TP order sanitizer (strictly monotonic and deduped)
# ------------------------------------------
def _sanitize_tp_order(draft):
    """
    Keep only TPs on the correct side of entry, enforce a minimal step (≥ fees),
    then strictly order & dedupe to at most 3.
    """
    try:
        side = str(getattr(draft, "side", "NONE")).upper()
        entry = float(getattr(draft, "entry"))
        tps = [float(x) for x in (getattr(draft, "tps", []) or []) if x is not None]
        if not tps or side not in ("LONG","SHORT"):
            return

        # Minimal sensible distance from entry (use fees cushion)
        fee_pct      = float(getattr(C, "FEE_PCT", 0.0005))
        fee_pad_mult = float(getattr(C, "FEE_PAD_MULT", 2.0))
        min_step_abs = max(entry * fee_pct * fee_pad_mult, 1e-6)

        # Keep only in-profit-direction TPs with ≥ min step from entry
        if side == "LONG":
            tps = [x for x in tps if x > entry + min_step_abs]
            asc = True
        else:
            tps = [x for x in tps if x < entry - min_step_abs]
            asc = False

        if not tps:
            draft.tps = []
            return

        # Round, dedupe, strict monotonic
        vals = sorted({round(x, 4) for x in tps}, reverse=not asc)
        out = []
        for x in vals:
            if not out:
                out.append(x); continue
            if (asc and x > out[-1]) or ((not asc) and x < out[-1]):
                out.append(x)

        draft.tps = out[:3]
    except Exception:
        # Fail-safe: leave draft.tps as-is
        pass

# ------------------------------------------
# Last-mile SL rail enforcement (guards too-tight SL)
# ------------------------------------------
def _enforce_min_sl(draft, price: float):
    """
    Guard against too-tight SL by enforcing the absolute rail from the *entry*,
    not the current scan price.
    """
    try:
        entry = float(getattr(draft, "entry"))
        sl    = float(getattr(draft, "sl"))
        dist  = abs(entry - sl)

        # Use entry-based rail (more consistent with sizing/R math)
        rail_pct = float(getattr(C, "MIN_SL_PCT", 0.0045))
        rail     = max(entry * rail_pct, 1e-6)

        if dist >= rail:
            return

        is_long = (str(getattr(draft, "side", "NONE")).upper() == "LONG")
        if is_long:
            sl_new = entry - rail
            if sl_new >= entry:
                sl_new = entry - rail
        else:
            sl_new = entry + rail
            if sl_new <= entry:
                sl_new = entry + rail

        old = sl
        draft.sl = round(float(sl_new), 4)
        try:
            telemetry.log(
                "scan", "SL_PADDED",
                f"SL too tight; padded to rail {rail:.6f} from entry",
                {"old": old, "new": draft.sl, "entry": entry}
            )
        except Exception:
            pass
    except Exception:
        pass

# ------------------------------------------
# Re-entry hygiene
# ------------------------------------------
def _recent_closed_trade():
    try:
        row = db.query(
            "SELECT id, side, entry, exit_price, created_ts, closed_ts, status "
            "FROM trades WHERE closed_ts IS NOT NULL ORDER BY id DESC LIMIT 1"
        )
        if row:
            tid, side, entry, exitp, cts, ets, status = row[0]
            return {
                "id": tid, "side": str(side).upper() if side else None,
                "entry": float(entry) if entry is not None else None,
                "exit": float(exitp) if exitp is not None else None,
                "created": int(cts) if cts else None,
                "closed": int(ets) if ets else None,
                "status": status
            }
    except Exception:
        pass
    return None

def _gate_reentry(now_ts: int, price: float, side: str) -> Optional[str]:
    """
    Cool-off + bar hygiene + (optionally) price-distance guard.
    If `side` is not LONG/SHORT, only bar/time checks are applied.
    Returns reason string if blocked; None if OK to proceed.
    """
    st = state.get()
    # 1) Require new 5m bar
    if getattr(C, "REQUIRE_NEW_BAR", True):
        last_bar = st.get("last_traded_bar_ts")
        if last_bar is not None and int(last_bar) == int(now_ts):
            return "same 5m bar (REQUIRE_NEW_BAR)"
    # 2) Cool-off by time
    last = _recent_closed_trade()
    if last and last.get("closed"):
        ago = max(0, _now_s() - int(last["closed"]//1000))
        if ago < getattr(C, "MIN_REENTRY_SECONDS", 60):
            # 3) Block re-entry at nearly same idea/price & side — only if side is explicit
            side_u = str(side).upper() if side is not None else "NONE"
            if side_u in ("LONG","SHORT") and last.get("side") == side_u and last.get("entry") is not None:
                if _pct(price, float(last["entry"])) < getattr(C, "BLOCK_REENTRY_PCT", 0.003):
                    return "price too close to last entry (BLOCK_REENTRY_PCT)"
            return f"cool-off {ago}s < MIN_REENTRY_SECONDS"
    return None

# ------------------------------------------
# Heatmap gating parameters (by aggression)
# ------------------------------------------
def _hm_gate_params():
    aggr = getattr(C, "AGGRESSION", "balanced").lower()
    if aggr == "aggressive":
        return {"tol_pct": 0.0010, "need_tfs": 3, "top_n": 12}
    if aggr == "conservative":
        return {"tol_pct": 0.0025, "need_tfs": 2, "top_n": 16}
    return {"tol_pct": 0.0015, "need_tfs": 2, "top_n": 12}  # balanced

# ------------------------------------------
# Engine order (respect env/config)
# ------------------------------------------

def _engine_order() -> List[str]:
    """
    Read engine order from config (ENV: ENGINE_ORDER), normalize common aliases.
    Defaults to TrendScalp-only if not provided.
    """
    try:
        order = [s.strip().lower() for s in getattr(C, "ENGINE_ORDER", ["trendscalp"]) or []]
        if isinstance(order, str):
            order = [s.strip().lower() for s in order.split(",") if s.strip()]
    except Exception:
        order = ["trendscalp"]
    # normalize aliases
    out = []
    for e in order:
        if e in ("trendscalp","scalp","ts","trend_scalp"):
            out.append("trendscalp")
        elif e in ("taser","rules","directional"):
            out.append("taser")
        elif e in ("trendfollow","follow"):
            out.append("trendfollow")
        else:
            out.append(e)
    # de-dup while preserving order
    seen=set(); dedup=[]
    for e in out:
        if e not in seen:
            dedup.append(e); seen.add(e)
    return dedup or ["trendscalp"]

# ------------------------------------------
# Default engine helper for recovery/resume
# ------------------------------------------
def _default_engine() -> str:
    try:
        return _engine_order()[0]
    except Exception:
        return "trendscalp"


# ------------------------------------------
# Minimal Draft shell (used by recovery/resume only)
# ------------------------------------------
class _Draft:
    def __init__(self, side, entry, sl, tps, reason, meta):
        self.side = str(side).upper() if side else "NONE"
        self.entry = float(entry) if entry is not None else 0.0
        self.sl = float(sl) if sl is not None else 0.0
        self.tps = [float(x) for x in (tps or [])]
        self.reason = reason or ""
        self.meta = dict(meta or {})


async def recover_open_trades(ex):
    try:
        opens = db.list_open_trades() if hasattr(db, "list_open_trades") else []
    except Exception:
        opens = []
    if not opens:
        telemetry.log("recover", "NO_OPEN", "no open trades to recover", {})
        return {"resume": False}

    tf1m = fetch_ohlcv(ex, "1m", 1440)
    if not has_series(tf1m, "timestamp", "high", "low"):
        telemetry.log("recover", "NO_1M", "cannot reconcile (empty/partial 1m)", {})
        return {"resume": False}

    ts = tf1m["timestamp"]; hi = tf1m["high"]; lo = tf1m["low"]
    recovered_any = False
    to_resume = None

    for tr in opens:
        trade_id = tr["id"]; side = str(tr["side"]).upper()
        is_long = (side == "LONG")
        entry = float(tr["entry"]); sl = float(tr["sl"]); qty = float(tr["qty"])
        created = int(tr.get("created_ts") or 0)

        idx0 = next((i for i,t in enumerate(ts) if t >= created), None)
        if idx0 is None:
            telemetry.log("recover", "NO_POST_CREATE_CANDLES",
                          f"trade {trade_id}: resume live", {"created": created})
            to_resume = tr
            continue

        hit = next((i for i in range(idx0, len(ts))
                    if _candle_sl_hit(is_long, float(hi[i]), float(lo[i]), sl)), None)

        if hit is not None:
            exit_px = sl
            pnl = calc_pnl(side, entry, exit_px, qty)
            try:
                db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL_RECOVERED")
                db.append_event(trade_id, "RECOVERED_CLOSE",
                                f"SL during downtime @ {exit_px:.4f} | PnL {pnl:.2f}")
            except Exception:
                pass
            telemetry.log("recover", "CLOSED_SL", f"trade {trade_id} closed on recovery",
                          {"exit": exit_px, "pnl": pnl})
            try:
                await tg_send(
                    f"🧹 Recovered: closed trade #{trade_id} at SL while offline.\n"
                    f"Exit {exit_px:.4f} | PnL {pnl:.2f}"
                )
            except Exception:
                pass
            recovered_any = True
        else:
            to_resume = tr

    if recovered_any:
        still = None
        try:
            still = db.get_open_trade()
        except Exception:
            still = None
        if still:
            _id, sym, side, entry, sl, tp1, tp2, tp3, qty, status, cts = still
            d = _Draft(side, float(entry), float(sl),
                       [x for x in [tp1, tp2, tp3] if x is not None],
                       "RECOVERED", {"engine": db.get_trade_engine(_id)})
            return {"resume": True, "trade_id": _id, "draft": d, "qty": float(qty)}
        return {"resume": False}

    if to_resume:
        d = _Draft(to_resume["side"], float(to_resume["entry"]), float(to_resume["sl"]),
                   [x for x in [to_resume.get("tp1"), to_resume.get("tp2"), to_resume.get("tp3")] if x is not None],
                   "RESUME", {"engine": db.get_trade_engine(to_resume["id"])})
        return {"resume": True, "trade_id": to_resume["id"], "draft": d, "qty": float(to_resume["qty"])}

    return {"resume": False}

# ------------------------------------------
# TrendScalp attempt helper (consistent fallback + logs)
# ------------------------------------------
def _try_trendscalp(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi=None):
    enabled = bool(getattr(C, "TRENDSCALP_ENABLED", True))
    if not enabled:
        telemetry.log("scalp", "DISABLED", "TRENDSCALP_ENABLED=false", {})
        return None
    cd = _engine_on_cooldown("trendscalp")
    if cd:
        telemetry.log("scalp", "COOLDOWN", cd, {})
        return None
    if ts_scalp_signal is None:
        telemetry.log("scalp", "IMPORT_FAIL", "app.trendscalp not available", {})
        return None
    try:
        sc = ts_scalp_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
    except Exception as e:
        telemetry.log("scalp", "ERROR", str(e), {})
        return None
    if getattr(sc, "side", "NONE") == "NONE":
        # give a compact reason if present and propagate meta for diagnostics
        r = getattr(sc, "reason", "NONE") if sc is not None else "NONE"
        try:
            sc.meta = (getattr(sc, "meta", {}) or {})
            sc.meta["engine"] = "trendscalp"
        except Exception:
            pass
        telemetry.log("scalp", "NONE", r, {})
        return sc  # return the neutral object so scheduler can capture meta
    # tag engine and reason prefix
    try:
        sc.meta = (getattr(sc, "meta", {}) or {})
        sc.meta["engine"] = "trendscalp"
        sc.reason = f"SCALP: {sc.reason}"
    except Exception:
        pass
    telemetry.log("scalp", "OK", sc.reason, {"side": getattr(sc, "side", "?"), "sl": getattr(sc, "sl", None), "tps": getattr(sc, "tps", None)})
    return sc

# ------------------------------------------
# Engine-split CSV export (for dashboard) — 24h and 7d
# ------------------------------------------
import os

def _export_engine_split_csv(window_seconds: int = 24*60*60, path: Optional[str] = None):
    """
    Export engine PnL split for the last `window_seconds` to CSV.
    Default: 24h window. When `path` is None, writes to runtime/engine_summary_{24h|7d}.csv
    """
    try:
        end_ms = int(time.time() * 1000)
        start_ms = end_ms - int(window_seconds) * 1000
        rows = db.query(
            "SELECT engine, realized_pnl FROM trades WHERE created_ts BETWEEN ? AND ? AND realized_pnl IS NOT NULL",
            (start_ms, end_ms)
        ) or []
        agg: Dict[str, Dict[str, float]] = {}
        for eng, pnl in rows:
            e = (eng or "").lower() or "unknown"
            pnl_f = float(pnl or 0.0)
            if e not in agg:
                agg[e] = {"pnl": 0.0, "trades": 0.0, "wins": 0.0, "losses": 0.0, "breakeven": 0.0}
            agg[e]["pnl"] += pnl_f
            agg[e]["trades"] += 1
            if pnl_f > 0: agg[e]["wins"] += 1
            elif pnl_f < 0: agg[e]["losses"] += 1
            else: agg[e]["breakeven"] += 1

        # Choose default path if none given
        if path is None:
            os.makedirs("runtime", exist_ok=True)
            suffix = "24h" if window_seconds == 24*60*60 else "7d" if window_seconds == 7*24*60*60 else f"{window_seconds}s"
            path = os.path.join("runtime", f"engine_summary_{suffix}.csv")

        with open(path, "w") as f:
            f.write("Engine,PnL,Trades,Wins,Losses,Breakeven\n")
            for e, m in agg.items():
                f.write(f"{e},{m['pnl']:.2f},{int(m['trades'])},{int(m['wins'])},{int(m['losses'])},{int(m['breakeven'])}\n")

        telemetry.log("export", "ENGINE_SPLIT_CSV", path, {"engines": list(agg.keys()), "window_s": window_seconds})
    except Exception as _e:
        # Non-blocking
        try:
            telemetry.log("export", "ENGINE_SPLIT_CSV_ERR", str(_e), {"window_s": window_seconds})
        except Exception:
            pass

# ------------------------------------------
# Single scan
# ------------------------------------------
async def scan_once(ex):
    tf5  = fetch_ohlcv(ex, "5m", None)
    if not has_series(tf5, "timestamp", "close"): return None
    tf15 = fetch_ohlcv(ex, "15m", None)
    if not has_series(tf15, "timestamp", "close"): return None
    tf1h = fetch_ohlcv(ex, "1h", None)
    if not has_series(tf1h, "timestamp", "close"): return None
    tf1m = fetch_ohlcv(ex, "1m", None)  # micro-noise input (auto min bars)

    now_ts = tf5["timestamp"][-1]
    price  = float(tf5["close"][-1])
    try:
        state.set_k("last_price", price)  # for dashboard
        state.set_k("last_scan_ts", now_ts)
    except Exception:
        pass

    # Daily context
    pdh, pdl = prior_day_high_low(tf1h, now_ts)
    delta_pos = pseudo_delta(tf5, 30) > 0
    oi_up = True

    # Single-position policy
    if getattr(C, "SINGLE_POSITION_MODE", True) and db.has_open_trade():
        try:
            ot = db.get_open_trade() if hasattr(db, "get_open_trade") else None
            info = {"open_id": ot[0] if ot else None, "side": ot[2] if ot else None, "entry": ot[3] if ot else None}
        except Exception:
            info = {}
        telemetry.log("scan", "SKIP", "single-position mode (trade open)", info)
        return None

    # Re-entry hygiene
    re_block = _gate_reentry(now_ts, price, side="NONE")  # pre-draft: only bar/time checks
    if re_block:
        # Add diagnostics and **stop** this scan so we don't trade on same-bar/cooldown
        last = _recent_closed_trade()
        ago = None
        last_entry = None
        if last and last.get("closed"):
            ago = max(0, _now_s() - int(last["closed"]//1000))
            last_entry = last.get("entry")
        telemetry.log("scan", "REENTRY_PRE", re_block, {"price": price, "side": "NONE", "last_entry": last_entry, "ago": ago})
        return None

    # Rules engine (respect ENGINE_ORDER)
    draft = None
    ordered = _engine_order()
    meta = {"engine": (ordered[0] if ordered else "trendscalp")}
    tried = []
    for eng in ordered:
        # seed meta with current engine for diagnostics even if engine returns None
        meta = {"engine": eng}
        tried.append(eng)
        if eng == "trendscalp":
            d = _try_trendscalp(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
        elif eng == "taser":
            d = _call_taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
        elif eng == "trendfollow":
            d = _try_trendfollow(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
        else:
            d = None

        # Ensure engine label is always present in result meta
        if d is not None:
            m = dict(getattr(d, "meta", {}) or {})
            if not m.get("engine"):
                m["engine"] = eng
                try:
                    setattr(d, "meta", m)
                except Exception:
                    pass

        if d is not None and getattr(d, "side", "NONE") != "NONE":
            draft = d
            break
        # keep meta if available for diagnostics
        if d is not None:
            meta = dict(getattr(d, "meta", {}) or {})

    if draft is None:
        # No engine produced a tradeable draft yet — create a neutral shell
        class _D: pass
        draft = _D()
        draft.side = "NONE"; draft.entry = 0.0; draft.sl = 0.0; draft.tps = []; draft.reason = "NO_EDGE"
        setattr(draft, "meta", meta if isinstance(meta, dict) else {})
        meta = dict(getattr(draft, "meta", {}) or {})
    else:
        meta = dict(getattr(draft, "meta", {}) or {})
    # Enrich meta for surveillance/flow manager
    try:
        meta.update({
            "pdh": float(pdh) if pdh is not None else None,
            "pdl": float(pdl) if pdl is not None else None,
            "flow_enabled": bool(getattr(C, "FLOW_ENABLED", True)),
            "flow_be_at_r_pct": float(getattr(C, "FLOW_BE_AT_R_PCT", 0.75)),
            "flow_tp2_r_mult": float(getattr(C, "FLOW_TP2_R_MULT", 1.6)),
            "flow_tp3_r_mult": float(getattr(C, "FLOW_TP3_R_MULT", 2.6)),
            # expose SL rails so surveil_loop can clamp consistently
            "min_sl_pct": float(getattr(C, "MIN_SL_PCT", 0.0045)),
            "max_sl_pct": float(getattr(C, "MAX_SL_PCT", 0.0120)),
            # fees for BE padding
            "fee_pct": float(getattr(C, "FEE_PCT", 0.0005)),
            "fee_pad_mult": float(getattr(C, "FEE_PAD_MULT", 2.0)),
        })
    except Exception:
        pass

    # ---- Multi-TF heatmap (build once, persist, confluence-gate) ----
    hm_multi = {}
    try:
        tf1d  = fetch_ohlcv(ex, "1d", None)
        if not has_series(tf1d, "close", "high", "low"):
            tf1d = {"close":[], "high":[], "low":[], "volume":[], "timestamp":[]}

        # 30d synthetic from 1h (720 bars)
        if has_series(tf1h, "close", "high", "low", "timestamp"):
            tf30d = {
                "timestamp": tf1h["timestamp"][-720:],
                "close":     tf1h["close"][-720:],
                "high":      tf1h["high"][-720:],
                "low":       tf1h["low"][-720:],
                "volume":    (tf1h.get("volume") or [])[-720:],
            }
        else:
            tf30d = None

        if build_liquidity_heatmap_multi:
            hm_multi = build_liquidity_heatmap_multi(tf5, tf15, tf1h, tf1d, tf30d)
            # persist for 90d analysis
            try: hm_save(int(now_ts), hm_multi)
            except Exception: pass

            # store per-TF levels for auditor & dashboard
            meta["heatmap_levels_5m"]  = (hm_multi.get("5m",  {}).get("levels") or [])[:24]
            meta["heatmap_levels_15m"] = (hm_multi.get("15m", {}).get("levels") or [])[:24]
            meta["heatmap_levels_1h"]  = (hm_multi.get("1h",  {}).get("levels") or [])[:24]
            meta["heatmap_levels_1d"]  = (hm_multi.get("1d",  {}).get("levels") or [])[:24]
            if "30d" in hm_multi:
                meta["heatmap_levels_30d"] = (hm_multi.get("30d", {}).get("levels") or [])[:24]
            # Hints for structural trailing (surveillance may use these)
            meta.setdefault("trail_hints", {})
            meta["trail_hints"].update({
                "use_vwap": True,
                "use_avwap": True,
                "use_pdh_pdl": True,
            })

            # confluence gate — aggression aware
            if draft.side != "NONE":
                gp = _hm_gate_params()
                gate = confluence_gate(hm_multi, price, draft.side,
                                       tol_pct=gp["tol_pct"], need_tfs=gp["need_tfs"], top_n=gp["top_n"])
                if gate.get("block"):
                    telemetry.log("scan", "FILTER_HEATMAP_BLOCK", gate.get("why",""), gate)
                    # Try next engines in configured order (skip the one we just used)
                    current_engine = (getattr(draft, "meta", {}) or {}).get("engine", "").lower()
                    next_try = [e for e in _engine_order() if e != current_engine]
                    alt = None
                    for eng in next_try:
                        if eng == "trendscalp":
                            alt = _try_trendscalp(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi)
                        elif eng == "trendfollow":
                            alt = _try_trendfollow(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi)
                        elif eng == "taser":
                            alt = _call_taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
                        else:
                            alt = None
                        if alt is not None and getattr(alt, "side", "NONE") != "NONE":
                            draft = alt
                            meta = dict(getattr(draft, "meta", {}) or {})
                            break
                    if getattr(draft, "side", "NONE") == "NONE":
                        meta_hm = dict(meta or {})
                        meta_hm.setdefault("engine", (ordered[0] if ordered else "trendscalp"))
                        msg = no_trade_message(price, f"Heatmap block: {gate.get('why','')}", meta_hm)
                        try: await tg_send(msg)
                        except Exception: pass
                        return None

        # keep simple single-TF heatmap for backward compatibility
        if build_liquidity_heatmap and not meta.get("heatmap_levels"):
            hm_simple = build_liquidity_heatmap(tf5, window=120) or {}
            meta["heatmap_levels"] = (hm_simple.get("levels") or [])[:12]

    except Exception as e:
        telemetry.log("scan", "HEATMAP_ERR", str(e), {})

    # attach meta
    draft.meta = meta
    # Last-mile safety only for actionable drafts (avoid SL_PADDED noise on NONE drafts)
    try:
        _side_ok = str(getattr(draft, "side", "NONE")).upper() in ("LONG","SHORT")
        _entry_ok = float(getattr(draft, "entry", 0.0)) > 0.0
        _sl_ok    = float(getattr(draft, "sl", 0.0))    > 0.0
    except Exception:
        _side_ok = _entry_ok = _sl_ok = False
    if _side_ok and _entry_ok and _sl_ok:
        _enforce_min_sl(draft, price)
        _sanitize_tp_order(draft)

    # If no edge, try fallback engines in configured order
    if draft.side == "NONE":
        attempt_map = {}
        for eng in _engine_order():
            attempt_map[eng] = "SKIPPED"
        for eng in _engine_order():
            if eng == (getattr(draft, "meta", {}) or {}).get("engine", "").lower():
                continue
            if eng == "trendscalp":
                alt = _try_trendscalp(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi or {})
            elif eng == "trendfollow":
                alt = _try_trendfollow(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m, hm_multi or {})
            elif eng == "taser":
                alt = _call_taser_signal(price, tf5, tf15, tf1h, pdh, pdl, oi_up, delta_pos, tf1m)
            else:
                alt = None
            attempt_map[eng] = "NONE" if (alt is None or getattr(alt, "side", "NONE") == "NONE") else "OK"
            if alt is not None and getattr(alt, "side", "NONE") != "NONE":
                draft = alt
                break
        if getattr(draft, "side", "NONE") == "NONE":
            meta_final = dict(getattr(draft, "meta", {}) or {})
            if not meta_final.get("engine"):
                meta_final["engine"] = (ordered[0] if ordered else "trendscalp")
            msg = no_trade_message(price, getattr(draft, "reason", "no setup"), meta_final)
            try: logger.console(msg)
            except Exception: pass
            telemetry.log("scan", "NO_TRADE", getattr(draft, "reason", "no setup"), {**meta_final, **{"attempt": attempt_map}})
            try: await tg_send(msg)
            except Exception: pass
            return None

    # Re-entry proximity check with the actual side (post-draft)
    prox = _gate_reentry(now_ts, price, side=draft.side)
    if prox and "BLOCK_REENTRY_PCT" in prox:
        # Add diagnostics to REENTRY_BLOCK log
        last = _recent_closed_trade()
        ago = None
        last_entry = None
        if last and last.get("closed"):
            ago = max(0, _now_s() - int(last["closed"]//1000))
            last_entry = last.get("entry")
        telemetry.log("scan", "REENTRY_BLOCK", prox, {"price": price, "side": draft.side, "last_entry": last_entry, "ago": ago})
        return None

    telemetry.log("scan", "RULE_APPROVED",
                  f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps})

    # --------- AUDIT (OpenAI) disabled unless OPENAI_USE=true ---------
    verdict = {"decision": "APPROVE", "why": "AI audit disabled"}
    if getattr(C, "OPENAI_USE", False):
        try:
            from app.audit import approve_with_rationale
            verdict = await approve_with_rationale(draft, tf5, tf15, tf1h)
            telemetry.log("audit", verdict.get("decision","?"), verdict.get("why",""), {"verdict": verdict})
            try: logger.console("[AUDIT] " + str(verdict))
            except Exception: pass
            state.set_k("last_audit", verdict)
        except Exception as e:
            telemetry.log("audit", "ERROR", str(e), {})
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
            telemetry.log("exec", "AUDIT_BLOCKED", verdict.get("why", ""),
                          {"side": draft.side, "entry": draft.entry})
            try: await tg_send(block_msg)
            except Exception: pass
            return None

    # Approved → notify
    approved_msg = signal_message(draft)
    try: logger.console(approved_msg)
    except Exception: pass
    telemetry.log("exec", "APPROVED", f"{draft.side} — {draft.reason}",
                  {"side": draft.side, "entry": draft.entry, "sl": draft.sl, "tps": draft.tps, "engine": (getattr(draft, 'meta', {}) or {}).get('engine', 'trendscalp')})
    state.set_k("last_signal", {
        "side": draft.side, "entry": draft.entry, "sl": draft.sl,
        "tps": draft.tps, "reason": draft.reason
    })
    # remember which 5m bar we traded on (for REQUIRE_NEW_BAR)
    state.set_k("last_traded_bar_ts", int(now_ts))

    # Guard: skip sizing when entry/SL are missing or zero
    try:
        _entry_ok = isinstance(draft.entry, (int,float)) and float(draft.entry) > 0
        _sl_ok    = isinstance(draft.sl,    (int,float)) and float(draft.sl)    > 0
    except Exception:
        _entry_ok = _sl_ok = False
    if not (_entry_ok and _sl_ok):
        telemetry.log("exec", "SKIP_SIZE_NO_EDGE", "Non-actionable signal (entry/sl missing or zero)",
                      {"entry": draft.entry, "sl": draft.sl, "side": draft.side})
        try:
            msg = no_trade_message(price, "No edge at actionable levels", draft.meta)
            await tg_send(msg)
        except Exception:
            pass
        return None

    # Balance → qty
    try:
        from app.data import fetch_balance_quote
        balance = fetch_balance_quote(ex, C.PAIR)
    except Exception:
        balance = 1000.0

    qty = choose_size(balance, draft.entry, draft.sl)
    if qty <= 0:
        # Throttle spam: only notify once per 5m bar
        try:
            st = state.get() if hasattr(state, "get") else {}
            last_err_bar = st.get("last_size_error_bar")
            if last_err_bar != now_ts:
                try: await tg_send("Cannot size position (check balance/SL).")
                except Exception: pass
                try: state.set_k("last_size_error_bar", int(now_ts))
                except Exception: pass
        except Exception:
            try: await tg_send("Cannot size position (check balance/SL).")
            except Exception: pass
        try:
            telemetry.log("exec", "SIZE_ZERO", "qty<=0 after sizing", {
                "engine": (getattr(draft, 'meta', {}) or {}).get('engine', 'taser'),
                "entry": draft.entry, "sl": draft.sl, "balance": balance,
                "min_sl_pct": float(getattr(C, "MIN_SL_PCT", 0.0045)),
                "min_qty": float(getattr(C, "MIN_QTY", 1.0)),
                "risk_pct": float(getattr(C, "RISK_PCT", 0.5)),
                "cap_frac": float(getattr(C, "CAPITAL_FRACTION", 0.5)),
                "dry_run": bool(getattr(C, "DRY_RUN", True)),
                "paper_start": float(getattr(C, "PAPER_START_BALANCE", 0.0)),
                "paper_use_start": bool(getattr(C, "PAPER_USE_START_BALANCE", False)),
            })
        except Exception:
            pass
        return None

    # Save trade + tag account
    tid = db.new_trade(C.PAIR, draft.side, draft.entry, draft.sl, draft.tps, qty, draft.meta)
    if hasattr(db, "tag_trade_account"):
        db.tag_trade_account(tid, "PAPER" if C.DRY_RUN else "LIVE")

    # Record audit decision on timeline (if any)
    try:
        db.append_event(tid, "AUDIT", f"{verdict.get('decision','SKIPPED')} — {verdict.get('why','AI off')}")
    except Exception:
        pass

    # Place bracket
    try:
        telemetry.log("exec", "BRACKET_PLACE", "placing bracket with flow-aware meta", {
            "tid": tid,
            "side": draft.side,
            "entry": draft.entry,
            "sl": draft.sl,
            "tps": draft.tps,
            "flow": {
                "enabled": meta.get("flow_enabled"),
                "be_r_pct": meta.get("flow_be_at_r_pct"),
                "tp2": meta.get("flow_tp2_r_mult"),
                "tp3": meta.get("flow_tp3_r_mult"),
            }
        })
    except Exception:
        pass
    
    place_bracket(ex, C.PAIR, draft, qty, tid)
    # Opportunistic: refresh the engine split CSVs for dashboard
    try:
        _export_engine_split_csv(24*60*60)        # 24h
        _export_engine_split_csv(7*24*60*60)      # 7d
    except Exception:
        pass

    return (ex, draft, tid, qty)

# ------------------------------------------
# Minimal indicators for TrendScalp FSM (ATR5 required; ADX14 optional)
def compute_indicators(tf5: dict, tf15: dict, tf1h: dict) -> dict:
    try:
        atr5 = _atr(tf5.get("high") or [], tf5.get("low") or [], 30)
    except Exception:
        atr5 = 0.0
    # ADX optional for ML; provide 0.0 if not available here
    adx14 = 0.0
    return {"atr5": float(atr5), "adx14": float(adx14)}

# ------------------------------------------
# Scheduler
# ------------------------------------------
def make_fetcher():
    return lambda ex_, tf, lim=None: fetch_ohlcv(ex_, tf, lim)

async def run_scheduler():
    try:
        telemetry.init_telemetry()
    except Exception:
        pass

    try:
        # Emit startup heartbeat with normalized engine order for dashboards
        from app import config as C
        eo = []
        try:
            eo = _engine_order()
        except Exception:
            eo = list(getattr(C, "ENGINE_ORDER", ["trendscalp"]))
        telemetry.log_startup_engine_order(eo)
    except Exception:
        pass

    db.init()
    if hasattr(db, "init_settings"): db.init_settings()
    if hasattr(db, "ensure_trades_account_column"): db.ensure_trades_account_column()
    memory.init_memory_tables()
    hm_init(); hm_purge()

    try:
        await tg_send("Trading runtime: starting up ✅")
    except Exception:
        pass

    ex = exchange()
    try:
        telemetry.log("run", "START", "scheduler started", {
            "api": getattr(ex, "urls", {}).get("api"),
            "pair": getattr(C, "PAIR", ""),
            "mode": "PAPER" if getattr(C, "DRY_RUN", False) else "LIVE"
        })
    except Exception:
        pass

    # Enforce: when LIVE, sizing must use free margin (never the paper start balance)
    try:
        if not getattr(C, "DRY_RUN", True) and getattr(C, "PAPER_USE_START_BALANCE", False):
            C.PAPER_USE_START_BALANCE = False
            telemetry.log("run", "LIVE_SIZING", "DRY_RUN=false → forcing PAPER_USE_START_BALANCE=False", {})
    except Exception:
        pass

    # Recovery
    try:
        rec = await recover_open_trades(ex)
    except Exception as e:
        telemetry.log("recover", "ERROR", str(e), {"trace": traceback.format_exc()})
        rec = {"resume": False}

    if rec and rec.get("resume"):
        fetcher = make_fetcher()
        tid = rec["trade_id"]; d = rec["draft"]; qty = rec["qty"]
        try: await tg_send(f"Resuming management of open trade #{tid} — {d.side} @ {d.entry:.4f}")
        except Exception: pass
        try:
            eng = (getattr(d, "meta", {}) or {}).get("engine", db.get_trade_engine(tid)).lower()
        except Exception:
            eng = db.get_trade_engine(tid)
        if eng == "trendscalp":
            await ts_runner.run_trendscalp_manage(ex, C.PAIR, d, tid, qty, fetcher, compute_indicators)
        else:
            from app.surveillance import surveil_loop
            await surveil_loop(ex, C.PAIR, d, tid, qty, fetcher, lambda: 0.0)

    scan_delay = float(getattr(C, "SCAN_INTERVAL_SECONDS", 2.0))
    # Periodic engine-split CSV export (keeps dashboard fresh). Allow disabling by setting ENGINE_SPLIT_EXPORT_SECS <= 0
    export_interval_s = int(getattr(C, "ENGINE_SPLIT_EXPORT_SECS", 300))  # default 5min
    if export_interval_s <= 0:
        _next_export_ts = None
        try:
            telemetry.log("export", "ENGINE_SPLIT_EXPORT_OFF", "engine-split CSV auto-export disabled", {})
        except Exception:
            pass
    else:
        _next_export_ts = time.time() + max(60, export_interval_s)  # at least 60s

    while True:
        try:
            result = await scan_once(ex)
            if result:
                _ex, draft, tid, qty = result
                fetcher = make_fetcher()
                # Choose manager by engine stamped in draft.meta
                try:
                    eng = (getattr(draft, "meta", {}) or {}).get("engine", "taser").lower()
                except Exception:
                    eng = "taser"
                if eng == "trendscalp":
                    await ts_runner.run_trendscalp_manage(_ex, C.PAIR, draft, tid, qty, fetcher, compute_indicators)
                else:
                    from app.surveillance import surveil_loop
                    await surveil_loop(_ex, C.PAIR, draft, tid, qty, fetcher, lambda: 0.0)
                continue

            # Periodic export tick (only if enabled)
            try:
                if _next_export_ts is not None:
                    now = time.time()
                    if now >= _next_export_ts:
                        _export_engine_split_csv(24*60*60)
                        _export_engine_split_csv(7*24*60*60)
                        _next_export_ts = now + export_interval_s
            except Exception:
                pass

            await asyncio.sleep(scan_delay)

        except asyncio.CancelledError:
            try: telemetry.log("run", "STOP", "scheduler cancelled", {})
            except Exception: pass
            raise
        except Exception as e:
            tb = traceback.format_exc()
            telemetry.log("run", "ERROR", str(e), {"trace": tb})
            try:
                last = "\n".join(tb.splitlines()[-3:])
                await tg_send(f"[RUN] Error: {e}\n{last}")
            except Exception:
                pass
            await asyncio.sleep(5)