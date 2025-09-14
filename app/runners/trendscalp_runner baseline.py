# app/runners/trendscalp_runner.py
from __future__ import annotations
import asyncio, time, ccxt
from typing import Callable, Optional, List, Dict, Any

from app import config as C, db, telemetry
from app.money import calc_pnl
from app.messenger import tg_send
from app.managers.trendscalp_fsm import Context, propose

# Re-use TASER helpers so messages / payloads remain identical
from app.surveillance import (
    _replace_stop_loss, _replace_takeprofits, _confirm_sl_breach,
    _tg_send_throttled, _fmt
)

async def run_trendscalp_manage(
    ex: ccxt.Exchange,
    pair: str,
    draft,              # object with .side .entry .sl .tps .meta (like TASER draft)
    trade_id: int,
    qty: float,
    fetch_ohlcv: Callable,   # fetch_ohlcv(ex, tf, n) -> {"timestamp": [...], "open": [...], "high":[...],"low":[...],"close":[...]}
    indicators: Callable,    # indicators(tf5, tf15, tf1h) -> {"atr5": float, "adx14": float, ...}
) -> None:
    """
    Manage ONE TrendScalp position using the FSM proposals and TASER's venue/TG helpers.
    Keeps telemetry.csv and Telegram text consistent with TASER.
    """
    side = draft.side.upper()
    is_long = (side == "LONG")
    entry = float(draft.entry)
    sl_cur = float(draft.sl)
    tp_list: List[float] = list(draft.tps or [])
    tp1 = float(tp_list[0]) if len(tp_list) >= 1 else None
    tp2 = float(tp_list[1]) if len(tp_list) >= 2 else None
    tp3 = float(tp_list[2]) if len(tp_list) >= 3 else None

    # State we maintain across ticks
    hit_tp1 = False
    hit_tp2 = False
    last_status_ts = 0

    # Excursions
    best_hi_seen = entry
    best_lo_seen = entry
    mfe_abs = 0.0
    mae_abs = 0.0

    # Cooldowns (re-use TASER environment knobs)
    SL_TIGHTEN_COOLDOWN_SEC = int(getattr(C, "SL_TIGHTEN_COOLDOWN_SEC", 55))
    TP_EXTEND_COOLDOWN_SEC  = int(getattr(C, "TP_EXTEND_COOLDOWN_SEC", 55))
    last_sl_move_ts = 0
    last_tp_ext_ts = 0

    # Init bookkeeping
    try:
        db.update_trade_status(trade_id, "OPEN")
        db.append_event(
            trade_id,
            "MANAGE_START",
            f"[TRENDSCALP] {side} {pair} @ {_fmt(entry)} SL {_fmt(sl_cur)} TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}",
        )
        await tg_send(
            f"[MANAGE][TRENDSCALP] {side} — {pair}\n"
            f"Entry {_fmt(entry)} | SL {_fmt(sl_cur)} | TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
        )
    except Exception:
        pass

    while True:
        await asyncio.sleep(C.MANAGE_POLL_SECONDS)

        # Pull latest 1m candle for status and crossings
        tf1m = fetch_ohlcv(ex, "1m", 2)
        if not tf1m.get("high") or not tf1m.get("low"):
            telemetry.log("surveil", "NO_1M", "empty 1m; continue", {"engine": "trendscalp"})
            continue
        hi = float(tf1m["high"][-1]); lo = float(tf1m["low"][-1]); px = float(tf1m["close"][-1])

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
                "manage", "STATUS",
                f"[TRENDSCALP] {side} {pair} price={_fmt(px)} SL={_fmt(sl_cur)} TP1={_fmt(tp1)} TP2={_fmt(tp2)} TP3={_fmt(tp3)}",
                {"hit_tp1": hit_tp1, "hit_tp2": hit_tp2, "qty": qty, "engine": "trendscalp",
                 "mfe_px": round(mfe_abs, 4), "mae_px": round(mae_abs, 4)}
            )
            last_status_ts = now

        # Optional: SL breach confirm (same as TASER behavior)
        sl_touch = (lo <= sl_cur) if is_long else (hi >= sl_cur)
        if sl_touch:
            need_sl_conf = int(getattr(C, "SL_CLOSE_CONFIRM_BARS", 0))
            if need_sl_conf > 0 and not _confirm_sl_breach(fetch_ohlcv, ex, need_sl_conf, is_long, sl_cur):
                telemetry.log("manage", "SL_TOUCH_WAIT_CONFIRM", f"touch at {_fmt(sl_cur)}; wait {need_sl_conf} closes", {"pair": pair, "engine":"trendscalp"})
            else:
                exit_px = sl_cur
                pnl = calc_pnl(draft.side, entry, exit_px, qty)
                try:
                    db.append_event(trade_id, "SL_HIT", f"Exit @ {_fmt(exit_px)}")
                    db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL")
                    telemetry.log("exec", "CLOSED", "SL", {"exit": exit_px, "pnl": pnl, "engine": "trendscalp"})
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
            },
        )

        # Ask FSM for proposals (pure function, no side-effects)
        try:
            prop = propose(ctx)
        except Exception as e:
            telemetry.log("manage", "FSM_ERROR", str(e), {"engine": "trendscalp"})
            prop = None

        # Apply SL proposal (tighten-only logic is already enforced inside FSM)
        if prop and prop.sl is not None:
            new_sl = float(prop.sl)
            if (is_long and new_sl > sl_cur) or ((not is_long) and new_sl < sl_cur):
                if (now - last_sl_move_ts) >= SL_TIGHTEN_COOLDOWN_SEC:
                    old_sl = sl_cur
                    sl_cur = new_sl
                    await _replace_stop_loss(ex, pair, side, qty, sl_cur, old_sl)
                    last_sl_move_ts = now
                else:
                    telemetry.log("manage", "SL_COOLDOWN_SKIP", f"guarded SL {_fmt(new_sl)}", {"pair": pair, "engine": "trendscalp"})

        # Apply TP proposals (extend/respaced)
        if prop and prop.tps:
            new_tps = prop.tps[:3]
            # Only mirror if different (venue churn protection is inside _replace_takeprofits too)
            if new_tps != [t for t in [tp1, tp2, tp3] if t is not None]:
                tp1 = float(new_tps[0]) if len(new_tps) > 0 else tp1
                tp2 = float(new_tps[1]) if len(new_tps) > 1 else tp2
                tp3 = float(new_tps[2]) if len(new_tps) > 2 else tp3
                try:
                    db.append_event(trade_id, "FLOW_TPS", f"TPs→ {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)} ({getattr(prop, 'why', '')})")
                except Exception:
                    pass
                await _replace_takeprofits(ex, pair, side, qty, [t for t in [tp1, tp2, tp3] if t is not None])

        # TP hit recognition on the 1m extremes
        if (tp1 is not None) and (not hit_tp1) and ((hi >= tp1) if is_long else (lo <= tp1)):
            hit_tp1 = True
            try:
                db.append_event(trade_id, "TP1_HIT", f"TP1 @ {_fmt(px)}")
                await tg_send(f"🟢 TP1 HIT — {pair}\nPrice {_fmt(px)}")
            except Exception:
                pass

        if (tp2 is not None) and hit_tp1 and (not hit_tp2) and ((hi >= tp2) if is_long else (lo <= tp2)):
            hit_tp2 = True
            try:
                db.append_event(trade_id, "TP2_HIT", f"TP2 @ {_fmt(px)}")
                await tg_send(f"🟢 TP2 HIT — {pair}\nPrice {_fmt(px)}")
            except Exception:
                pass