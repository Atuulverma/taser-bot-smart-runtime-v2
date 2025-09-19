from __future__ import annotations

from typing import List


class IdentityScaler:
    def fit(self, X: List[List[float]]) -> "IdentityScaler":
        return self

    def transform(self, X: List[List[float]]) -> List[List[float]]:
        return X

    def fit_transform(self, X: List[List[float]]) -> List[List[float]]:
        return X
