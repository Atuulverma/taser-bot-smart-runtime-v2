# app/components/ratchet.py
from __future__ import annotations

# Placeholder for future extraction; not used directly by the FSM baseline yet.

def behind_extreme(cur_sl: float, is_long: bool, recent_hi: float, recent_lo: float, atr: float, wall_pad_mult: float | None = None) -> float:
    pad = 0.5 * float(atr or 0.0)
    if wall_pad_mult is not None:
        pad = max(0.25 * float(atr or 0.0), pad * float(wall_pad_mult))
    cand = float(recent_lo) + pad if is_long else float(recent_hi) - pad
    return max(cur_sl, cand) if is_long else min(cur_sl, cand)