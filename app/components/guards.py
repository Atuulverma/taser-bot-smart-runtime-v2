# app/components/guards.py
from __future__ import annotations
from .. import config as C

def be_floor(sl_new: float, is_long: bool, entry: float) -> float:
    fees = float(getattr(C, "FEES_PCT_PAD", 0.0007))
    be = float(entry) * (1.0 + fees) if is_long else float(entry) * (1.0 - fees)
    return max(float(sl_new), be) if is_long else min(float(sl_new), be)

def guard_min_gap(sl: float, is_long: bool, price: float, entry: float, atr: float) -> float:
    try:
        g_atr = float(getattr(C, "SL_MIN_GAP_ATR_MULT", 0.35)) * float(atr or 0.0)
    except Exception:
        g_atr = 0.0
    try:
        g_pct = float(getattr(C, "SL_MIN_GAP_PCT", 0.0012)) * float(price or entry or 1.0)
    except Exception:
        g_pct = 0.0
    gap = max(1e-6, max(g_atr, g_pct))
    return min(sl, float(price) - gap) if is_long else max(sl, float(price) + gap)


# --- TrendScalp-safe SL guard (polarity-aware, min-gap, freeze, tighten-only)

def _def_true(v) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "on"}


def _min_gap_px(price: float, entry: float, atr: float) -> float:
    try:
        g_atr = float(getattr(C, "SL_MIN_GAP_ATR_MULT", 0.35)) * float(atr or 0.0)
    except Exception:
        g_atr = 0.0
    try:
        g_buf = float(getattr(C, "TS_SL_MIN_BUFFER_ATR", 0.20)) * float(atr or 0.0)
    except Exception:
        g_buf = 0.0
    try:
        g_pct = float(getattr(C, "SL_MIN_GAP_PCT", 0.0012)) * float(price or entry or 1.0)
    except Exception:
        g_pct = 0.0
    return max(1e-6, g_pct, g_atr, g_buf)


def guard_sl(sl_candidate: float,
             sl_current: float,
             is_long: bool,
             price: float,
             entry: float,
             atr: float,
             *,
             hit_tp1: bool = False,
             allow_be: bool = False) -> float:
    """
    Unified SL guard for TrendScalp (safe for generic use as well):
      - Respects GLOBAL_NO_TRAIL_BEFORE_TP1 / TRENDSCALP_PAUSE_ABS_LOCKS (freeze before TP1)
      - Optional BE allowance (only floor to BE, still clamped to min-gap and tighten-only)
      - Polarity-safe clamp relative to current price with min-gap (ATR/%/buffer)
      - Tighten-only: never loosens the stop
    Returns the final stop-loss price (float).
    """
    try:
        freeze_all = _def_true(getattr(C, "GLOBAL_NO_TRAIL_BEFORE_TP1", True)) or _def_true(getattr(C, "TRENDSCALP_PAUSE_ABS_LOCKS", False))
    except Exception:
        freeze_all = True

    # If pre‑TP1 freeze is on, return current SL unless BE explicitly allowed
    if (not hit_tp1) and freeze_all and (not allow_be):
        return float(sl_current)

    # Compute min gap
    mg = _min_gap_px(float(price), float(entry), float(atr or 0.0))

    # Optional BE floor
    sl_target = float(sl_candidate)
    if allow_be:
        sl_target = be_floor(sl_target, is_long, float(entry))

    # Polarity clamp around current price by min-gap
    if is_long:
        sl_clamped = min(sl_target, float(price) - mg)
        # tighten-only for longs (stop can only go up)
        sl_final = max(float(sl_current), sl_clamped)
    else:
        sl_clamped = max(sl_target, float(price) + mg)
        # tighten-only for shorts (stop can only go down)
        sl_final = min(float(sl_current), sl_clamped)

    return float(sl_final)