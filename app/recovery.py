# app/recovery.py
from __future__ import annotations
from dataclasses import dataclass
import app.config as C

@dataclass
class RecoverySnapshot:
    start_balance: float
    equity: float
    drawdown_abs: float
    drawdown_pct: float
    recovered_pct: float

def recovery_snapshot(realized_pnl: float, unrealized_pnl: float = 0.0) -> RecoverySnapshot:
    start = float(getattr(C, "PAPER_START_BALANCE", 0.0))
    equity = start + float(realized_pnl) + float(unrealized_pnl)
    dd = start - equity
    dd_abs = max(0.0, dd)
    dd_pct = (dd_abs / start * 100.0) if start > 0 else 0.0
    rec_pct = (equity / start * 100.0) if start > 0 else 0.0
    return RecoverySnapshot(start, equity, dd_abs, dd_pct, rec_pct)

def estimate_days_to_recover(avg_daily_realized_pnl: float, realized_pnl: float, unrealized_pnl: float = 0.0) -> float | None:
    snap = recovery_snapshot(realized_pnl, unrealized_pnl)
    if avg_daily_realized_pnl <= 0:
        return None
    return snap.drawdown_abs / avg_daily_realized_pnl