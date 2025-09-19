# app/runners/trendscalp_runner.py
from __future__ import annotations  # noqa: I001

# Standard library
import asyncio
import time
from math import isfinite
from typing import Any, List, Callable, Optional

# Third-party
import ccxt

# Local application imports
from app import config as C
from app import db, telemetry
from app.components.guards import guard_sl, post_entry_validity

# regime-based exit/partial helpers
from app.execution import ensure_partial_tp1, exit_remainder_market
from app.indicators import rsi_compact
from app.managers.trendscalp_fsm import (
    Context,
    build_entry_validity_snapshot,
    is_hard_invalidation,  # NEW
    propose,
)
from app.messenger import tg_send
from app.money import calc_pnl
from app.regime import soft_degrade

# Re-use TASER helpers so messages / payloads remain identical
from app.surveillance import (
    _confirm_sl_breach,
    _fmt,
    _replace_stop_loss,
    _replace_takeprofits,
)

# --- Flow/ML helpers ---
from app.manage.flow import after_tp1_replace, giveback_exit
from app import state


# --- Portfolio/Risk helpers (wired inline) ---
# --- Ledger helpers (call when you actually submit/cancel/close orders) ---
def _ledger_open(
    trade_id: str,
    ts_ms: int,
    symbol: str,
    side: str,
    entry: float,
    sl: float,
    size_usd: float,
    meta: dict,
) -> None:
    try:
        import app.config as C
        from app.ledger_duck import TradeOpen, append_open, ensure_schema

        ensure_schema(getattr(C, "LEDGER_PATH", "ledger.duckdb"))
        append_open(
            getattr(C, "LEDGER_PATH", "ledger.duckdb"),
            TradeOpen(
                trade_id, ts_ms, symbol, side, float(entry), float(sl), float(size_usd), meta
            ),
        )
    except Exception as e:
        try:
            from app.telemetry import log as _tlog

            _tlog("mgr", "LEDGER OPEN FAIL", str(e), {})
        except Exception:
            pass


def _ledger_close(
    trade_id: str, ts_ms: int, exit_px: float, pnl_usd: float, reason: str, meta: dict
) -> None:
    try:
        import app.config as C
        from app.ledger_duck import TradeClose, append_close, ensure_schema

        ensure_schema(getattr(C, "LEDGER_PATH", "ledger.duckdb"))
        append_close(
            getattr(C, "LEDGER_PATH", "ledger.duckdb"),
            TradeClose(trade_id, ts_ms, float(exit_px), float(pnl_usd), reason, meta),
        )
    except Exception as e:
        try:
            from app.telemetry import log as _tlog

            _tlog("mgr", "LEDGER CLOSE FAIL", str(e), {})
        except Exception:
            pass


_R_OPEN = 0  # live trades
_R_EQUITY_USD = None  # set from config or broker API elsewhere
_R_DAILY_LOSS = 0.0
_R_DAILY_RESET_MARK = None  # utc midnight token


def _risk_can_open(sl_distance_pct: float) -> tuple[bool, float, str]:
    try:
        import app.config as C
    except Exception:
        import importlib as _importlib

        C = _importlib.import_module("config")

    max_conc = int(getattr(C, "RISK_MAX_CONCURRENT_TRADES", 2))
    if _R_OPEN >= max_conc:
        return False, 0.0, "max concurrent trades reached"

    # equity detection: if not set, assume 10_000 for dry-run sizing
    eq = float(_R_EQUITY_USD or 10_000.0)
    risk_pct = float(getattr(C, "RISK_CAPITAL_PCT_PER_TRADE", 0.10))
    risk_usd = max(0.0, eq * max(0.0, min(1.0, risk_pct)))

    # daily stop check (simple; _R_DAILY_LOSS updated on closes)
    stop_pct = float(getattr(C, "RISK_DAILY_STOP_PCT", 0.25))
    if stop_pct > 0 and _R_DAILY_LOSS >= eq * stop_pct:
        return False, 0.0, "daily stop already hit"

    size_usd = risk_usd / max(1e-9, sl_distance_pct)
    return True, size_usd, ""


