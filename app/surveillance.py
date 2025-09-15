# app/surveillance.py (TASER-only)
import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import ccxt

from . import config as C
from . import db, telemetry
from .messenger import tg_send
from .money import calc_pnl
from .taser_rules import manage_with_flow, prior_day_high_low, taser_signal

# Module-level throttling state (replaces function attributes for mypy compatibility)
_TG_GATE: Dict[Tuple[Any, ...], float] = {}
_SL_LAST: Dict[Tuple[str, str], Tuple[float, float]] = {}
_TP_LAST: Dict[Tuple[str, str], Tuple[Tuple[float, ...], float]] = {}


# =====================
# Small utils
# =====================
def _fmt(x: Optional[float]) -> str:
    try:
        if x is None:
            return ""
        xf = float(x)
        if xf != xf:  # NaN
            return ""
        return f"{xf:.4f}"
    except Exception:
        return ""


# --- Throttled Telegram sender (per-key, per-trade)
async def _tg_send_throttled(
    key: tuple,
    text: str,
    *,
    min_interval: float = 10.0,
    silent: bool = False,
):
    """Send a TG message at most once per `min_interval` seconds for the given key."""
    gate = _TG_GATE
    now_ts = time.time()
    last_ts = gate.get(key, 0)
    if (now_ts - last_ts) < float(min_interval):
        return
    gate[key] = now_ts
    try:
        # If silent delivery is ever needed, extend messenger.tg_send signature.
        await tg_send(text)
    except Exception:
        pass


def _atr(highs: List[float], lows: List[float], n: int = 30) -> float:
    k = min(n, len(highs))
    if k <= 0:
        return 0.0
    tr = [float(highs[i]) - float(lows[i]) for i in range(-k, 0)]
    return (sum(tr) / len(tr)) if tr else 0.0


# --- SL gap & BE lock helpers
def _min_sl_gap(px: float, entry: float, atr: float) -> float:
    try:
        g_atr = float(getattr(C, "SL_MIN_GAP_ATR_MULT", 0.35)) * float(atr or 0.0)
    except Exception:
        g_atr = 0.0
    try:
        g_pct = float(getattr(C, "SL_MIN_GAP_PCT", 0.0012)) * float(px or entry or 1.0)
    except Exception:
        g_pct = 0.0
    return max(1e-6, max(g_atr, g_pct))


def _apply_be_floor(sl_new: float, is_long: bool, entry: float, hit_tp1: bool) -> float:
    if not hit_tp1:
        return float(sl_new)
    if not bool(getattr(C, "LOCK_NEVER_WORSE_THAN_BE", True)):
        return float(sl_new)
    fees_pad = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    be = float(entry) * (1.0 + fees_pad) if is_long else float(entry) * (1.0 - fees_pad)
    return max(float(sl_new), be) if is_long else min(float(sl_new), be)


def _apply_abs_lock(
    sl_new: float,
    is_long: bool,
    entry: float,
    px: float,
    mfe_abs: float,
    abs_lock_usd: float,
) -> float:
    try:
        lock = float(abs_lock_usd or 0.0)
    except Exception:
        lock = 0.0
    if lock <= 0.0:
        return float(sl_new)
    try:
        if float(mfe_abs) < lock:
            return float(sl_new)
    except Exception:
        return float(sl_new)
    fees_pad = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    fee_px = abs(float(entry)) * fees_pad
    floor = (float(entry) + lock + fee_px) if is_long else (float(entry) - lock - fee_px)
    if is_long:
        return float(min(max(sl_new, floor), float(px) - 1e-6))
    else:
        return float(max(min(sl_new, floor), float(px) + 1e-6))


def _guard_sl(
    sl_target: float,
    is_long: bool,
    px: float,
    entry: float,
    atr: float,
    hit_tp1: bool,
) -> float:
    guarded = _apply_be_floor(float(sl_target), is_long, float(entry), bool(hit_tp1))
    gap = _min_sl_gap(float(px), float(entry), float(atr or 0.0))
    if is_long:
        guarded = min(guarded, float(px) - gap)
    else:
        guarded = max(guarded, float(px) + gap)
    return float(round(guarded, 4))


# --- SL close confirmation helper (optional)
def _confirm_sl_breach(
    fetch_ohlcv: Callable,
    ex: ccxt.Exchange,
    need_bars: int,
    is_long: bool,
    sl_level: float,
) -> bool:
    try:
        if need_bars <= 0:
            return True
        tf = fetch_ohlcv(ex, "1m", max(need_bars, 2))
        closes = tf.get("close") or []
        if len(closes) < need_bars:
            return False
        lastN = [float(c) for c in closes[-need_bars:]]
        return all(c <= sl_level for c in lastN) if is_long else all(c >= sl_level for c in lastN)
    except Exception:
        return True


