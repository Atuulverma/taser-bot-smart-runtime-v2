# app/components/tp.py
from __future__ import annotations
from typing import Optional
from .. import config as C

def ensure_order(tp1: Optional[float], tp2: Optional[float], tp3: Optional[float], is_long: bool):
    arr = [x for x in [tp1, tp2, tp3] if x is not None]
    if not arr:
        return tp1, tp2, tp3
    arr = sorted(arr) if is_long else sorted(arr, reverse=True)
    out = []
    last = None
    for x in arr:
        x = float(round(x, 4))
        if last is None or (is_long and x > last) or ((not is_long) and x < last):
            out.append(x); last = x
        if len(out) == 3:
            break
    while len(out) < 3:
        out.append(None)
    return out[0], out[1], out[2]

def clamp_tp1_distance(entry: float, sl: float, tp1: Optional[float], tp2: Optional[float], tp3: Optional[float], is_long: bool, atr5: float):
    """
    Compute/Clamp TP ladder so TP1 is realistic (ATR‑based) and never pushed unreasonably far.
    - Preserves signature and return types.
    - Uses ATR ladder by default via ENV:
        TP_MODE=atr (default), TP1_ATR_MULT=0.60, TP2_ATR_MULT=1.00, TP3_ATR_MULT=1.50
    - Falls back to modest R‑based seed if ATR unavailable.
    - Ensures extend‑only semantics at init/restart by capping TP1 to the seed distance (pre‑TP1 effect).
    """
    try:
        entry_f = float(entry); sl_f = float(sl)
    except Exception:
        return tp1, tp2, tp3

    # ---------- Config & seeds ----------
    mode = str(getattr(C, "TP_MODE", "atr")).lower()
    atr5_f = float(atr5 or 0.0)

    # Defaults for ATR ladder
    tp1_mult = float(getattr(C, "TP1_ATR_MULT", 0.60))
    tp2_mult = float(getattr(C, "TP2_ATR_MULT", 1.00))
    tp3_mult = float(getattr(C, "TP3_ATR_MULT", 1.50))

    # Conservative R fallback if ATR missing
    try:
        R = abs(entry_f - sl_f)
    except Exception:
        R = 0.0

    # Build seed ladder
    if mode == "atr" and atr5_f > 0.0:
        d1 = tp1_mult * atr5_f
        d2 = tp2_mult * atr5_f
        d3 = tp3_mult * atr5_f
    else:
        # Fallback: modest R distances (kept small to avoid pushing TP1 too far)
        d1 = max(0.10, 0.40 * R)
        d2 = max(d1 + 0.10, 0.90 * R)
        d3 = max(d2 + 0.10, 1.40 * R)

    if is_long:
        seed_tp1 = entry_f + d1
        seed_tp2 = entry_f + d2
        seed_tp3 = entry_f + d3
    else:
        seed_tp1 = entry_f - d1
        seed_tp2 = entry_f - d2
        seed_tp3 = entry_f - d3

    # ---------- Clamp TP1 to avoid over‑far targets pre‑confirm ----------
    # If a caller supplies tp1, we DO NOT let it be further than seed on first clamp.
    # (Extend‑only behavior post‑TP1 should be handled by the manager; here we keep init sane.)
    tp1_eff = tp1
    if tp1_eff is None:
        tp1_eff = seed_tp1
    else:
        try:
            t1 = float(tp1_eff)
            if is_long:
                # do not push further than seed
                tp1_eff = min(t1, seed_tp1)
            else:
                tp1_eff = max(t1, seed_tp1)
        except Exception:
            tp1_eff = seed_tp1

    # ---------- Derive TP2/TP3 with order and spacing ----------
    # If provided and in correct order relative to tp1, keep them; else regenerate from seeds.
    if is_long:
        t2 = (float(tp2) if tp2 is not None else seed_tp2)
        t3 = (float(tp3) if tp3 is not None else seed_tp3)
        if t2 <= float(tp1_eff):
            t2 = max(seed_tp2, float(tp1_eff) + max(0.01, 0.10 * d1))
        if t3 <= float(t2):
            t3 = max(seed_tp3, float(t2) + max(0.01, 0.10 * d1))
    else:
        t2 = (float(tp2) if tp2 is not None else seed_tp2)
        t3 = (float(tp3) if tp3 is not None else seed_tp3)
        if t2 >= float(tp1_eff):
            t2 = min(seed_tp2, float(tp1_eff) - max(0.01, 0.10 * d1))
        if t3 >= float(t2):
            t3 = min(seed_tp3, float(t2) - max(0.01, 0.10 * d1))

    # Round and ensure order
    out_tp1 = round(float(tp1_eff), 4)
    out_tp2 = round(float(t2), 4) if t2 is not None else None
    out_tp3 = round(float(t3), 4) if t3 is not None else None
    out_tp1, out_tp2, out_tp3 = ensure_order(out_tp1, out_tp2, out_tp3, is_long)
    return out_tp1, out_tp2, out_tp3    