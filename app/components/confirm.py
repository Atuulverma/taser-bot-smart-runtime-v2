# app/components/confirm.py
from __future__ import annotations

from collections import deque
from typing import Deque


class ConfirmationBuffer:
    def __init__(self, need_bars: int = 2) -> None:
        self.need = int(max(1, need_bars))
        self._closes: Deque[float] = deque(maxlen=self.need)
        self._last_ts: int | None = None

    def append_closed(self, ts: int, close: float) -> None:
        if ts is None:
            return
        if self._last_ts == ts:
            return
        self._last_ts = ts
        self._closes.append(float(close))

    def confirm(self, level: float, is_long: bool) -> bool:
        if len(self._closes) < self.need:
            return False
        window = list(self._closes)[-self.need :]
        if is_long:
            return all(c >= float(level) for c in window)
        return all(c <= float(level) for c in window)
