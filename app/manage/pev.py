# app/manage/pev.py
from __future__ import annotations

from dataclasses import dataclass

from app import config as C
from app import telemetry
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
    # ML heartbeat for PEV (pre-TP1)
    try:
        bars = 0
        if isinstance(features_5m, dict):
            closes = features_5m.get("close", [])
            bars = len(closes) if hasattr(closes, "__len__") else 0
        telemetry.log(
            component="manage",
            tag="PEV_ML",
            message="pev ml tick",
            payload={
                "side": str(side).lower(),
                "warm": bool(ml.warm),
                "bias": ml.bias,
                "conf": float(ml.conf),
                "slope": float(ml.slope),
                "bars": int(bars),
                "grace_over": bool(grace_over),
                "bars_confirmed": bool(bars_confirmed),
                "thr": float(getattr(C, "TS_ML_CONF_THR", 0.56)),
            },
        )
    except Exception:
        pass

    if not ml.warm:
        return PEVDecision("hold")

    thr = float(getattr(C, "TS_ML_CONF_THR", 0.56))
    side = str(side).lower()
    if side == "long":
        if ml.bias == "short" and ml.conf >= thr and bars_confirmed:
            try:
                telemetry.log(
                    component="manage",
                    tag="PEV_DECISION",
                    message="exit",
                    payload={
                        "reason": "PEV_ML_FLIP",
                        "side": side,
                        "bias": ml.bias,
                        "conf": float(ml.conf),
                        "thr": float(thr),
                    },
                )
            except Exception:
                pass
            return PEVDecision("exit", "PEV_ML_FLIP")
    else:
        if ml.bias == "long" and ml.conf >= thr and bars_confirmed:
            try:
                telemetry.log(
                    component="manage",
                    tag="PEV_DECISION",
                    message="exit",
                    payload={
                        "reason": "PEV_ML_FLIP",
                        "side": side,
                        "bias": ml.bias,
                        "conf": float(ml.conf),
                        "thr": float(thr),
                    },
                )
            except Exception:
                pass
            return PEVDecision("exit", "PEV_ML_FLIP")

    if grace_over and ml.conf < thr:
        try:
            telemetry.log(
                component="manage",
                tag="PEV_DECISION",
                message="tighten",
                payload={
                    "reason": "PEV_ML_WEAK",
                    "target": "BE_FEES",
                    "side": side,
                    "bias": ml.bias,
                    "conf": float(ml.conf),
                    "thr": float(thr),
                    "grace_over": bool(grace_over),
                },
            )
        except Exception:
            pass
        return PEVDecision("tighten", "PEV_ML_WEAK", "BE_FEES")

    return PEVDecision("hold")