# =====================
# Order helpers (idempotent, throttled)
# =====================
async def _replace_stop_loss(
    ex: ccxt.Exchange,
    pair: str,
    side: str,
    qty: float,
    new_sl: float,
    old_sl: Optional[float] = None,
):
    _last_sl = _SL_LAST
    key = (pair.upper(), side.upper())
    now_ts = time.time()
    SL_EPS = float(getattr(C, "SL_EPS", 0.0003))
    SL_MIN_INTERVAL = float(getattr(C, "SL_MIN_INTERVAL_S", 20.0))
    prev = _last_sl.get(key)  # (prev_sl, ts)
    if prev is not None:
        prev_sl, prev_ts = prev
        if abs(float(new_sl) - float(prev_sl)) <= SL_EPS:
            return
        if (now_ts - prev_ts) < SL_MIN_INTERVAL and abs(float(new_sl) - float(prev_sl)) <= (
            2.0 * SL_EPS
        ):
            return

    if C.DRY_RUN:
        telemetry.log(
            "manage",
            "SL_REPLACED_DRY",
            f"SL {_fmt(old_sl)}→{_fmt(new_sl)}",
            {"pair": pair},
        )
        try:
            await _tg_send_throttled(
                ("SL_DRY", pair, side),
                (f"🧪 DRY-RUN: SL would update — {pair}\nSL {_fmt(old_sl)}→{_fmt(new_sl)}"),
            )
        except Exception:
            pass
        _SL_LAST[key] = (float(new_sl), now_ts)
        return
    try:
        try:
            open_orders = ex.fetch_open_orders(pair) or []
        except Exception:
            open_orders = []
        for o in open_orders:
            typ = (o.get("type") or "").lower()
            info = o.get("info") or {}
            if "stop" in typ or "stop" in str(info).lower():
                try:
                    ex.cancel_order(o.get("id", ""), pair)
                except Exception:
                    pass
        params_sl = {"reduceOnly": True, "stopLossPrice": float(new_sl)}
        ex.create_order(
            pair,
            type="stop",
            side=("sell" if side.upper() == "LONG" else "buy"),
            amount=qty,
            price=None,
            params=params_sl,
        )
        telemetry.log("manage", "SL_REPLACED", f"SL {_fmt(old_sl)}→{_fmt(new_sl)}", {"pair": pair})
        try:
            await _tg_send_throttled(
                ("SL", pair, side),
                (f"🔧 SL UPDATED — {pair}\nSL {_fmt(old_sl)}→{_fmt(new_sl)}"),
            )
        except Exception:
            pass
        _SL_LAST[key] = (float(new_sl), now_ts)
    except Exception as e:
        telemetry.log("manage", "SL_REPLACE_ERROR", str(e), {"pair": pair, "new_sl": new_sl})


