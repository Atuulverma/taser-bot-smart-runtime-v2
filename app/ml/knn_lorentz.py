from __future__ import annotations

import math
from typing import List


def lorentz_distance(a: List[float], b: List[float]) -> float:
    return sum(math.log(1.0 + abs(a[i] - b[i])) for i in range(min(len(a), len(b))))
