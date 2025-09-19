# app/manage/pev.py
from __future__ import annotations

from dataclasses import dataclass

from app import config as C
from app.ml.gate import get_ml_signal


@dataclass(frozen=True)
class PEVDecision:
    action: str  # "hold" | "tighten" | "exit"
    reason: str = ""
    target: str = ""  # e.g. "BE_FEES"


def revalidate(features_5m, side: str, grace_over: bool, bars_confirmed: bool) -> PEVDecision:
    if not getattr(C, "PEV_ENABLED", True):
        return PEVDecision("hold")

    ml = get_ml_signal(features_5m)
    if not ml.warm:
        return PEVDecision("hold")

    thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
    if side == "long":
        if ml.bias == "short" and ml.conf >= thr and bars_confirmed:
            return PEVDecision("exit", "PEV_ML_FLIP")
    else:
        if ml.bias == "long" and ml.conf >= thr and bars_confirmed:
            return PEVDecision("exit", "PEV_ML_FLIP")

    if grace_over and ml.conf < thr:
        return PEVDecision("tighten", "PEV_ML_WEAK", "BE_FEES")

    return PEVDecision("hold")
