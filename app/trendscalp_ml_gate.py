"""
ML gate for TrendScalp (Lorentzian KNN placeholder, library-ready).
This module is intentionally lightweight and mypy/ruff/black friendly.
All behavior is OFF unless TS_USE_ML_GATE=true in config/.env.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from typing import Dict as _Dict
from typing import List as _List
from typing import Tuple as _Tuple

try:
    import app.config as C
except Exception:  # pragma: no cover
    import importlib as _importlib

    C = _importlib.import_module("config")

Bias = str  # "long" | "short" | "neutral"


def _safe_float(x: Any, default: float = 0.0) -> float:
    try:
        return float(x)
    except Exception:
        return default


def _dummy_confidence(closes: List[float]) -> float:
    # Simple, deterministic placeholder: higher conf if recent slope exists.
    if not closes or len(closes) < 8:
        return 0.0
    slope = closes[-1] - closes[-8]
    mag = abs(slope) / max(
        1e-9, sum(abs(closes[i] - closes[i - 1]) for i in range(1, len(closes))) / len(closes)
    )
    return max(0.0, min(0.99, mag))


def infer_bias_conf(
    tf5: Dict[str, List[float]],
    features: Optional[Dict[str, List[float]]] = None,
    symbol: Optional[str] = None,
) -> Tuple[Bias, float, Optional[str]]:
    """
    Return (bias, confidence, regime_ml) where bias in {"long","short","neutral"}.
    If no trained artifacts are found or flag disabled, returns neutral with 0 conf.
    """
    use_ml = bool(getattr(C, "TS_USE_ML_GATE", False))
    if not use_ml:
        return "neutral", 0.0, None

    closes = list(map(_safe_float, (tf5.get("close") or [])))
    if len(closes) < int(getattr(C, "TS_ML_WARMUP_BARS", 600)):
        # Not enough data to trust ML yet
        return "neutral", 0.0, None

    # Library hook: load model per symbol if available (placeholder)
    # In production, load from .ml/<SYMBOL>/model.pkl via app.ml.store
    conf = _dummy_confidence(closes)
    thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
    if conf < thr:
        return "neutral", conf, None

    # Sign by short-term slope as placeholder (library will replace)
    bias = "long" if closes[-1] >= closes[-4] else "short"

    # Optional simple regime proxy from conf for now
    regime_ml = "RUNNER" if conf >= max(0.75, thr + 0.15) else "CHOP"

    return bias, conf, regime_ml


# --- Compatibility wrapper expected by app/ml/gate.py ---


def predict_bias_conf(tf5: _Dict[str, _List[float]]) -> _Tuple[Bias, float]:
    """Adapter returning (bias, conf) by delegating to infer_bias_conf.
    Keeps gate callers simple and mypy-clean.
    """
    bias, conf, _ = infer_bias_conf(tf5)
    return bias, float(conf)
