from __future__ import annotations

from typing import Iterable, List, Tuple


def walkforward_splits(
    n: int, folds: int = 5, min_train: int = 500, step: int = 200
) -> Iterable[Tuple[List[int], List[int]]]:
    """
    Yield (train_idx, val_idx) splits for walk-forward validation.
    Minimal placeholder with index ranges.
    """
    start = min_train
    while start + step < n and folds > 0:
        train_idx = list(range(0, start))
        val_idx = list(range(start, min(n, start + step)))
        yield train_idx, val_idx
        start += step
        folds -= 1
