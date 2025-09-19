# app/manage/reverse.py
from __future__ import annotations

from app import config as C
from app import telemetry
from app.ml.gate import get_ml_signal


def allow_reverse(features_5m, position_side: str, structure_flipped: bool) -> bool:
    side = str(position_side).lower()
    try:
        bars = len(features_5m.get("close", [])) if isinstance(features_5m, dict) else 0
    except Exception:
        bars = 0

    if not structure_flipped:
        try:
            telemetry.log(
                component="manage",
                tag="REVERSE_CHECK",
                message="structure not flipped",
                payload={"side": side, "bars": int(bars)},
            )
        except Exception:
            pass
        return False
    ml = get_ml_signal(features_5m)
    try:
        telemetry.log(
            component="manage",
            tag="REVERSE_ML",
            message="ml tick",
            payload={
                "side": side,
                "warm": bool(ml.warm),
                "bias": ml.bias,
                "conf": float(ml.conf),
                "slope": float(getattr(ml, "slope", 0.0)),
                "bars": int(bars),
                "thr": float(getattr(C, "TS_ML_CONF_THR", 0.56)),
            },
        )
    except Exception:
        pass
    if not ml.warm:
        try:
            telemetry.log(
                component="manage",
                tag="REVERSE_CHECK",
                message="ml cold",
                payload={"side": side, "bars": int(bars)},
            )
        except Exception:
            pass
        return False
    thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
    if side == "long":
        allowed = ml.bias == "short" and ml.conf >= thr
    else:
        allowed = ml.bias == "long" and ml.conf >= thr
    try:
        telemetry.log(
            component="manage",
            tag=("REVERSE_ALLOW" if allowed else "REVERSE_BLOCK"),
            message=("reverse allowed" if allowed else "reverse blocked"),
            payload={
                "side": side,
                "bias": ml.bias,
                "conf": float(ml.conf),
                "thr": float(thr),
            },
        )
    except Exception:
        pass
    return bool(allowed)
