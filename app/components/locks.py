# app/components/locks.py
from __future__ import annotations

from .. import config as C


def abs_lock_from_entry(
    cur_sl: float, is_long: bool, entry: float, price: float, mfe_abs: float, abs_lock_usd: float
) -> float:
    try:
        lock = float(abs_lock_usd or 0.0)
    except Exception:
        lock = 0.0
    if lock <= 0.0:
        return float(cur_sl)
    try:
        if float(mfe_abs) < lock:
            return float(cur_sl)
    except Exception:
        return float(cur_sl)
    fees = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    floor = float(entry) * (1.0 + fees) + lock if is_long else float(entry) * (1.0 - fees) - lock
    if is_long:
        return float(min(max(cur_sl, floor), float(price) - 1e-6))
    else:
        return float(max(min(cur_sl, floor), float(price) + 1e-6))


def to_tp_lock(cur_sl: float, is_long: bool, tp: float, atr_mult: float, atr: float) -> float:
    buf = float(atr_mult) * float(atr or 0.0)
    target = (tp - buf) if is_long else (tp + buf)
    return max(cur_sl, target) if is_long else min(cur_sl, target)


def trail_fracR(
    cur_sl: float, is_long: bool, entry: float, tp: float, *, frac: float, atr_pad: float
) -> float:
    base = (
        float(entry) + float(frac) * (float(tp) - float(entry))
        if is_long
        else float(entry) - float(frac) * (float(entry) - float(tp))
    )
    target = base - float(atr_pad or 0.0) if is_long else base + float(atr_pad or 0.0)
    return max(cur_sl, target) if is_long else min(cur_sl, target)