async def run_trendscalp_manage(
    ex: ccxt.Exchange,
    pair: str,
    draft,  # object with .side .entry .sl .tps .meta (like TASER draft)
    trade_id: int,
    qty: float,
    # fetch_ohlcv(ex, tf, n) -> {
    #   "timestamp": [...],
    #   "open": [...],
    #   "high": [...],
    #   "low": [...],
    #   "close": [...]
    # }
    fetch_ohlcv: Callable,
    # indicators(tf5, tf15, tf1h) -> {"atr5": float, "adx14": float, ...}
    indicators: Callable,
) -> None:
    """
    Manage ONE TrendScalp position using the FSM proposals and TASER's venue/TG helpers.
    Keeps telemetry.csv and Telegram text consistent with TASER.
    """
    side = draft.side.upper()
    is_long = side == "LONG"
    entry = float(draft.entry)
    sl_cur = float(draft.sl)
    tp_list = list(draft.tps or [])
    tp1 = float(tp_list[0]) if len(tp_list) >= 1 else None
    tp2 = float(tp_list[1]) if len(tp_list) >= 2 else None
    tp3 = float(tp_list[2]) if len(tp_list) >= 3 else None

    # State we maintain across ticks
    hit_tp1 = False
    hit_tp2 = False
    tp_replaced = False
    last_status_ts = 0

    # Track if we ever saw RUNNER since entry (for flip handling), and debounce STATUS
    had_runner_since_entry: bool = False
    _last_status: dict[str, object] = {}
    # On-change-only emission: track a compact signature of material fields
    _last_status_sig: tuple | None = None
    # Revalidation plan emission (only on change)
    _last_plan_sig: tuple | None = None

    # Bar tracking for post‑TP1 grace behavior
    bars_since_tp1 = 0
    _last_seen_bar_ts = None

    # Milestone mode (toggle via env). If disabled, we rely on FSM SL proposals entirely.
    MS_MODE = bool(getattr(C, "TS_MILESTONE_MODE", True))
    # milestone every 0.5R beyond TP1
    MS_STEP_R = float(getattr(C, "TS_MS_STEP_R", 0.5))
    # each milestone raises SL by +0.25R from entry
    MS_LOCK_DELTA_R = float(getattr(C, "TS_MS_LOCK_DELTA_R", 0.25))
    # at TP2, SL jumps to 70% of entry→TP2
    TS_TP2_LOCK_FRACR = float(getattr(C, "TS_TP2_LOCK_FRACR", 0.70))
    TS_POST_TP2_ATR_MULT = float(getattr(C, "TS_POST_TP2_ATR_MULT", 0.50))

    # Regime tracking (CHOP vs RUNNER) with simple hysteresis
    TS_REGIME_AUTO = bool(getattr(C, "TS_REGIME_AUTO", True))
    TS_ADX_UP = float(getattr(C, "TS_ADX_UP", 26.0))
    TS_ADX_DN = float(getattr(C, "TS_ADX_DN", 23.0))
    TS_ATR_UP = float(getattr(C, "TS_ATR_UP", 0.0040))  # 0.40%
    TS_ATR_DN = float(getattr(C, "TS_ATR_DN", 0.0035))  # 0.35%
    TS_PARTIAL_TP1 = float(getattr(C, "TS_PARTIAL_TP1", 0.5))

    last_regime: Optional[str] = None
    regime: Optional[str] = None

    initial_sl = float(draft.sl)
    R_init = abs(entry - initial_sl) if abs(entry - initial_sl) > 1e-12 else 0.0
    last_ms_k = 0

    # Excursions
    best_hi_seen = entry
    best_lo_seen = entry
    mfe_abs = 0.0
    mae_abs = 0.0

    # Cooldowns (re-use TASER environment knobs)
    SL_TIGHTEN_COOLDOWN_SEC = int(getattr(C, "SL_TIGHTEN_COOLDOWN_SEC", 55))
    TP_EXTEND_COOLDOWN_SEC = int(getattr(C, "TP_EXTEND_COOLDOWN_SEC", 55))
    last_sl_move_ts: float = 0.0
    last_tp_ext_ts: float = 0.0

    # Optional: TP hit confirmation bars (touch vs close); default 0 = touch
    TP_HIT_CONFIRM_BARS = int(getattr(C, "TP_HIT_CONFIRM_BARS", 0))

    # Optional: periodic position-flat check via exchange (defensive); 0 disables
    TS_CHECK_POS_EVERY_S = int(getattr(C, "TS_CHECK_POS_EVERY_S", 10))
    _last_pos_check_ts = 0

    def _confirm_tp_hit(fetch, ex, bars: int, want_long: bool, level: float) -> bool:
        try:
            if bars <= 0:
                return True
            tf = fetch(ex, "1m", max(3, bars + 1))
            closes = tf.get("close") or []
            if len(closes) < bars:
                return False
            if want_long:
                return all(float(c) >= level for c in closes[-bars:])
            else:
                return all(float(c) <= level for c in closes[-bars:])
        except Exception:
            return False

    # One-time reconcile BEFORE we announce manage: if venue is flat, close and exit silently
    if getattr(C, "DRY_RUN", True):
        pass  # skip venue-flat reconcile in paper mode
    else:
        try:
            size0 = None
            if hasattr(ex, "fetch_positions"):
                try:
                    poss0 = ex.fetch_positions([pair])
                    for p in poss0:
                        if str(p.get("symbol")) == pair:
                            size0 = abs(
                                float(
                                    p.get("contracts")
                                    or p.get("positionAmt")
                                    or p.get("size")
                                    or 0.0
                                )
                            )
                            break
                except Exception:
                    size0 = None
            if size0 is None and hasattr(ex, "fetch_position"):
                try:
                    p0 = ex.fetch_position(pair)
                    size0 = abs(
                        float(p0.get("contracts") or p0.get("positionAmt") or p0.get("size") or 0.0)
                    )
                except Exception:
                    size0 = None
            if size0 is not None and size0 <= 1e-8:
                # venue already flat — close DB and exit WITHOUT sending manage header spam
                exit_px0 = float(getattr(draft, "entry", 0.0) or 0.0)
                try:
                    pnl0 = calc_pnl(draft.side, float(draft.entry), exit_px0, float(qty))
                except Exception:
                    pnl0 = 0.0
                try:
                    _msg = f"Closed due to venue qty=0 @ {exit_px0:.4f} | PnL {pnl0:.2f}"
                    db.append_event(trade_id, "VENUE_FLAT_CLOSE", _msg)
                except Exception:
                    pass
                try:
                    db.close_trade(trade_id, exit_px0, pnl0, "CLOSED_VENUE_FLAT")
                except Exception:
                    try:
                        db.update_trade_status(trade_id, "CLOSED")
                    except Exception:
                        pass
                try:
                    _txt = "venue qty=0 — pre‑manage reconcile"
                    telemetry.log(
                        "manage",
                        "CLOSED_VENUE_FLAT",
                        _txt,
                        {"tid": trade_id, "exit": exit_px0, "pnl": pnl0},
                    )
                except Exception:
                    pass
                try:
                    await tg_send(f"⚪ EXIT — {pair}\nqty flat on venue")
                except Exception:
                    pass
                return
        except Exception:
            pass

    # Init bookkeeping
    try:
        db.update_trade_status(trade_id, "OPEN")
        _hdr = (
            f"[TRENDSCALP] {side} {pair} @ {_fmt(entry)} "
            f"SL {_fmt(sl_cur)} TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
        )
        db.append_event(
            trade_id,
            "MANAGE_START",
            _hdr,
        )
        await tg_send(
            f"[MANAGE][TRENDSCALP] {side} — {pair}\n"
            f"Entry {_fmt(entry)} | SL {_fmt(sl_cur)} | TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
        )
        _txt = f"milestone={MS_MODE} stepR={MS_STEP_R} lockDeltaR={MS_LOCK_DELTA_R}"
        telemetry.log(
            "manage",
            "MS_MODE",
            _txt,
            {"engine": "trendscalp", "tid": trade_id},
        )
    except Exception:
        pass

    while True:
        await asyncio.sleep(C.MANAGE_POLL_SECONDS)
        # Wall-clock now for cooldowns/signatures
        now = time.time()
        # Defensive: exit if position is flat on venue (if supported).
        # Does not change messages unless we actually exit.
        try:
            if getattr(C, "DRY_RUN", True):
                raise Exception("skip venue-flat reconcile in DRY_RUN")
            now_ts = int(time.time())
            if TS_CHECK_POS_EVERY_S > 0 and now_ts - _last_pos_check_ts >= TS_CHECK_POS_EVERY_S:
                _last_pos_check_ts = now_ts
                size = None
                if hasattr(ex, "fetch_positions"):
                    try:
                        poss = ex.fetch_positions([pair])
                        for p in poss:
                            if str(p.get("symbol")) == pair:
                                size = abs(
                                    float(
                                        p.get("contracts")
                                        or p.get("positionAmt")
                                        or p.get("size")
                                        or 0.0
                                    )
                                )
                                break
                    except Exception:
                        size = None
                # If still unknown, try a generic derivative position field if present
                if size is None and hasattr(ex, "fetch_position"):
                    try:
                        p = ex.fetch_position(pair)
                        size = abs(
                            float(
                                p.get("contracts") or p.get("positionAmt") or p.get("size") or 0.0
                            )
                        )
                    except Exception:
                        size = None
                if size is not None and size <= 1e-8:
                    # --- Hard reconcile: venue is flat; close DB trade once and stop managing
                    exit_px = float(locals().get("px", sl_cur))
                    try:
                        pnl = calc_pnl(draft.side, float(entry), exit_px, float(qty))
                    except Exception:
                        pnl = 0.0

                    # Best-effort: close trade; if close_trade fails, at least flip status
                    closed_ok = False
                    try:
                        _msg2 = f"Closed due to venue qty=0 @ {exit_px:.4f} | PnL {pnl:.2f}"
                        db.append_event(trade_id, "VENUE_FLAT_CLOSE", _msg2)
                    except Exception:
                        pass
                    try:
                        db.close_trade(trade_id, exit_px, pnl, "CLOSED_VENUE_FLAT")
                        closed_ok = True
                    except Exception:
                        # Fallback: mark status closed to avoid scheduler re-manage loops
                        try:
                            db.update_trade_status(trade_id, "CLOSED")
                        except Exception:
                            pass

                    try:
                        _txt2 = "venue qty=0 — manager exiting"
                        telemetry.log(
                            "manage",
                            "CLOSED_VENUE_FLAT",
                            _txt2,
                            {"tid": trade_id, "exit": exit_px, "pnl": pnl},
                        )
                    except Exception:
                        pass
                    try:
                        await tg_send(f"⚪ EXIT — {pair}\nqty flat on venue")
                    except Exception:
                        pass

                    return
        except Exception:
            pass

        # Pull a longer 1m window so FSM can do structure‑trail and stall detection
        tf1m = fetch_ohlcv(ex, "1m", 240)
        highs = tf1m.get("high") or []
        lows = tf1m.get("low") or []
        closes = tf1m.get("close") or []
        stamps = tf1m.get("timestamp") or []
        if not highs or not lows or not closes:
            telemetry.log(
                "surveil", "NO_1M", "empty 1m; continue", {"engine": "trendscalp", "tid": trade_id}
            )
            continue
        hi = float(highs[-1])
        lo = float(lows[-1])
        px = float(closes[-1])

        # Compute RSI14 series for stall confirmation in FSM via shared indicators helper
        try:
            tf1m["rsi14"] = rsi_compact([float(x) for x in closes], 14) or []
        except Exception:
            tf1m["rsi14"] = []

        # Track bar changes to count bars_since_tp1 for the FSM grace window
        try:
            cur_bar_ts = int(stamps[-1]) if stamps else None
        except Exception:
            cur_bar_ts = None
        if _last_seen_bar_ts is None:
            _last_seen_bar_ts = cur_bar_ts
        elif cur_bar_ts is not None and _last_seen_bar_ts != cur_bar_ts:
            # advanced to a new bar
            _last_seen_bar_ts = cur_bar_ts
            if hit_tp1:
                bars_since_tp1 += 1
            else:
                bars_since_tp1 = 0

        # MFE/MAE tracking
        best_hi_seen = max(best_hi_seen, hi)
        best_lo_seen = min(best_lo_seen, lo)
        if is_long:
            cur_mfe = max(0.0, best_hi_seen - entry)
            cur_mae = max(0.0, entry - best_lo_seen)
        else:
            cur_mfe = max(0.0, entry - best_lo_seen)
            cur_mae = max(0.0, best_hi_seen - entry)
        mfe_abs = max(mfe_abs, float(cur_mfe))
        mae_abs = max(mae_abs, float(cur_mae))

        # (removed periodic status block)

        # Optional: SL breach confirm (same as TASER behavior)
        sl_touch = (lo <= sl_cur) if is_long else (hi >= sl_cur)
        if sl_touch:
            need_sl_conf = int(getattr(C, "SL_CLOSE_CONFIRM_BARS", 0))
            if need_sl_conf > 0 and not _confirm_sl_breach(
                fetch_ohlcv, ex, need_sl_conf, is_long, sl_cur
            ):
                _txt3 = f"touch at {_fmt(sl_cur)}; wait {need_sl_conf} closes"
                telemetry.log(
                    "manage",
                    "SL_TOUCH_WAIT_CONFIRM",
                    _txt3,
                    {"pair": pair, "engine": "trendscalp", "tid": trade_id},
                )
            else:
                exit_px = sl_cur
                pnl = calc_pnl(draft.side, entry, exit_px, qty)
                try:
                    db.append_event(trade_id, "SL_HIT", f"Exit @ {_fmt(exit_px)}")
                    db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL")
                    telemetry.log(
                        "exec",
                        "CLOSED",
                        "SL",
                        {"exit": exit_px, "pnl": pnl, "engine": "trendscalp", "tid": trade_id},
                    )
                    await tg_send(f"🔴 SL HIT — {pair}\nExit {_fmt(exit_px)} | PnL {pnl:.2f}")
                except Exception:
                    pass
                return

        # Higher TFs + indicators (gives the FSM its meta signals, incl. ATR/ADX)
        try:
            tf5 = fetch_ohlcv(ex, "5m", 220)
            tf15 = fetch_ohlcv(ex, "15m", 220)
            tf1h = fetch_ohlcv(ex, "1h", 200)
            feats = indicators(tf5, tf15, tf1h)  # must at least provide atr5, adx14
        except Exception as e:
            telemetry.log(
                "manage", "INDICATORS_ERROR", str(e), {"engine": "trendscalp", "tid": trade_id}
            )
            feats = {}

        # --- Regime evaluation using ADX and ATR% (hysteresis). No series here; use thresholds.
        if TS_REGIME_AUTO:
            # Pull ADX/ATR with fallbacks to common keys to avoid zeros when feature names differ
            try:
                _adx_raw = (feats or {}).get("adx14")
                if _adx_raw is None:
                    _adx_raw = (feats or {}).get("adx")
                if _adx_raw is None:
                    _adx_raw = (feats or {}).get("di_adx_14")
                adx = float(_adx_raw) if _adx_raw is not None else 0.0
            except Exception:
                adx = 0.0
            try:
                _atr_raw = (feats or {}).get("atr5")
                if _atr_raw is None:
                    _atr_raw = (feats or {}).get("atr14")
                if _atr_raw is None:
                    _atr_raw = (feats or {}).get("atr")
                atr5 = float(_atr_raw) if _atr_raw is not None else 0.0
            except Exception:
                atr5 = 0.0
            atr_pct = (atr5 / px) if px > 0 else 0.0

            # Decide with hysteresis based on the previous regime
            if last_regime == "RUNNER":
                regime = "CHOP" if (adx <= TS_ADX_DN or atr_pct <= TS_ATR_DN) else "RUNNER"
            elif last_regime == "CHOP":
                regime = "RUNNER" if (adx >= TS_ADX_UP and atr_pct >= TS_ATR_UP) else "CHOP"
            else:
                regime = "RUNNER" if (adx >= TS_ADX_UP and atr_pct >= TS_ATR_UP) else "CHOP"

            if regime != last_regime:
                telemetry.log(
                    "manage",
                    "REGIME",
                    f"{last_regime} -> {regime}",
                    {
                        "engine": "trendscalp",
                        "tid": trade_id,
                        "adx14": round(adx, 3),
                        "atr_pct": round(atr_pct, 6),
                        "up": {"adx": TS_ADX_UP, "atr": TS_ATR_UP},
                        "dn": {"adx": TS_ADX_DN, "atr": TS_ATR_DN},
                    },
                )
            last_regime = regime
            if regime == "RUNNER":
                had_runner_since_entry = True
        else:
            regime = None

        # Latest ML snapshot mirrored by scheduler ML_TICK
        try:
            ml_last = state.get().get("ml_last") if hasattr(state, "get") else None
        except Exception:
            ml_last = None

        # --- Post‑Entry Validity Guard (pre‑TP1 only) ---
        pev_state = None
        pev_diag: dict[str, Any] = {}
        try:
            if getattr(C, "PEV_ENABLED", True) and not hit_tp1:
                # Ensure we have an entry snapshot (normally set at fill time);
                # build once if missing
                try:
                    if not getattr(draft, "meta", None):
                        draft.meta = {}
                    if "entry_validity" not in (draft.meta or {}):
                        # Build a snapshot using entry price (closest proxy to fill time)
                        ctx0 = Context(
                            price=entry,
                            side=side,
                            entry=entry,
                            sl=sl_cur,
                            tps=[t for t in [tp1, tp2, tp3] if t is not None],
                            tf1m=tf1m,
                            meta=draft.meta or {},
                        )
                        draft.meta["entry_validity"] = build_entry_validity_snapshot(ctx0, feats)
                except Exception:
                    pass

                # Evaluate continuation validity with fresh features (5m) and optional 1m confirm
                prev_state = ((draft.meta or {}).get("pe_guard") or {}).get("state")
                pev_state, pev_diag = post_entry_validity(side, px, feats, None, draft.meta, C)
                # Enrich diag with hard/soft if not provided by the underlying checker
                try:
                    if not isinstance(pev_diag, dict):
                        pev_diag = {}

                    # HARD: EMA-side flip (5m or 15m) + structure break on 1m with ATR pad
                    if "hard" not in pev_diag:
                        hard_diag = is_hard_invalidation(
                            px, is_long, getattr(draft, "meta", {}) or {}, tf1m
                        )
                        for k, v in hard_diag.items():
                            if k not in pev_diag:
                                pev_diag[k] = v

                    # SOFT: ADX/ATR degrade with slope-aware ADX minimum
                    if ("soft" not in pev_diag) or ("adx_min_eff" not in pev_diag):
                        adx_series_raw = (
                            (getattr(draft, "meta", {}) or {}).get("adx14_series")
                            or feats.get("adx14_series")
                            or []
                        )
                        atr_series_raw = (
                            (getattr(draft, "meta", {}) or {}).get("atr5_series")
                            or feats.get("atr5_series")
                            or []
                        )
                        closes_series_raw = tf5.get("close") if isinstance(tf5, dict) else []

                        adx_series_f: List[float] = (
                            [float(x) for x in adx_series_raw]
                            if isinstance(adx_series_raw, list)
                            else []
                        )
                        atr_series_f: List[float] = (
                            [float(x) for x in atr_series_raw]
                            if isinstance(atr_series_raw, list)
                            else []
                        )
                        closes_series_f: List[float] = (
                            [float(x) for x in closes_series_raw]
                            if isinstance(closes_series_raw, list)
                            else []
                        )

                        adx_min = float(getattr(C, "TS_ADX_MIN", 22.0))
                        atr_floor_pct = float(getattr(C, "TS_ATR_FLOOR_PCT", 0.0015))
                        sdiag = soft_degrade(
                            adx_series_f,
                            atr_series_f,
                            closes_series_f,
                            adx_min=adx_min,
                            atr_floor_pct=atr_floor_pct,
                            slope_bonus=float(getattr(C, "TS_ADX_SLOPE_BONUS", 2.0)),
                        )
                        for k, v in sdiag.items():
                            if k not in pev_diag:
                                pev_diag[k] = v
                except Exception:
                    pass

                if pev_state == "EXIT":
                    is_hard = bool((pev_diag or {}).get("hard"))
                    # If not a hard break, downgrade to WARN (tighten via FSM/guards) to
                    # avoid cutting winners on noise
                    if not is_hard:
                        telemetry.log(
                            "manage",
                            "PEV_DOWNGRADED",
                            "soft invalidation downgraded to WARN",
                            {**pev_diag, "engine": "trendscalp", "tid": trade_id},
                        )
                    else:
                        telemetry.log(
                            "manage",
                            "PEV_EXIT",
                            "pre‑TP1 hard invalidation",
                            {**pev_diag, "engine": "trendscalp", "tid": trade_id},
                        )
                        # Trigger exit via execution helper
                        exit_px_used = float(locals().get("px", sl_cur))
                        try:
                            exit_remainder_market(ex, pair, draft, trade_id, qty_hint=qty)
                        except Exception:
                            pass

                        # In DRY_RUN, if trade still appears open, force-close it locally to matc
                        # h EXIT intent.
                        try:
                            if getattr(C, "DRY_RUN", True):
                                ot = db.get_open_trade() if hasattr(db, "get_open_trade") else None
                                still_open = bool(ot and (ot[0] == trade_id))
                                if still_open:
                                    exit_px2 = float(locals().get("px", sl_cur))
                                    pnl2 = calc_pnl(draft.side, float(entry), exit_px2, float(qty))
                                    try:
                                        db.append_event(
                                            trade_id,
                                            "PEV_FORCED_CLOSE",
                                            f"forced @ {exit_px2:.4f} | PnL {pnl2:.2f}",
                                        )
                                    except Exception:
                                        pass
                                    try:
                                        db.close_trade(trade_id, exit_px2, pnl2, "CLOSED_PEV")
                                    except Exception:
                                        try:
                                            db.update_trade_status(trade_id, "CLOSED")
                                        except Exception:
                                            pass
                                    try:
                                        telemetry.log(
                                            "manage",
                                            "CLOSED_PEV_FORCED",
                                            "paper reconcile after PEV exit",
                                            {"tid": trade_id, "exit": exit_px2, "pnl": pnl2},
                                        )
                                    except Exception:
                                        pass
                                    exit_px_used = exit_px2
                        except Exception:
                            pass

                        # Estimate PnL for messaging if we don't have venue fill
                        try:
                            est_pnl = calc_pnl(
                                draft.side, float(entry), float(exit_px_used), float(qty)
                            )
                        except Exception:
                            est_pnl = 0.0

                        # Now inform Telegram after we believe DB is closed
                        try:
                            await tg_send(
                                f"⚪ EXIT — {pair}\nPEV exit pre‑TP1 @ {_fmt(exit_px_used)} \n"
                                f"| PnL {est_pnl:.2f}"
                            )
                        except Exception:
                            pass

                        return

                if pev_state == "WARN" and prev_state != "WARN":
                    telemetry.log(
                        "manage",
                        "PEV_WARN",
                        "pre‑TP1 degrade (grace)",
                        {**pev_diag, "engine": "trendscalp", "tid": trade_id},
                    )
                elif pev_state == "OK" and prev_state == "WARN":
                    telemetry.log(
                        "manage",
                        "PEV_OK",
                        "recovered within grace",
                        {**pev_diag, "engine": "trendscalp", "tid": trade_id},
                    )
        except Exception:
            pass

        # --- Revalidation plan: emit only when it changes ---
        try:
            # --- Risk gating before continuing management ---
            # SL distance as pct from entry (fallback to 0.01 if unknown)
            sl_dist_pct = 0.01
            try:
                sl_dist_pct = abs((sl_cur or 0.0) - entry) / max(1e-9, entry)
            except Exception:
                pass
            ok, size_usd, why_risk = _risk_can_open(sl_dist_pct)
            if not ok:
                try:
                    from app.telemetry import log as _tlog

                    _tlog("mgr", "RISK BLOCKED", why_risk, {"sl_dist_pct": sl_dist_pct})
                except Exception:
                    pass
                # Abort manage loop if risk layer blocks further exposure
                return
            else:
                # Remember suggested size to include in status payload later
                try:
                    suggested_size_usd = float(size_usd)
                except Exception:
                    suggested_size_usd = None
        except Exception:
            suggested_size_usd = None
            pass

        plan = "hold_and_trail"
        reason = "ok"

        if getattr(C, "PEV_ENABLED", True) and not hit_tp1 and ("pev_state" in locals()):
            if pev_state == "WARN":
                plan = "grace_wait"
                reason = "pev_warn"
            elif pev_state == "EXIT" and bool((pev_diag or {}).get("hard")):
                plan = "exit_now"
                reason = "pev_exit"

        if plan == "hold_and_trail" and TS_REGIME_AUTO and regime == "CHOP" and not hit_tp1:
            plan = "close_at_tp1"
            reason = "chop_tp1_policy"

        sig_plan = (
            plan,
            regime,
            bool(hit_tp1),
            (pev_state or "") if ("pev_state" in locals()) else "",
            bool((pev_diag or {}).get("hard")) if ("pev_diag" in locals()) else False,
        )
        if sig_plan != _last_plan_sig:
            payload = {
                "engine": "trendscalp",
                "tid": trade_id,
                "plan": plan,
                "reason": reason,
                "regime": regime,
                "pre_tp1": (not hit_tp1),
            }
            if isinstance(pev_diag, dict) and pev_diag:
                payload.update(pev_diag)
            telemetry.log("manage", "REVALIDATE", "plan update", payload)
            _last_plan_sig = sig_plan

        # Build FSM Context
        ctx = Context(
            price=px,
            side=side,
            entry=entry,
            sl=sl_cur,
            tps=[t for t in [tp1, tp2, tp3] if t is not None],
            tf1m=tf1m,
            meta={
                **(getattr(draft, "meta", {}) or {}),
                **feats,
                "mfe_abs": mfe_abs,
                "hit_tp1": hit_tp1,
                "hit_tp2": hit_tp2,
                "hit_tp3": False,
                "bars_since_tp1": bars_since_tp1,
            },
        )

        # Ask FSM for proposals (pure function, no side-effects)
        try:
            prop = propose(ctx)
        except Exception as e:
            telemetry.log("manage", "FSM_ERROR", str(e), {"engine": "trendscalp", "tid": trade_id})
            prop = None

        # Apply SL (milestone-based if enabled; otherwise FSM proposal). Always tighten-only.
        new_sl_candidate = None

        if MS_MODE:
            fees = float(getattr(C, "FEES_PCT_PAD", 0.0007))
            be_price = (entry * (1.0 + fees)) if is_long else (entry * (1.0 - fees))
            new_sl_candidate = sl_cur  # start from current

            if not hit_tp1:
                # Pre‑TP1: no micro trailing; only optional BE insurance once MFE passes threshold
                abs_lock_usd = float(getattr(C, "SCALP_ABS_LOCK_USD", 0.0))
                if abs_lock_usd > 0.0 and mfe_abs >= abs_lock_usd:
                    _cur = (
                        float(new_sl_candidate) if new_sl_candidate is not None else float(sl_cur)
                    )
                    if is_long:
                        new_sl_candidate = max(_cur, float(be_price))
                    else:
                        new_sl_candidate = min(_cur, float(be_price))
            elif hit_tp1 and not hit_tp2:
                # After TP1: enforce BE and then milestone ratchets every MS_STEP_R beyond TP1
                _cur = float(new_sl_candidate) if new_sl_candidate is not None else float(sl_cur)
                if is_long:
                    new_sl_candidate = max(_cur, float(be_price))
                else:
                    new_sl_candidate = min(_cur, float(be_price))

                if R_init > 0.0 and MS_STEP_R > 0.0:
                    step_px = MS_STEP_R * R_init
                    # tp1 should be non-None when hit_tp1 is True, but be
                    # defensive for type-checkers/safety
                    tp1_val = float(tp1) if tp1 is not None else float(entry)
                    prog = (px - tp1_val) if is_long else (tp1_val - px)
                    _ratio = prog / max(1e-12, step_px)
                    k = int(prog // step_px) if isfinite(_ratio) and prog > 0 else 0
                    if k > last_ms_k:
                        delta_px = k * MS_LOCK_DELTA_R * R_init
                        base = entry + delta_px if is_long else entry - delta_px
                        _cur = (
                            float(new_sl_candidate)
                            if new_sl_candidate is not None
                            else float(sl_cur)
                        )
                        if is_long:
                            new_sl_candidate = max(_cur, float(base))
                        else:
                            new_sl_candidate = min(_cur, float(base))
                        last_ms_k = k
            else:
                # After TP2: jump to an aggressive fraction of entry→TP2, then trail by ATR
                if tp2 is not None and R_init > 0.0:
                    if is_long:
                        base = entry + TS_TP2_LOCK_FRACR * (tp2 - entry)
                    else:
                        base = entry - TS_TP2_LOCK_FRACR * (entry - tp2)
                    _cur = (
                        float(new_sl_candidate) if new_sl_candidate is not None else float(sl_cur)
                    )
                    if is_long:
                        new_sl_candidate = max(_cur, float(base))
                    else:
                        new_sl_candidate = min(_cur, float(base))
                atr5 = float((feats or {}).get("atr5", 0.0))
                if atr5 > 0.0:
                    if is_long:
                        trail = px - TS_POST_TP2_ATR_MULT * atr5
                    else:
                        trail = px + TS_POST_TP2_ATR_MULT * atr5
                    _cur = (
                        float(new_sl_candidate) if new_sl_candidate is not None else float(sl_cur)
                    )
                    if is_long:

                        new_sl_candidate = max(_cur, float(trail))
                    else:
                        new_sl_candidate = min(_cur, float(trail))

        # If milestone mode is off or didn't propose anything better, fall back to FSM proposal
        if new_sl_candidate is None and prop and prop.sl is not None:
            new_sl_candidate = float(prop.sl)

        if new_sl_candidate is not None:
            # Compute guarded SL (polarity-aware, min-gap, freeze pre-TP1, tighten-only)
            atr5 = float((feats or {}).get("atr5", 0.0)) if "feats" in locals() else 0.0
            # do not allow pre‑TP1 BE moves here; milestone logic already handles it
            sl_final = guard_sl(
                sl_candidate=float(new_sl_candidate),
                sl_current=float(sl_cur),
                is_long=bool(is_long),
                price=float(px),
                entry=float(entry),
                atr=atr5,
                hit_tp1=bool(hit_tp1),
                allow_be=False,
            )

            # Only move if improved and cooldown allows
            if sl_final != sl_cur:
                improved = (is_long and sl_final > sl_cur) or ((not is_long) and sl_final < sl_cur)
                if improved:
                    if (now - last_sl_move_ts) >= SL_TIGHTEN_COOLDOWN_SEC:
                        old_sl = sl_cur
                        sl_cur = float(sl_final)
                        await _replace_stop_loss(ex, pair, side, qty, sl_cur, old_sl)
                        last_sl_move_ts = float(now)
                    else:
                        _txt4 = f"guarded SL {_fmt(sl_final)}"
                        telemetry.log(
                            "manage",
                            "SL_COOLDOWN_SKIP",
                            _txt4,
                            {
                                "pair": pair,
                                "engine": "trendscalp",
                                "milestone": MS_MODE,
                                "tid": trade_id,
                            },
                        )

        # Apply TP proposals (extend/respaced)
        if prop and prop.tps:
            new_tps = prop.tps[:3]
            # Only mirror if different (venue churn protection is inside _replace_takeprofits too)
            if new_tps != [t for t in [tp1, tp2, tp3] if t is not None]:
                tp1 = float(new_tps[0]) if len(new_tps) > 0 else tp1
                tp2 = float(new_tps[1]) if len(new_tps) > 1 else tp2
                tp3 = float(new_tps[2]) if len(new_tps) > 2 else tp3
                try:
                    _tp_txt = (
                        f"TPs→ {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)} ({getattr(prop, 'why', '')})"
                    )
                    db.append_event(trade_id, "FLOW_TPS", _tp_txt)
                except Exception:
                    pass
                now_ts2 = time.time()
                if (now_ts2 - last_tp_ext_ts) >= TP_EXTEND_COOLDOWN_SEC:
                    _tp_list_now = [t for t in [tp1, tp2, tp3] if t is not None]
                    await _replace_takeprofits(ex, pair, side, qty, _tp_list_now)
                    last_tp_ext_ts = float(now_ts2)
                else:
                    _txt5 = f"guarded TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
                    telemetry.log(
                        "manage",
                        "TP_COOLDOWN_SKIP",
                        _txt5,
                        {"pair": pair, "engine": "trendscalp", "tid": trade_id},
                    )

        # --- Giveback guard using ML slope (post-entry) ---
        try:
            if R_init > 0.0:
                if is_long:
                    mfe_r = float(max(0.0, (best_hi_seen - entry) / R_init))
                    curr_r = float((px - entry) / R_init)
                else:
                    mfe_r = float(max(0.0, (entry - best_lo_seen) / R_init))
                    curr_r = float((entry - px) / R_init)
                ml_slope_now = 0.0
                try:
                    if isinstance(ml_last, dict):
                        ml_slope_now = float(ml_last.get("slope", 0.0))
                except Exception:
                    ml_slope_now = 0.0
                if giveback_exit(mfe_r, curr_r, ml_slope_now):
                    try:
                        telemetry.log(
                            "manage",
                            "GIVEBACK_FLATTEN",
                            "exit by giveback guard",
                            {
                                "engine": "trendscalp",
                                "tid": trade_id,
                                "mfe_r": mfe_r,
                                "curr_r": curr_r,
                                "ml_slope": ml_slope_now,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        exit_remainder_market(ex, pair, draft, trade_id, qty_hint=qty)
                    except Exception:
                        pass
                    # DRY_RUN reconcile if still open
                    try:
                        if getattr(C, "DRY_RUN", True):
                            ot = db.get_open_trade() if hasattr(db, "get_open_trade") else None
                            still_open = bool(ot and (ot[0] == trade_id))
                            if still_open:
                                exit_px2 = float(locals().get("px", sl_cur))
                                pnl2 = calc_pnl(draft.side, float(entry), exit_px2, float(qty))
                                try:
                                    db.append_event(
                                        trade_id,
                                        "GIVEBACK_FORCED_CLOSE",
                                        f"forced @ {exit_px2:.4f} | PnL {pnl2:.2f}",
                                    )
                                except Exception:
                                    pass
                                try:
                                    db.close_trade(trade_id, exit_px2, pnl2, "CLOSED_PEV")
                                except Exception:
                                    try:
                                        db.update_trade_status(trade_id, "CLOSED")
                                    except Exception:
                                        pass
                                try:
                                    telemetry.log(
                                        "manage",
                                        "GIVEBACK_FORCED",
                                        "paper reconcile after giveback",
                                        {"tid": trade_id, "exit": exit_px2, "pnl": pnl2},
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    # Message and stop managing
                    exit_px_msg = float(locals().get("px", sl_cur))
                    try:
                        est_pnl = calc_pnl(draft.side, float(entry), float(exit_px_msg), float(qty))
                    except Exception:
                        est_pnl = 0.0
                    try:
                        await tg_send(
                            f"⚪ EXIT — {pair}\ngiveback: ML slope down; \n"
                            f"surrender @ {_fmt(exit_px_msg)} | PnL {est_pnl:.2f}"
                        )
                    except Exception:
                        pass
                    return
        except Exception:
            pass

        # --- Debounced STATUS emit (after regime + potential SL/TP changes) ---
        status_payload = {
            "hit_tp1": bool(hit_tp1),
            "hit_tp2": bool(hit_tp2),
            "qty": float(qty),
            "engine": "trendscalp",
            "mfe_px": round(mfe_abs, 4),
            "mae_px": round(mae_abs, 4),
            "regime": regime if TS_REGIME_AUTO else None,
            "sl": round(float(sl_cur), 4) if sl_cur is not None else None,
            "tp1": round(float(tp1), 4) if tp1 is not None else None,
            "tp2": round(float(tp2), 4) if tp2 is not None else None,
            "tp3": round(float(tp3), 4) if tp3 is not None else None,
            "tid": trade_id,
        }
        if "suggested_size_usd" in locals() and suggested_size_usd is not None:
            try:
                status_payload["size_usd"] = float(suggested_size_usd)
            except Exception:
                pass
        if isinstance(ml_last, dict) and ml_last:
            try:
                status_payload["ml_last"] = {
                    "bias": ml_last.get("bias"),
                    "conf": float(ml_last.get("conf", 0.0)),
                    "slope": float(ml_last.get("slope", 0.0)),
                    "warm": bool(ml_last.get("warm", False)),
                }
            except Exception:
                pass
        # Only emit when a *material* field changes; ignore price/MFE/MAE/qty jitter
        sig = (
            status_payload.get("regime"),
            status_payload.get("sl"),
            status_payload.get("tp1"),
            status_payload.get("tp2"),
            status_payload.get("tp3"),
            bool(status_payload.get("hit_tp1")),
            bool(status_payload.get("hit_tp2")),
        )
        if sig != _last_status_sig:
            telemetry.log(
                "manage",
                "STATUS",
                (
                    f"[TRENDSCALP] {side} {pair} price={_fmt(px)} "
                    f"SL={_fmt(sl_cur)} TP1={_fmt(tp1)} TP2={_fmt(tp2)} TP3={_fmt(tp3)}"
                ),
                status_payload,
            )
            _last_status = dict(status_payload)
            _last_status_sig = sig

        # TP hit recognition on the 1m extremes
        if (
            (tp1 is not None)
            and (not hit_tp1)
            and ((hi >= tp1) if is_long else (lo <= tp1))
            and _confirm_tp_hit(
                fetch_ohlcv,
                ex,
                TP_HIT_CONFIRM_BARS,
                is_long,
                tp1,
            )
        ):
            hit_tp1 = True
            bars_since_tp1 = 0  # start grace window
            try:
                db.append_event(trade_id, "TP1_HIT", f"TP1 @ {_fmt(px)}")
                await tg_send(f"🟢 TP1 HIT — {pair}\nPrice {_fmt(px)}")
            except Exception:
                pass
            # One-time TP replace after TP1 (idempotent helper; telemetry inside)
            try:
                if not tp_replaced:
                    after_tp1_replace(draft, entry=float(entry), sl=float(sl_cur))
                    tp_replaced = True
            except Exception:
                pass
            # Regime-based immediate actions at TP1
            try:
                if TS_REGIME_AUTO and regime == "RUNNER":
                    ensure_partial_tp1(
                        ex, pair, draft, trade_id, fraction=TS_PARTIAL_TP1, qty_hint=qty
                    )
                    telemetry.log(
                        "manage",
                        "TP1_PARTIAL_ENSURE",
                        f"runner: ensure {int(TS_PARTIAL_TP1*100)}% partial at TP1",
                        {"engine": "trendscalp", "tid": trade_id},
                    )
                elif TS_REGIME_AUTO and regime == "CHOP":
                    # Exit remainder quickly; do not wait for TP2 in a choppy tape
                    try:
                        telemetry.log(
                            "manage",
                            "TP1_CHOP_EXIT_PENDING",
                            "chop: request market flatten after TP1",
                            {"engine": "trendscalp", "tid": trade_id},
                        )
                    except Exception:
                        pass

                    # Ask execution layer to flatten
                    exit_remainder_market(ex, pair, draft, trade_id, qty_hint=qty)

                    # In DRY_RUN, reconcile DB state to be certain it is closed before messaging
                    try:
                        if getattr(C, "DRY_RUN", True):
                            ot = db.get_open_trade() if hasattr(db, "get_open_trade") else None
                            still_open = bool(ot and (ot[0] == trade_id))
                            if still_open:
                                exit_px2 = float(locals().get("px", sl_cur))
                                pnl2 = calc_pnl(draft.side, float(entry), exit_px2, float(qty))
                                try:
                                    db.append_event(
                                        trade_id,
                                        "TP1_CHOP_FORCED_CLOSE",
                                        f"forced @ {exit_px2:.4f} | PnL {pnl2:.2f}",
                                    )
                                except Exception:
                                    pass
                                try:
                                    db.close_trade(trade_id, exit_px2, pnl2, "CLOSED_PEV")
                                except Exception:
                                    try:
                                        db.update_trade_status(trade_id, "CLOSED")
                                    except Exception:
                                        pass
                                try:
                                    telemetry.log(
                                        "manage",
                                        "TP1_CHOP_FORCED",
                                        "paper reconcile after TP1 chop exit",
                                        {"tid": trade_id, "exit": exit_px2, "pnl": pnl2},
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    # Compute exit price and pnl for messaging (best-effort)
                    exit_px_msg = float(locals().get("px", sl_cur))
                    try:
                        est_pnl = calc_pnl(draft.side, float(entry), float(exit_px_msg), float(qty))
                    except Exception:
                        est_pnl = 0.0

                    # Confirmed event + TG only after above reconcile
                    try:
                        telemetry.log(
                            "manage",
                            "TP1_CHOP_EXIT_CONFIRMED",
                            "chop: flattened remainder at market",
                            {
                                "engine": "trendscalp",
                                "tid": trade_id,
                                "exit": exit_px_msg,
                                "pnl": est_pnl,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        await tg_send(
                            f"⚪ EXIT — {pair}\nchop regime: flatten after TP1 \n"
                            f"@ {_fmt(exit_px_msg)} | PnL {est_pnl:.2f}"
                        )
                    except Exception:
                        pass
                    return
            except Exception:
                pass

        if (
            (tp2 is not None)
            and hit_tp1
            and (not hit_tp2)
            and ((hi >= tp2) if is_long else (lo <= tp2))
            and _confirm_tp_hit(
                fetch_ohlcv,
                ex,
                TP_HIT_CONFIRM_BARS,
                is_long,
                tp2,
            )
        ):
            hit_tp2 = True
            try:
                db.append_event(trade_id, "TP2_HIT", f"TP2 @ {_fmt(px)}")
                await tg_send(f"🟢 TP2 HIT — {pair}\nPrice {_fmt(px)}")
            except Exception:
                pass

        # If we had a RUNNER phase but are now in CHOP before TP2, flatten remainder
        try:
            if TS_REGIME_AUTO and (not hit_tp2) and had_runner_since_entry and regime == "CHOP":
                # Only act if we had been RUNNER earlier in the life of this trade
                # Heuristic: bars_since_tp1 > 0 implies we advanced beyond entry and now stalled
                if hit_tp1:
                    try:
                        telemetry.log(
                            "manage",
                            "FLIP_RUNNER_TO_CHOP_EXIT_PENDING",
                            "flip before TP2: request flatten",
                            {"engine": "trendscalp", "tid": trade_id},
                        )
                    except Exception:
                        pass

                    exit_remainder_market(ex, pair, draft, trade_id, qty_hint=qty)

                    # DRY_RUN reconcile if still open
                    try:
                        if getattr(C, "DRY_RUN", True):
                            ot = db.get_open_trade() if hasattr(db, "get_open_trade") else None
                            still_open = bool(ot and (ot[0] == trade_id))
                            if still_open:
                                exit_px2 = float(locals().get("px", sl_cur))
                                pnl2 = calc_pnl(draft.side, float(entry), exit_px2, float(qty))
                                try:
                                    db.append_event(
                                        trade_id,
                                        "FLIP_FORCED_CLOSE",
                                        f"forced @ {exit_px2:.4f} | PnL {pnl2:.2f}",
                                    )
                                except Exception:
                                    pass
                                try:
                                    db.close_trade(trade_id, exit_px2, pnl2, "CLOSED_PEV")
                                except Exception:
                                    try:
                                        db.update_trade_status(trade_id, "CLOSED")
                                    except Exception:
                                        pass
                                try:
                                    telemetry.log(
                                        "manage",
                                        "FLIP_FORCED",
                                        "paper reconcile after runner→chop exit",
                                        {"tid": trade_id, "exit": exit_px2, "pnl": pnl2},
                                    )
                                except Exception:
                                    pass
                    except Exception:
                        pass

                    exit_px_msg = float(locals().get("px", sl_cur))
                    try:
                        est_pnl = calc_pnl(draft.side, float(entry), float(exit_px_msg), float(qty))
                    except Exception:
                        est_pnl = 0.0

                    try:
                        telemetry.log(
                            "manage",
                            "FLIP_RUNNER_TO_CHOP_EXIT_CONFIRMED",
                            "flip before TP2: flattened remainder",
                            {
                                "engine": "trendscalp",
                                "tid": trade_id,
                                "exit": exit_px_msg,
                                "pnl": est_pnl,
                            },
                        )
                    except Exception:
                        pass
                    try:
                        await tg_send(
                            f"⚪ EXIT — {pair}\nregime flip: runner -> chop before TP2 @ \n"
                            f"{_fmt(exit_px_msg)} | PnL {est_pnl:.2f}"
                        )
                    except Exception:
                        pass
                    return
        except Exception:
            pass


# TODO_LEDGER_OPEN: call _ledger_open(trade_id, ts_ms, symbol, side, entry, sl, size_usd, meta)

# TODO_LEDGER_CLOSE: call _ledger_close(trade_id, ts_ms, exit_px, pnl_usd, reason, meta)
