"""
ML gate for TrendScalp (Lorentzian KNN placeholder, library-ready).
This module is intentionally lightweight and mypy/ruff/black friendly.
All behavior is OFF unless TS_USE_ML_GATE=true in config/.env.
"""

from __future__ import annotations

import traceback
from typing import Any, Dict, List, Optional, Tuple
from typing import Dict as _Dict
from typing import List as _List
from typing import Tuple as _Tuple

from app import telemetry

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
    # --- DEBUG: input feature lengths for ML core ---
    try:
        c = tf5.get("close", []) if isinstance(tf5, dict) else []
        h = tf5.get("high", []) if isinstance(tf5, dict) else []
        low = tf5.get("low", []) if isinstance(tf5, dict) else []
        print(f"[ML_CORE] inputs: close={len(c)} high={len(h)} low={len(low)}")
    except Exception as e:
        print(f"[ML_CORE] inputs: unavailable ({e})\n{traceback.format_exc()}")
        c, h, low = [], [], []

    use_ml = bool(getattr(C, "TS_USE_ML_GATE", False))
    if not use_ml:
        try:
            n = len(c)
            telemetry.log(
                component="ml",
                tag="ML_CORE",
                message="bars/bias/conf",
                payload={"bars": n, "bias": "neutral", "conf": 0.0, "regime": None},
            )
            print(f"[ML_CORE] bars={n} bias=neutral conf=0.0000 regime=None")
        except Exception:
            pass
        return "neutral", 0.0, None

    closes = list(map(_safe_float, (tf5.get("close") or [])))
    if len(closes) < int(getattr(C, "TS_ML_WARMUP_BARS", 600)):
        # Not enough data to trust ML yet
        try:
            n = len(c)
            telemetry.log(
                component="ml",
                tag="ML_CORE",
                message="bars/bias/conf",
                payload={"bars": n, "bias": "neutral", "conf": 0.0, "regime": None},
            )
            print(f"[ML_CORE] bars={n} bias=neutral conf=0.0000 regime=None")
        except Exception:
            pass
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

    try:
        n = len(c)
        telemetry.log(
            component="ml",
            tag="ML_CORE",
            message="bars/bias/conf",
            payload={"bars": n, "bias": bias, "conf": float(conf), "regime": regime_ml},
        )
        print(f"[ML_CORE] bars={n} bias={bias} conf={float(conf):.4f} regime={regime_ml}")
        if (bias in ("long", "short")) and (float(conf) == 0.0):
            telemetry.log(
                component="ml",
                tag="ML_CORE_WARN",
                message="directional bias with zero confidence",
                payload={},
            )
            print("[ML_CORE] WARNING: directional bias with zero confidence")
    except Exception as e:
        print(f"[ML_CORE] log error: {e}\n{traceback.format_exc()}")

    return bias, conf, regime_ml


# --- Compatibility wrapper expected by app/ml/gate.py ---


def predict_bias_conf(tf5: _Dict[str, _List[float]]) -> _Tuple[Bias, float]:
    """Adapter returning (bias, conf) by delegating to infer_bias_conf.
    Keeps gate callers simple and mypy-clean.
    """
    bias, conf, _ = infer_bias_conf(tf5)
    return bias, float(conf)