async def _replace_takeprofits(
    ex: ccxt.Exchange,
    pair: str,
    side: str,
    qty: float,
    new_tps: List[float],
):
    if not getattr(C, "FLOW_REPLACE_TPS", False):
        return
    _last = _TP_LAST
    key = (pair.upper(), side.upper())
    now_ts = time.time()
    EPS = float(getattr(C, "TP_EPS", 0.0003))
    MIN_INTERVAL = float(getattr(C, "TP_MIN_INTERVAL_S", 30.0))
    tpl = tuple(round(float(x), 4) for x in (new_tps or []))
    prev = _last.get(key)
    if prev:
        prev_tpl, prev_ts = prev
        same_len = len(prev_tpl) == len(tpl)
        same_vals = same_len and all(abs(a - b) <= EPS for a, b in zip(prev_tpl, tpl))
        if same_vals:
            return
        if (now_ts - prev_ts) < MIN_INTERVAL:
            return
    _TP_LAST[key] = (tpl, now_ts)

    if C.DRY_RUN:
        telemetry.log(
            "manage",
            "TPS_REPLACED_DRY",
            f"TPs→ {','.join([_fmt(x) for x in new_tps])}",
            {"pair": pair},
        )
        try:
            await _tg_send_throttled(
                ("TP_DRY", pair, side),
                (
                    f"🧪 DRY-RUN: TPs would update — {pair}\n"
                    f"→ {', '.join([_fmt(x) for x in new_tps])}"
                ),
            )
        except Exception:
            pass
        return

    # Sanity: qty must be positive and TP list must be valid floats
    try:
        if qty is None or float(qty) <= 0.0:
            telemetry.log(
                "manage", "TPS_SKIP_QTY", "qty<=0; skip replace", {"pair": pair, "qty": qty}
            )
            return
    except Exception:
        telemetry.log(
            "manage", "TPS_SKIP_QTY", "qty parse error; skip replace", {"pair": pair, "qty": qty}
        )
        return

    # Normalize new_tps to a clean float list (drop Nones/NaNs/non-numeric)
    clean_tps: List[float] = []
    for t in new_tps or []:
        try:
            ft = float(t)
            # NaN check: ft != ft iff NaN
            if ft == ft:
                clean_tps.append(ft)
        except Exception:
            continue
    new_tps = clean_tps
    try:
        try:
            open_orders = ex.fetch_open_orders(pair) or []
        except Exception:
            open_orders = []
        for o in open_orders:
            typ = (o.get("type") or "").lower()
            info = o.get("info") or {}
            if ("stop" not in typ) and (str(info).lower().find("reduceonly") != -1):
                try:
                    ex.cancel_order(o.get("id", ""), pair)
                except Exception:
                    pass
        n = len(new_tps)
        if n <= 0:
            return
        per = max(0.0, float(qty)) / float(n)
        if per <= 0.0:
            telemetry.log(
                "manage",
                "TPS_SKIP_QTY_PER",
                "computed per<=0; skip replace",
                {"pair": pair, "qty": qty, "n": n},
            )
            return

        for tp in new_tps:
            # For TP placement, use a plain reduce-only LIMIT order at the target price.
            # Avoid exchange-specific keys like 'takeProfitPrice' to
            # keep CCXT-unified usage consistent.
            params_tp = {"reduceOnly": True}
            try:
                ex.create_order(
                    pair,
                    type="limit",
                    side=("sell" if side.upper() == "LONG" else "buy"),
                    amount=per,
                    price=float(tp),
                    params=params_tp,
                )
            except Exception as e:
                telemetry.log(
                    "manage", "TP_PLACE_ERROR", str(e), {"pair": pair, "tp": tp, "amount": per}
                )
                continue
        telemetry.log(
            "manage",
            "TPS_REPLACED",
            f"TPs→ {','.join([_fmt(x) for x in new_tps])}",
            {"pair": pair},
        )
        try:
            await _tg_send_throttled(
                ("TP", pair, side),
                (f"🎯 TPs UPDATED — {pair}\n→ {', '.join([_fmt(x) for x in new_tps])}"),
            )
        except Exception:
            pass
    except Exception as e:
        telemetry.log("manage", "TPS_REPLACE_ERROR", str(e), {"pair": pair, "tps": new_tps})


