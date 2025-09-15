# app/runners/trendscalp_runner.py
from __future__ import annotations

import asyncio
import time
from math import isfinite
from typing import Callable

import ccxt

from app import config as C
from app import db, telemetry
from app.components.guards import guard_sl
from app.indicators import rsi_compact
from app.managers.trendscalp_fsm import Context, propose
from app.messenger import tg_send
from app.money import calc_pnl

# Re-use TASER helpers so messages / payloads remain identical
from app.surveillance import (
    _confirm_sl_breach,
    _fmt,
    _replace_stop_loss,
    _replace_takeprofits,
)


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
    last_status_ts = 0

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
    last_sl_move_ts = 0
    last_tp_ext_ts = 0

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
            {"engine": "trendscalp"},
        )
    except Exception:
        pass

    while True:
        await asyncio.sleep(C.MANAGE_POLL_SECONDS)

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
            telemetry.log("surveil", "NO_1M", "empty 1m; continue", {"engine": "trendscalp"})
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

        # Periodic status in telemetry.csv
        now = int(time.time())
        if now - last_status_ts >= max(5, C.STATUS_INTERVAL_SECONDS):
            telemetry.log(
                "manage",
                "STATUS",
                (
                    f"[TRENDSCALP] {side} {pair} price={_fmt(px)} "
                    f"SL={_fmt(sl_cur)} TP1={_fmt(tp1)} "
                    f"TP2={_fmt(tp2)} TP3={_fmt(tp3)}"
                ),
                {
                    "hit_tp1": hit_tp1,
                    "hit_tp2": hit_tp2,
                    "qty": qty,
                    "engine": "trendscalp",
                    "mfe_px": round(mfe_abs, 4),
                    "mae_px": round(mae_abs, 4),
                },
            )
            last_status_ts = now

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
                    {"pair": pair, "engine": "trendscalp"},
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
                        {"exit": exit_px, "pnl": pnl, "engine": "trendscalp"},
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
            telemetry.log("manage", "INDICATORS_ERROR", str(e), {"engine": "trendscalp"})
            feats = {}

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
            telemetry.log("manage", "FSM_ERROR", str(e), {"engine": "trendscalp"})
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
                    if is_long:
                        new_sl_candidate = max(new_sl_candidate, be_price)
                    else:
                        new_sl_candidate = min(new_sl_candidate, be_price)
            elif hit_tp1 and not hit_tp2:
                # After TP1: enforce BE and then milestone ratchets every MS_STEP_R beyond TP1
                if is_long:
                    new_sl_candidate = max(new_sl_candidate, be_price)
                else:
                    new_sl_candidate = min(new_sl_candidate, be_price)

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
                        if is_long:
                            new_sl_candidate = max(new_sl_candidate, base)
                        else:
                            new_sl_candidate = min(new_sl_candidate, base)
                        last_ms_k = k
            else:
                # After TP2: jump to an aggressive fraction of entry→TP2, then trail by ATR
                if tp2 is not None and R_init > 0.0:
                    if is_long:
                        base = entry + TS_TP2_LOCK_FRACR * (tp2 - entry)
                    else:
                        base = entry - TS_TP2_LOCK_FRACR * (entry - tp2)
                    if is_long:
                        new_sl_candidate = max(new_sl_candidate, base)
                    else:
                        new_sl_candidate = min(new_sl_candidate, base)
                atr5 = float((feats or {}).get("atr5", 0.0))
                if atr5 > 0.0:
                    if is_long:
                        trail = px - TS_POST_TP2_ATR_MULT * atr5
                    else:
                        trail = px + TS_POST_TP2_ATR_MULT * atr5
                    if is_long:
                        new_sl_candidate = max(new_sl_candidate, trail)
                    else:
                        new_sl_candidate = min(new_sl_candidate, trail)

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
                        last_sl_move_ts = now
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
                if (now - last_tp_ext_ts) >= TP_EXTEND_COOLDOWN_SEC:
                    _tp_list_now = [t for t in [tp1, tp2, tp3] if t is not None]
                    await _replace_takeprofits(ex, pair, side, qty, _tp_list_now)
                    last_tp_ext_ts = now
                else:
                    _txt5 = f"guarded TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
                    telemetry.log(
                        "manage",
                        "TP_COOLDOWN_SKIP",
                        _txt5,
                        {"pair": pair, "engine": "trendscalp"},
                    )

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
