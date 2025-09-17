# app/engines/trendscalp/regime.py
from typing import Dict, List, Optional, Tuple


def classify(
    adx_series: List[float],
    atr_series: List[float],
    closes: List[float],
    ema200_last: float,
    prev: Optional[str],
    *,
    adx_up: float,
    adx_dn: float,
    atr_up: float,
    atr_dn: float,
) -> Tuple[str, Dict[str, float]]:
    """
    Classify the 5m market regime as 'RUNNER' or 'CHOP' using hysteresis.
    Inputs:
      - adx_series: ADX(14) history
      - atr_series: ATR(14) history (absolute price units)
      - closes: close prices
      - ema200_last: last EMA200 value (5m)
      - prev: previous regime if known ('RUNNER'|'CHOP'|None)
      - thresholds:
          adx_up: upgrade threshold (e.g., 26.0)
          adx_dn: downgrade threshold (e.g., 23.0)
          atr_up: upgrade threshold as fraction of price (e.g., 0.0040 == 0.40%)
          atr_dn: downgrade threshold as fraction of price (e.g., 0.0035 == 0.35%)
    Returns:
      regime: 'RUNNER' or 'CHOP'
      dbg:    diagnostics for telemetry
    """
    if not adx_series or not atr_series or not closes:
        return (prev or "CHOP"), {"adx": 0.0, "atr_pct": 0.0, "ema_side": 0.0, "ema_slope": 0.0}

    adx = float(adx_series[-1])
    price = float(closes[-1])
    atr = float(atr_series[-1])
    atr_pct = atr / max(1e-9, price)

    # EMA200 side and a simple close-slope proxy (keeps this module dependency-free)
    ema_side = 1.0 if price >= float(ema200_last) else -1.0
    close_slope = 0.0
    if len(closes) >= 2:
        close_slope = 1.0 if closes[-1] > closes[-2] else -1.0

    # Hysteresis decisions
    want_runner = (adx >= adx_up) and (atr_pct >= atr_up) and (ema_side * close_slope >= 0.0)
    want_chop = (adx <= adx_dn) or (atr_pct <= atr_dn)

    if prev == "RUNNER":
        regime = "CHOP" if want_chop else "RUNNER"
    elif prev == "CHOP":
        regime = "RUNNER" if want_runner else "CHOP"
    else:
        regime = "RUNNER" if want_runner else ("CHOP" if want_chop else "CHOP")

    dbg = {
        "adx": round(adx, 3),
        "atr_pct": round(atr_pct, 6),
        "ema_side": float(ema_side),
        "ema_slope": float(close_slope),
        "adx_up": float(adx_up),
        "adx_dn": float(adx_dn),
        "atr_up": float(atr_up),
        "atr_dn": float(atr_dn),
    }
    return regime, dbg


# --- PEV-support helpers (dependency-light) ---------------------------------


def adx_slope(adx_series: List[float], bars: int = 3) -> float:
    """Return short-horizon ADX slope (last - last-bars). If not enough data, 0.0."""
    try:
        if not adx_series or len(adx_series) <= bars:
            return 0.0
        return float(adx_series[-1] - adx_series[-1 - bars])
    except Exception:
        return 0.0


def soft_degrade(
    adx_series: List[float],
    atr_series: List[float],
    closes: List[float],
    *,
    adx_min: float,
    atr_floor_pct: float,
    slope_bonus: float = 2.0,
) -> Dict[str, float | int | bool]:
    """
    Soft invalidation assessment (no EMA/structure here to avoid deps).
    Returns diag with keys: soft(bool), adx(float),
    atr_pct(float), adx_min_eff(float), slope3(float)
    Logic:
      - Compute effective ADX min with a small slope bonus if ADX rising over ~3 bars.
      - Mark soft=True if ADX < adx_min_eff OR ATR% < atr_floor_pct.
    """
    if not adx_series or not atr_series or not closes:
        return {
            "soft": True,
            "adx": 0.0,
            "atr_pct": 0.0,
            "adx_min_eff": float(adx_min),
            "slope3": 0.0,
        }

    adx_last = float(adx_series[-1])
    price = float(closes[-1])
    atr = float(atr_series[-1])
    atr_pct = atr / max(1e-9, price)

    s3 = adx_slope(adx_series, 3)
    adx_min_eff = float(adx_min - (slope_bonus if s3 > 0.0 else 0.0))

    soft = (adx_last < adx_min_eff) or (atr_pct < float(atr_floor_pct))

    return {
        "soft": bool(soft),
        "adx": round(adx_last, 3),
        "atr_pct": round(atr_pct, 6),
        "adx_min_eff": round(adx_min_eff, 3),
        "slope3": round(s3, 3),
    }
