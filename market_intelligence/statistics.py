from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from statistics import mean, pstdev
from typing import Deque, Iterable, List


def pearson_corr(xs: List[float], ys: List[float]) -> float:
    """Compute Pearson correlation coefficient."""
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0
    mx = mean(xs)
    my = mean(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    vx = sum((x - mx) ** 2 for x in xs)
    vy = sum((y - my) ** 2 for y in ys)
    if vx <= 1e-12 or vy <= 1e-12:
        return 0.0
    return cov / (vx * vy) ** 0.5


def spearman_corr(xs: List[float], ys: List[float]) -> float:
    """BLOCK 2.2: Compute Spearman rank correlation coefficient.

    More robust to outliers than Pearson correlation.
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0

    # Convert to ranks
    def rank_transform(values: List[float]) -> List[float]:
        """Assign ranks to values (average rank for ties)."""
        n = len(values)
        # Create pairs of (value, original_index)
        indexed = [(v, i) for i, v in enumerate(values)]
        # Sort by value
        indexed.sort(key=lambda x: x[0])

        ranks = [0.0] * n
        i = 0
        while i < n:
            # Find ties
            j = i
            while j < n and indexed[j][0] == indexed[i][0]:
                j += 1

            # Average rank for ties (ranks start at 1)
            avg_rank = (i + 1 + j) / 2.0

            for k in range(i, j):
                ranks[indexed[k][1]] = avg_rank

            i = j

        return ranks

    rank_x = rank_transform(xs)
    rank_y = rank_transform(ys)

    # Compute Pearson correlation on ranks
    return pearson_corr(rank_x, rank_y)


def robust_corr(xs: List[float], ys: List[float], method: str = "auto") -> float:
    """BLOCK 2.2: Robust correlation that handles outliers gracefully.

    Args:
        xs: First data series
        ys: Second data series
        method: "auto", "pearson", or "spearman"
            - "auto": Use Spearman if Pearson and Spearman differ significantly (>0.15),
                      otherwise return average of both
            - "pearson": Force Pearson correlation
            - "spearman": Force Spearman correlation

    Returns:
        Correlation coefficient in [-1, 1] range
    """
    if len(xs) != len(ys) or len(xs) < 3:
        return 0.0

    if method == "pearson":
        return pearson_corr(xs, ys)
    elif method == "spearman":
        return spearman_corr(xs, ys)
    elif method == "auto":
        pearson = pearson_corr(xs, ys)
        spearman = spearman_corr(xs, ys)

        # If they differ significantly, outliers are likely present - use Spearman
        if abs(pearson - spearman) > 0.15:
            return spearman

        # Otherwise, return average of both
        return (pearson + spearman) / 2.0
    else:
        raise ValueError(f"Unknown method: {method}")


@dataclass
class RollingStats:
    window: int

    def __post_init__(self) -> None:
        self._values: Deque[float] = deque(maxlen=self.window)

    def push(self, value: float) -> None:
        self._values.append(float(value))

    @property
    def values(self) -> List[float]:
        return list(self._values)

    def mean(self) -> float:
        return mean(self._values) if self._values else 0.0

    def std(self) -> float:
        if len(self._values) < 2:
            return 0.0
        return pstdev(self._values)

    def zscore(self, value: float) -> float:
        m = self.mean()
        s = self.std()
        if s <= 1e-12:
            return 0.0
        return (value - m) / s

    def min(self) -> float:
        return min(self._values) if self._values else 0.0

    def max(self) -> float:
        return max(self._values) if self._values else 0.0

    def minmax_scale(self, value: float) -> float:
        lo = self.min()
        hi = self.max()
        if hi - lo <= 1e-12:
            return 0.5
        return (value - lo) / (hi - lo)

    def percentile_rank(self, value: float) -> float:
        vals = list(self._values)
        if not vals:
            return 0.5
        less_or_equal = sum(1 for x in vals if x <= value)
        return less_or_equal / len(vals)


def rolling_returns(values: Iterable[float]) -> List[float]:
    vals = list(values)
    out: List[float] = []
    for i in range(1, len(vals)):
        prev = vals[i - 1]
        if abs(prev) <= 1e-12:
            out.append(0.0)
        else:
            out.append((vals[i] - prev) / prev)
    return out
