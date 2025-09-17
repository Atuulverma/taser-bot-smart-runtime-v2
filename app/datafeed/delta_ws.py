from __future__ import annotations

from typing import Callable


class DeltaWS:
    """
    Placeholder for WebSocket subscriber for trades/orderbook/candles.
    """

    def __init__(self) -> None: ...

    def subscribe(self, channel: str, symbol: str, on_message: Callable[[dict], None]) -> None:
        # Implement using official WS when wiring
        ...
