from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

import app.telemetry as telemetry
from app import config as C


@dataclass(frozen=True)
class MLSig:
    bias: str  # "long" | "short" | "neutral"
    conf: float  # 0..1
    slope: float  # conf delta since last call
    warm: bool


_prev_conf: float | None = None


def get_ml_signal(features_5m: Dict[str, List[float]]) -> MLSig:
    """
    Adapter over your Lorentzian library. This function is the *only* ML entrypoint
    called by entry, PEV, manage, reverse. Keep it tiny and deterministic.
    """
    try:
        closes = features_5m.get("close", [])
        bars = len(closes)
    except Exception:
        closes = []
        bars = 0
    thr = int(getattr(C, "TS_ML_WARMUP_BARS", 600))
    print(f"[ML_GATE] warmup check: bars={bars} thr={thr}")

    predictor: Optional[Callable[[Dict[str, List[float]]], Tuple[str, float]]] = None
    try:
        from app.trendscalp_ml_gate import predict_bias_conf as _predict_bias_conf

        predictor = _predict_bias_conf
        print(f"[ML_GATE] using predictor=predict_bias_conf warm={bars >= thr}")
    except Exception as e:
        print(f"[ML_GATE] predictor import exception: {e}")
        predictor = None

    warm = bars >= thr

    bias, conf = ("neutral", 0.0)
    if (predictor is not None) and warm:
        try:
            bias, conf = predictor(features_5m)
        except Exception as e:
            import traceback

            print(f"[ML_GATE] predictor exception: {e}\n{traceback.format_exc()}")
            bias, conf = ("neutral", 0.0)

    global _prev_conf
    slope = 0.0 if _prev_conf is None else (conf - _prev_conf)
    _prev_conf = conf
    telemetry.log(
        "gate",
        "ml",
        "gate check",
        {
            "message": (
                f"[ML_GATE] warm={warm} bias={bias} conf={conf:.4f} "
                f"slope={slope:.4f} bars={bars}"
            ),
            "warm": bool(warm),
            "bias": str(bias),
            "conf": float(conf),
            "slope": float(slope),
            "bars": int(bars),
        },
    )
    print(f"[ML_GATE] warm={warm} bias={bias} conf={conf:.4f} slope={slope:.4f} \n" f"bars={bars}")
    return MLSig(bias=bias, conf=conf, slope=slope, warm=warm)
