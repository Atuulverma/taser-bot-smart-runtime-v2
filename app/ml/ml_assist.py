# app/ml/ml_assist.py — lightweight, safe ML assist for TrendScalp
from __future__ import annotations

from typing import Any

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
