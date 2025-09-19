# app/manage/flow.py
from __future__ import annotations

import math
from dataclasses import dataclass

from app import config as C
from app import telemetry
from app.execution import amend_tps_simple


@dataclass(frozen=True)
class FlowEvent:
    kind: str  # "none" | "tp_replaced" | "locked" | "giveback_exit"
    note: str = ""


def after_tp1_replace(trade, entry: float, sl: float) -> FlowEvent:
    if not getattr(C, "FLOW_REPLACE_TPS", False):
        return FlowEvent("none")
    # compute TP2/TP3 from R-multiples
    r = abs(entry - sl)
    sgn = 1 if str(getattr(trade, "side", "")).lower() == "long" else -1
    tp2 = round(entry + (float(getattr(C, "FLOW_TP2_R_MULT", 2.0)) * r) * sgn, 4)
    tp3 = round(entry + (float(getattr(C, "FLOW_TP3_R_MULT", 3.0)) * r) * sgn, 4)
    tps = [tp2, tp3]
    # telemetry before attempting amend
    try:
        telemetry.log(
            component="manage",
            tag="TP_REPLACE_ATTEMPT",
            message="replace tp2/tp3 after tp1",
            payload={
                "trade_id": getattr(trade, "id", None),
                "entry": float(entry),
                "sl": float(sl),
                "r": float(r),
                "tps": tps,
                "side": str(getattr(trade, "side", "")).lower(),
            },
        )
    except Exception:
        pass
    # apply
    try:
        amend_tps_simple(trade.id, tps)  # idempotent; paper-safe; live requires ex-handle path
        try:
            telemetry.log(
                component="manage",
                tag="TP_REPLACED",
                message="tp2/tp3 amended",
                payload={"trade_id": getattr(trade, "id", None), "tps": tps},
            )
        except Exception:
            pass
        return FlowEvent("tp_replaced", f"tp2={tp2:.4f}, tp3={tp3:.4f}")
    except Exception as e:
        try:
            telemetry.log(
                component="manage",
                tag="TP_REPLACE_ERROR",
                message="amend failed",
                payload={"trade_id": getattr(trade, "id", None), "error": str(e)},
            )
        except Exception:
            pass
        return FlowEvent("none", "tp amend failed")


# Back-compat: retain old name used earlier in patches
after_tp1_repace = after_tp1_replace


def milestone_lock(mfe_r: float) -> float:
    if not getattr(C, "TS_MILESTONE_MODE", True):
        return 0.0
    step = float(getattr(C, "TS_MS_STEP_R", 0.5))
    bump = float(getattr(C, "TS_MS_LOCK_DELTA_R", 0.25))
    steps_crossed = math.floor(mfe_r / step)
    lock_r = steps_crossed * bump  # lock-to R from entry
    if lock_r > 0:
        try:
            telemetry.log(
                component="manage",
                tag="MS_LOCK",
                message="milestone lock update",
                payload={
                    "mfe_r": float(mfe_r),
                    "lock_r": float(lock_r),
                    "step": step,
                    "bump": bump,
                },
            )
        except Exception:
            pass
    return lock_r


def giveback_exit(mfe_r: float, curr_r: float, ml_slope: float) -> bool:
    arm = float(getattr(C, "TS_GIVEBACK_ARM_R", 1.5))
    frac = float(getattr(C, "TS_GIVEBACK_FRAC", 0.25))
    armed = mfe_r >= arm
    triggered = armed and (ml_slope < 0) and (curr_r <= (1.0 - frac) * mfe_r)
    if armed:
        try:
            telemetry.log(
                component="manage",
                tag=("GIVEBACK_EXIT" if triggered else "GIVEBACK_ARMED_HOLD"),
                message=("giveback exit" if triggered else "giveback armed, holding"),
                payload={
                    "mfe_r": float(mfe_r),
                    "curr_r": float(curr_r),
                    "ml_slope": float(ml_slope),
                    "arm_r": float(arm),
                    "frac": float(frac),
                },
            )
        except Exception:
            pass
    return bool(triggered)