# =====================
# Main TASER manage loop (single position)
# =====================
async def surveil_loop(
    ex: ccxt.Exchange,
    pair: str,
    draft,
    trade_id: int,
    qty: float,
    fetch_ohlcv: Callable,
    cvd_ref: Callable,
):
    """Manage ONE TASER trade until fully closed with volatility-aware trailing."""
    side = draft.side.upper()
    is_long = side == "LONG"
    entry = float(draft.entry)
    sl_cur = float(draft.sl)
    tp_list: List[float] = list(draft.tps or [])

    tp1 = float(tp_list[0]) if len(tp_list) >= 1 else None
    tp2 = float(tp_list[1]) if len(tp_list) >= 2 else None
    tp3 = float(tp_list[2]) if len(tp_list) >= 3 else None

    # Break-even sentinel line (fees padded)
    fees_pad = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    be_line = entry * (1.0 + fees_pad) if is_long else entry * (1.0 - fees_pad)

    hit_tp1: bool = False
    hit_tp2: bool = False
    last_status_ts = 0

    # Excursions
    best_hi_seen = entry
    best_lo_seen = entry
    mfe_abs = 0.0
    mae_abs = 0.0

    # Controls
    SL_TIGHTEN_COOLDOWN_SEC = int(getattr(C, "SL_TIGHTEN_COOLDOWN_SEC", 55))
    last_sl_move_ts = 0

    # Init
    try:
        db.update_trade_status(trade_id, "OPEN")
        db.append_event(
            trade_id,
            "MANAGE_START",
            (
                f"[TASER] {side} {pair} @ {_fmt(entry)} "
                f"SL {_fmt(sl_cur)} TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
            ),
        )
        await tg_send(
            (
                f"[MANAGE][TASER] {side} — {pair}\n"
                f"Entry {_fmt(entry)} | SL {_fmt(sl_cur)} | "
                f"TPs {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)}"
            )
        )
    except Exception:
        pass

    while True:
        await asyncio.sleep(C.MANAGE_POLL_SECONDS)

        # Latest 1m candle
        tf1m = fetch_ohlcv(ex, "1m", 2)
        if not tf1m.get("high") or not tf1m.get("low"):
            telemetry.log("surveil", "NO_1M", "empty 1m; continue", {})
            continue
        hi = float(tf1m["high"][-1])
        lo = float(tf1m["low"][-1])
        px = float(tf1m["close"][-1])

        # Exit early if position is flat (no qty left)
        try:
            if qty <= 1e-8:
                db.append_event(trade_id, "CLOSED_FLAT", "Exit — qty 0")
                db.close_trade(trade_id, px, 0.0, "CLOSED_FLAT")
                telemetry.log("exec", "CLOSED", "FLAT", {"exit": px, "pnl": 0.0})
                await tg_send(f"⚪ EXIT — {pair} qty flat")
                return
        except Exception:
            pass

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

        # Status
        now = int(time.time())
        if now - last_status_ts >= max(5, C.STATUS_INTERVAL_SECONDS):
            telemetry.log(
                "manage",
                "STATUS",
                (
                    f"[TASER] {side} {pair} "
                    f"price={_fmt(px)} SL={_fmt(sl_cur)} "
                    f"TP1={_fmt(tp1)} TP2={_fmt(tp2)} TP3={_fmt(tp3)}"
                ),
                {
                    "hit_tp1": hit_tp1,
                    "hit_tp2": hit_tp2,
                    "qty": qty,
                    "engine": "taser",
                    "mfe_px": round(mfe_abs, 4),
                    "mae_px": round(mae_abs, 4),
                },
            )
            last_status_ts = now

        # Hard SL touch check with optional confirmation
        sl_touch = (lo <= sl_cur) if is_long else (hi >= sl_cur)
        if sl_touch:
            need_sl_conf = int(getattr(C, "SL_CLOSE_CONFIRM_BARS", 0))
            if need_sl_conf > 0 and not _confirm_sl_breach(
                fetch_ohlcv, ex, need_sl_conf, is_long, sl_cur
            ):
                telemetry.log(
                    "manage",
                    "SL_TOUCH_WAIT_CONFIRM",
                    f"touch at {_fmt(sl_cur)}; wait {need_sl_conf} closes",
                    {"pair": pair},
                )
            else:
                exit_px = sl_cur
                pnl = calc_pnl(draft.side, entry, exit_px, qty)
                try:
                    db.append_event(trade_id, "SL_HIT", f"Exit @ {_fmt(exit_px)}")
                    db.close_trade(trade_id, exit_px, pnl, "CLOSED_SL")
                    telemetry.log("exec", "CLOSED", "SL", {"exit": exit_px, "pnl": pnl})
                    await tg_send((f"🔴 SL HIT — {pair}\nExit {_fmt(exit_px)} | PnL {pnl:.2f}"))
                except Exception:
                    pass
                return

        # Re-scan higher TFs and ATR; run TASER flow manager
        try:
            tf5 = fetch_ohlcv(ex, "5m", 220)
            tf15 = fetch_ohlcv(ex, "15m", 220)
            tf1h = fetch_ohlcv(ex, "1h", 200)
            atr = 0.0
            try:
                atr = (
                    _atr(tf5["high"], tf5["low"], 30) if tf5.get("high") and tf5.get("low") else 0.0
                )
            except Exception:
                atr = 0.0
            if tf5.get("timestamp"):
                now_ts = tf5["timestamp"][-1]
                pdh, pdl = (
                    prior_day_high_low(tf1h, now_ts) if tf1h.get("timestamp") else (None, None)
                )
                # Pass boolean flags inline to avoid any chance of reassigning typed locals.
                recheck = taser_signal(
                    px,
                    tf5,
                    tf15,
                    tf1h,
                    pdh,
                    pdl,
                    True,  # oi_up_flag: assume on (no venue OI here)
                    bool(cvd_ref() > 0),  # delta_pos_flag
                )
            else:
                recheck = draft
        except Exception as e:
            telemetry.log("manage", "RECHECK_ERROR", str(e), {})
            recheck = draft
            atr = float(getattr(draft, "meta", {}).get("atr") or 0.0)

        # Flow-aware adjustments (tighten-only SL, TP respacing)
        try:
            adj = manage_with_flow(
                price=px,
                side=side,
                entry=entry,
                sl=sl_cur,
                tps=[t for t in [tp1, tp2, tp3] if t is not None],
                meta=(getattr(recheck, "meta", {}) or {}),
                tf1m=tf1m,
            )
            # SL tighten-only with BE/min-gap guards and optional abs lock
            new_sl = float(adj.get("sl", sl_cur))
            try:
                _abs_lock_usd = float(
                    getattr(
                        C,
                        "TASER_ABS_LOCK_USD",
                        getattr(C, "SCALP_ABS_LOCK_USD", 0.0),
                    )
                )
            except Exception:
                _abs_lock_usd = 0.0
            new_sl = _apply_abs_lock(new_sl, is_long, entry, px, mfe_abs, _abs_lock_usd)
            if (is_long and new_sl > sl_cur) or ((not is_long) and new_sl < sl_cur):
                guarded = _guard_sl(new_sl, is_long, px, entry, atr, hit_tp1)
                if (is_long and guarded > sl_cur) or ((not is_long) and guarded < sl_cur):
                    if now - last_sl_move_ts >= SL_TIGHTEN_COOLDOWN_SEC:
                        old_sl = sl_cur
                        sl_cur = guarded
                        await _replace_stop_loss(ex, pair, side, qty, sl_cur, old_sl)
                        if (is_long and sl_cur >= be_line) or ((not is_long) and sl_cur <= be_line):
                            try:
                                db.append_event(
                                    trade_id,
                                    "BE_LOCKED",
                                    f"SL→{_fmt(sl_cur)} (≥ {_fmt(be_line)})",
                                )
                            except Exception:
                                pass
                        last_sl_move_ts = now
                    else:
                        telemetry.log(
                            "manage",
                            "SL_COOLDOWN_SKIP",
                            f"guarded SL {_fmt(guarded)}",
                            {"pair": pair},
                        )

            # TP updates (if any) — keep at most remaining, preserve order, mirror to venue
            new_tps = adj.get("tps", [])
            if new_tps and new_tps != [t for t in [tp1, tp2, tp3] if t is not None]:
                _old_tp1, _old_tp2, _old_tp3 = tp1, tp2, tp3
                pad = new_tps[:3]
                tp1 = float(pad[0]) if len(pad) >= 1 else tp1
                tp2 = float(pad[1]) if len(pad) >= 2 else tp2
                tp3 = float(pad[2]) if len(pad) >= 3 else tp3
                # Ensure monotonic order TP2/TP3
                if tp2 is not None and tp3 is not None:
                    if is_long and tp2 >= tp3:
                        tp3 = round(tp2 + abs(tp3 - tp2), 4)
                    elif (not is_long) and tp2 <= tp3:
                        tp3 = round(tp2 - abs(tp3 - tp2), 4)
                try:
                    db.append_event(
                        trade_id,
                        "FLOW_TPS",
                        (f"TPs→ {_fmt(tp1)},{_fmt(tp2)},{_fmt(tp3)} ({adj.get('why', '')})"),
                    )
                except Exception:
                    pass
                try:
                    await _replace_takeprofits(
                        ex,
                        pair,
                        side,
                        qty,
                        [t for t in [tp1, tp2, tp3] if t is not None],
                    )
                except Exception:
                    pass
        except Exception as e:
            telemetry.log("manage", "FLOW_MANAGER_ERROR", str(e), {})

        # TP hit recognition (simple cross on extremes)
        if (tp1 is not None) and (not hit_tp1) and ((hi >= tp1) if is_long else (lo <= tp1)):
            hit_tp1 = True
            try:
                db.append_event(trade_id, "TP1_HIT", f"TP1 @ {_fmt(px)}")
                await tg_send((f"🟢 TP1 HIT — {pair}\nPrice {_fmt(px)}"))
            except Exception:
                pass
            # After TP1, immediately apply BE floor if configured
            guarded = _apply_be_floor(sl_cur, is_long, entry, True)
            try:
                if (is_long and guarded > sl_cur) or ((not is_long) and guarded < sl_cur):
                    old_sl = sl_cur
                    sl_cur = guarded
                    await _replace_stop_loss(ex, pair, side, qty, sl_cur, old_sl)
            except Exception:
                pass

        if (
            (tp2 is not None)
            and hit_tp1
            and (not hit_tp2)
            and ((hi >= tp2) if is_long else (lo <= tp2))
        ):
            hit_tp2 = True
            try:
                db.append_event(trade_id, "TP2_HIT", f"TP2 @ {_fmt(px)}")
                await tg_send((f"🟢 TP2 HIT — {pair}\nPrice {_fmt(px)}"))
            except Exception:
                pass
