from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class AllocationDecision:
    approved: bool
    size_usd: float
    reason: Optional[str] = None


class PortfolioManager:
    def __init__(self, equity_usd: float) -> None:
        self.equity = equity_usd
        self.open_trades = 0

    def approve(
        self,
        risk_pct_per_trade: float,
        max_concurrent: int,
        daily_stop_hit: bool,
        sl_distance_pct: float,
    ) -> AllocationDecision:
        if daily_stop_hit:
            return AllocationDecision(False, 0.0, "daily stop hit")
        if self.open_trades >= max_concurrent:
            return AllocationDecision(False, 0.0, "max concurrent reached")
        risk_cap = max(0.0, self.equity * max(0.0, min(1.0, risk_pct_per_trade)))
        size = risk_cap / max(1e-9, sl_distance_pct)
        return AllocationDecision(True, size)
