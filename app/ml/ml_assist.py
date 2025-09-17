# app/ml/ml_assist.py — lightweight, safe ML assist for TrendScalp
from __future__ import annotations

from typing import Any, Iterable, Sequence

try:
    import joblib

    _HAVE_SK = True
except Exception:
    _HAVE_SK = False


_model = None


def _load_model():
    global _model
    if _model is not None:
        return _model
    if not _HAVE_SK:
        _model = None
        return None
    try:
        import os

        here = os.path.dirname(__file__)
        _model = joblib.load(os.path.join(here, "models", "tp1_model.pkl"))
    except Exception:
        _model = None
    return _model


def score_tp1_probability(**features: Any) -> float:
    """Return calibrated probability of hitting TP1 within the horizon.

    Falls back to a neutral probability (0.55) if the model is missing or an error occurs.
    """
    m = _load_model()
    if m is None:
        return 0.55
    xs = [
        float(features.get("price", 0.0)),
        float(features.get("entry", 0.0)),
        float(features.get("sl", 0.0)),
        float(features.get("tp1", 0.0) or 0.0),
        float((features.get("meta", {}) or {}).get("atr5", 0.0)),
        float((features.get("meta", {}) or {}).get("adx14", 0.0)),
    ]
    try:
        p = float(m.predict_proba([xs])[0][1])
        return max(0.05, min(0.95, p))
    except Exception:
        return 0.55


# === Lightweight, reusable helpers (no new deps) ===================


def ema_aligned(price: float, ema: float | None, side: str, tol_pct: float = 0.0015) -> bool:
    """Return True if price is on the correct side of EMA200 with a small tolerance band.
    side: "LONG" or "SHORT" (case-insensitive). If ema is None, return True (non-blocking).
    tol_pct default 0.15% keeps us from flip-flopping in chop around EMA200.
    """
    try:
        if ema is None:
            return True
        s = (side or "").upper()
        tol = float(tol_pct)
        if s == "LONG":
            return (price >= ema) or (abs(price - ema) / max(1e-9, ema) <= tol)
        elif s == "SHORT":
            return (price <= ema) or (abs(price - ema) / max(1e-9, ema) <= tol)
        return True
    except Exception:
        return True


def adx_slope(series: Sequence[float] | Iterable[float], bars: int = 3) -> float:
    """Short-horizon ADX slope: last - last-bars. Returns 0.0 on error/short series."""
    try:
        seq = list(series)
        if not seq or len(seq) <= bars:
            return 0.0
        return float(seq[-1] - seq[-1 - bars])
    except Exception:
        return 0.0


def effective_adx_min(adx_last: float, base_min: float, slope: float, bonus: float = 2.0) -> float:
    """Apply a small slope bonus to the ADX minimum when slope>0 (momentum rebuilding)."""
    try:
        return float(base_min - (bonus if float(slope) > 0.0 else 0.0))
    except Exception:
        return float(base_min)


def coalesce_series(meta: dict | None, feats: dict | None, key: str) -> list[float]:
    """Pull a numeric series from meta first, then feats. Returns [] if missing."""
    try:
        m = meta or {}
        f = feats or {}
        return list(m.get(key) or f.get(key) or [])
    except Exception:
        return []
