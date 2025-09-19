from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

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
    from app.trendscalp_ml_gate import predict_bias_conf  # use your existing lib wrapper

    warm = len(features_5m.get("close", [])) >= int(getattr(C, "TS_ML_WARMUP_BARS", 600))
    if not warm:
        return MLSig("neutral", 0.0, 0.0, False)

    bias, conf = predict_bias_conf(features_5m)  # must return ("long"/"short"/"neutral", float)
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
                f"slope={slope:.4f} bars={len(features_5m.get('close', []))}"
            ),
            "warm": bool(warm),
            "bias": str(bias),
            "conf": float(conf),
            "slope": float(slope),
            "bars": int(len(features_5m.get("close", []))),
        },
    )
    print(
        f"[ML_GATE] warm={warm} bias={bias} conf={conf:.4f} slope={slope:.4f} \n"
        f"bars={len(features_5m.get('close', []))}"
    )
    return MLSig(bias=bias, conf=conf, slope=slope, warm=True)
