# app/manage/flow.py
from __future__ import annotations

import math
from dataclasses import dataclass

from app import config as C
from app.execution import amend_tps_simple


@dataclass(frozen=True)
class FlowEvent:
    kind: str  # "none" | "tp_replaced" | "locked" | "giveback_exit"
    note: str = ""


def after_tp1_repace(trade, entry: float, sl: float) -> FlowEvent:
    if not getattr(C, "FLOW_REPLACE_TPS", False):
        return FlowEvent("none")
    # compute TP2/TP3 from R-multiples
    r = abs(entry - sl)
    tp2 = entry + (C.FLOW_TP2_R_MULT * r) * (1 if trade.side == "long" else -1)
    tp3 = entry + (C.FLOW_TP3_R_MULT * r) * (1 if trade.side == "long" else -1)
    amend_tps_simple(trade.id, [tp2, tp3])  # idempotent; paper-safe; live requires ex-handle path
    return FlowEvent("tp_replaced", f"tp2={tp2:.4f}, tp3={tp3:.4f}")


def milestone_lock(mfe_r: float) -> float:
    if not getattr(C, "TS_MILESTONE_MODE", True):
        return 0.0
    step = float(getattr(C, "TS_MS_STEP_R", 0.5))
    bump = float(getattr(C, "TS_MS_LOCK_DELTA_R", 0.25))
    steps_crossed = math.floor(mfe_r / step)
    return steps_crossed * bump  # lock-to R from entry


def giveback_exit(mfe_r: float, curr_r: float, ml_slope: float) -> bool:
    arm = float(getattr(C, "TS_GIVEBACK_ARM_R", 1.5))
    frac = float(getattr(C, "TS_GIVEBACK_FRAC", 0.25))
    if mfe_r >= arm and ml_slope < 0 and curr_r <= (1.0 - frac) * mfe_r:
        return True
    return False
