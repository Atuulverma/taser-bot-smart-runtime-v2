# app/manage/reverse.py
from __future__ import annotations

from app import config as C
from app.ml.gate import get_ml_signal


def allow_reverse(features_5m, position_side: str, structure_flipped: bool) -> bool:
    if not structure_flipped:
        return False
    ml = get_ml_signal(features_5m)
    if not ml.warm:
        return False
    thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
    if position_side == "long":
        return ml.bias == "short" and ml.conf >= thr
    return ml.bias == "long" and ml.conf >= thr
