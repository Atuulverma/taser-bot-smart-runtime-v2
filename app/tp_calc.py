from typing import List, Dict, Any

# Import minimal helpers to avoid circular deps
from .taser_rules import _order_tps, _enforce_min_r, _tp_guard


def _bool(v, default=False):
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "y", "on"): return True
    if s in ("0", "false", "no", "n", "off"): return False
    return bool(default)


def _floats_csv(val, default: str) -> List[float]:
    """Parse floats from CSV or JSON-like list strings into up to 3 floats."""
    if isinstance(val, (list, tuple)):
        out = []
        for x in val:
            try: out.append(float(x))
            except Exception: pass
        return out[:3] if out else _floats_csv(default, default)
    s = str(val).strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    parts = [p.strip() for p in s.split(",") if p.strip()]
    out: List[float] = []
    for p in parts:
        try: out.append(float(p))
        except Exception: pass
    if out:
        return out[:3]
    if default is None:
        return []
    return _floats_csv(default, default) if isinstance(default, str) else [float(default)]


def _normalize_fracs(fracs: List[float]) -> List[float]:
    """Clamp negatives to 0, cap to 1 each, and renormalize to sum=1 if sum>0; else fallback to [0.3,0.3,0.4]."""
    safe = [max(0.0, float(x)) for x in fracs[:3]]
    s = sum(safe)
    if s <= 0.0:
        return [0.3, 0.3, 0.4]
    return [x / s for x in safe]

def _fractions_for_mode(price: float, atr_ref: float, adx_last: float, C) -> List[float]:
    """
    Decide TP size fractions (TP1, TP2, TP3) based on mode (chop vs rally).
    - If MODE_ADAPT_ENABLED is True and ATR% & ADX indicate chop → take more at TP1/2.
    - If rally → leave more for TP3 to ride.
    - If adapt is off → use TP_FRACTIONS or defaults.
    """
    adapt_on = _bool(getattr(C, "MODE_ADAPT_ENABLED", False))
    if not adapt_on:
        # global default or env-provided
        base = _floats_csv(getattr(C, "TP_FRACTIONS", "0.30,0.30,0.40"), "0.30,0.30,0.40")
        return _normalize_fracs(base)

    # Adapt by ATR% and ADX
    try:
        atr_pct = float(atr_ref) / max(1e-9, float(price))
    except Exception:
        atr_pct = 0.0
    chop_atr_max = float(getattr(C, "MODE_CHOP_ATR_PCT_MAX", 0.0025))
    chop_adx_max = float(getattr(C, "MODE_CHOP_ADX_MAX", 25.0))

    if (atr_pct <= chop_atr_max) and (float(adx_last) <= chop_adx_max):
        # Chop: take profits earlier
        fr = _floats_csv(getattr(C, "MODE_CHOP_TP_FRACS", "0.50,0.30,0.20"), "0.50,0.30,0.20")
    else:
        # Rally: keep more for the tail
        fr = _floats_csv(getattr(C, "MODE_RALLY_TP_FRACS", "0.30,0.30,0.40"), "0.30,0.30,0.40")
    return _normalize_fracs(fr)


def compute_tps(
    price: float,
    sl: float,
    side: str,
    atr_ref: float,
    adx_last: float,
    C,
) -> List[float]:
    """
    Unified TP generator for TrendScalp.

    - TP_MODE == 'r'  → TS_TP_R multipliers on R = |price - sl|
    - TP_MODE == 'atr'→ ATR multipliers; if MODE_ADAPT_ENABLED, auto-pick chop/rally set
      using ATR% of price and ADX thresholds.
    - Always orders and guards via _enforce_min_r / _tp_guard.
    - If TS_TP_STRUCTURED is True, returns a list of dicts:
      [{"px": <float>, "size_frac": <float in 0..1>}, ...], otherwise returns legacy [float, float, float].
    """
    side = str(side).upper()
    tp_mode = str(getattr(C, "TP_MODE", "r")).strip().lower()

    # --- pick raw ladder ---
    if tp_mode == "atr":
        # ATR% classifier
        try:
            atr_pct = float(atr_ref) / max(1e-9, float(price))
        except Exception:
            atr_pct = 0.0
        adapt_on = _bool(getattr(C, "MODE_ADAPT_ENABLED", False))
        if adapt_on:
            chop_atr_max = float(getattr(C, "MODE_CHOP_ATR_PCT_MAX", 0.0025))
            chop_adx_max = float(getattr(C, "MODE_CHOP_ADX_MAX", 25.0))
            if (atr_pct <= chop_atr_max) and (float(adx_last) <= chop_adx_max):
                mults = _floats_csv(getattr(C, "MODE_CHOP_TP_ATR_MULTS", "0.60,1.00,1.50"), "0.60,1.00,1.50")
            else:
                mults = _floats_csv(getattr(C, "MODE_RALLY_TP_ATR_MULTS", "0.90,1.60,2.60"), "0.90,1.60,2.60")
        else:
            m1 = float(getattr(C, "TP1_ATR_MULT", 0.60))
            m2 = float(getattr(C, "TP2_ATR_MULT", 1.10))
            m3 = float(getattr(C, "TP3_ATR_MULT", 1.80))
            mults = [m1, m2, m3]
        raw = []
        for m in mults[:3]:
            d = float(m) * float(atr_ref)
            raw.append((price + d) if side == "LONG" else (price - d))
    else:
        # Legacy R-based ladder
        rmults = _floats_csv(getattr(C, "TS_TP_R", "0.8,1.4,2.2"), "0.8,1.4,2.2")
        R = max(1e-9, abs(float(price) - float(sl)))
        raw = [(price + m * R) if side == "LONG" else (price - m * R) for m in rmults[:3]]

    # --- normalize and guard ---
    tps = _order_tps(side, raw)
    tps = _enforce_min_r(price, sl, tps, side, float(atr_ref))
    tps = _tp_guard(side, float(price), float(sl), tps, float(atr_ref))

    final_tps = [float(round(x, 4)) for x in tps]

    # Optional structured return with size fractions (backward compatible by flag)
    if _bool(getattr(C, "TS_TP_STRUCTURED", False), False):
        fracs = _fractions_for_mode(price, atr_ref, adx_last, C)
        # align length to TPs we actually have
        if len(fracs) > len(final_tps):
            fracs = fracs[:len(final_tps)]
        elif len(fracs) < len(final_tps):
            # pad remaining evenly if fewer fracs provided
            rem = max(0, len(final_tps) - len(fracs))
            pad = [0.0] * rem
            fracs = fracs + pad
            # normalize again so sum==1 when possible
            fracs = _normalize_fracs(fracs) if sum(fracs) > 0 else [1.0/len(final_tps)] * len(final_tps)
        structured = [{"px": final_tps[i], "size_frac": float(fracs[i])} for i in range(len(final_tps))]
        return structured

    return final_tps