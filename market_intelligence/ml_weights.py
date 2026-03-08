"""BLOCK 4.1: Online weight optimization for adaptive ML weighting.

Implements ridge regression-based weight optimization without external ML libraries.
Uses only numpy/pure Python for numerical stability and minimal dependencies.
"""
from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque, Dict, List, Optional

logger = logging.getLogger("market_intelligence")


@dataclass
class WeightRecord:
    """Single training record for weight optimization."""
    feature_vector: Dict[str, float]  # Normalized feature values
    score: float  # Score assigned at the time
    actual_outcome: float  # Actual outcome (price change, PnL, etc.)
    timestamp: float


class OnlineWeightOptimizer:
    """BLOCK 4.1: Online weight optimizer using ridge regression.

    Maintains history of (features, score, outcome) and periodically recomputes
    optimal weights. Uses momentum-based updates to avoid overfitting on recent data.
    """

    def __init__(
        self,
        max_history: int = 5000,
        recompute_interval: int = 100,
        momentum: float = 0.7,
        l2_lambda: float = 0.1,
        min_weight: float = 0.05,
        max_weight: float = 0.50,
    ):
        """Initialize optimizer.

        Args:
            max_history: Maximum records to keep
            recompute_interval: Recompute weights every N records
            momentum: Weight update momentum (0.7 = 70% new, 30% current)
            l2_lambda: L2 regularization strength
            min_weight: Minimum allowed weight
            max_weight: Maximum allowed weight
        """
        self.max_history = max_history
        self.recompute_interval = recompute_interval
        self.momentum = momentum
        self.l2_lambda = l2_lambda
        self.min_weight = min_weight
        self.max_weight = max_weight

        self._history: Deque[WeightRecord] = deque(maxlen=max_history)
        self._current_weights: Dict[str, float] = {}
        self._optimized_weights: Dict[str, float] = {}
        self._records_since_update: int = 0
        self._total_optimizations: int = 0

    def record(
        self,
        feature_vector: Dict[str, float],
        score: float,
        actual_outcome: float,
        timestamp: float,
    ) -> None:
        """Record a new training sample.

        Args:
            feature_vector: Feature values used for scoring
            score: Score that was computed
            actual_outcome: Actual outcome (e.g., spread change sign)
        """
        record = WeightRecord(
            feature_vector=dict(feature_vector),
            score=score,
            actual_outcome=actual_outcome,
            timestamp=timestamp,
        )
        self._history.append(record)
        self._records_since_update += 1

        # Trigger recomputation if interval reached and enough data
        if (
            self._records_since_update >= self.recompute_interval
            and len(self._history) >= 200
        ):
            self._recompute_weights()
            self._records_since_update = 0

    def _recompute_weights(self) -> None:
        """Recompute optimal weights using ridge regression."""
        try:
            # Extract features and outcomes
            feature_names = set()
            for rec in self._history:
                feature_names.update(rec.feature_vector.keys())

            feature_names = sorted(feature_names)
            n_samples = len(self._history)
            n_features = len(feature_names)

            if n_samples < n_features:
                logger.warning("Not enough samples for weight optimization: %d < %d", n_samples, n_features)
                return

            # Build design matrix X and target vector y
            X: List[List[float]] = []
            y: List[float] = []

            for rec in self._history:
                row = [rec.feature_vector.get(fname, 0.0) for fname in feature_names]
                X.append(row)
                # Target: sign of actual outcome (did prediction match reality?)
                y.append(1.0 if rec.actual_outcome > 0 else -1.0)

            # Ridge regression: w = (X^T X + λI)^-1 X^T y
            # Using pure Python implementation for numerical stability
            optimized = self._ridge_regression(X, y, feature_names)

            if not optimized:
                logger.warning("Ridge regression failed to converge")
                return

            # Apply momentum update
            if self._current_weights:
                for fname in feature_names:
                    old_val = self._current_weights.get(fname, optimized[fname])
                    new_val = self.momentum * optimized[fname] + (1.0 - self.momentum) * old_val
                    # Clip to bounds
                    new_val = max(self.min_weight, min(self.max_weight, new_val))
                    self._optimized_weights[fname] = new_val
            else:
                # First optimization - just clip
                self._optimized_weights = {
                    fname: max(self.min_weight, min(self.max_weight, val))
                    for fname, val in optimized.items()
                }

            # Normalize to sum to 1.0
            total = sum(self._optimized_weights.values())
            if total > 0:
                self._optimized_weights = {
                    fname: val / total
                    for fname, val in self._optimized_weights.items()
                }

            self._total_optimizations += 1
            logger.info(
                "Weight optimization #%d complete: %d samples, %d features",
                self._total_optimizations,
                n_samples,
                n_features,
            )

        except Exception as e:
            logger.error("Weight optimization failed: %s", e, exc_info=True)

    def _ridge_regression(
        self,
        X: List[List[float]],
        y: List[float],
        feature_names: List[str],
    ) -> Dict[str, float]:
        """Solve ridge regression using normal equations with L2 regularization.

        Pure Python implementation to avoid sklearn dependency.
        """
        n_samples = len(X)
        n_features = len(X[0]) if X else 0

        if n_samples == 0 or n_features == 0:
            return {}

        # Compute X^T X
        XtX = [[0.0] * n_features for _ in range(n_features)]
        for i in range(n_features):
            for j in range(n_features):
                s = 0.0
                for row in X:
                    s += row[i] * row[j]
                XtX[i][j] = s

        # Add L2 regularization: X^T X + λI
        for i in range(n_features):
            XtX[i][i] += self.l2_lambda

        # Compute X^T y
        Xty = [0.0] * n_features
        for i in range(n_features):
            s = 0.0
            for k, row in enumerate(X):
                s += row[i] * y[k]
            Xty[i] = s

        # Solve linear system using Gaussian elimination
        weights = self._solve_linear_system(XtX, Xty)

        if weights is None:
            return {}

        # Convert to absolute values (we care about importance, not sign)
        return {
            feature_names[i]: abs(weights[i])
            for i in range(len(weights))
        }

    @staticmethod
    def _solve_linear_system(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
        """Solve Ax = b using Gaussian elimination with partial pivoting.

        Args:
            A: Coefficient matrix (will be modified)
            b: Right-hand side vector (will be modified)

        Returns:
            Solution vector x, or None if singular
        """
        n = len(b)

        # Make copies to avoid modifying originals
        A = [row[:] for row in A]
        b = b[:]

        # Forward elimination
        for k in range(n - 1):
            # Partial pivoting
            max_idx = k
            max_val = abs(A[k][k])
            for i in range(k + 1, n):
                if abs(A[i][k]) > max_val:
                    max_val = abs(A[i][k])
                    max_idx = i

            if max_val < 1e-12:
                return None  # Singular matrix

            # Swap rows
            if max_idx != k:
                A[k], A[max_idx] = A[max_idx], A[k]
                b[k], b[max_idx] = b[max_idx], b[k]

            # Eliminate
            for i in range(k + 1, n):
                factor = A[i][k] / A[k][k]
                for j in range(k + 1, n):
                    A[i][j] -= factor * A[k][j]
                b[i] -= factor * b[k]
                A[i][k] = 0.0

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            s = b[i]
            for j in range(i + 1, n):
                s -= A[i][j] * x[j]
            if abs(A[i][i]) < 1e-12:
                return None
            x[i] = s / A[i][i]

        return x

    def get_weights(self) -> Dict[str, float]:
        """Get current optimized weights.

        Returns empty dict if not enough data for optimization.
        """
        if len(self._history) < 200:
            return {}

        return dict(self._optimized_weights)

    def has_sufficient_data(self) -> bool:
        """Check if optimizer has enough data for reliable weights."""
        return len(self._history) >= 200

    def save(self, path: Path) -> None:
        """Persist optimizer state to JSON file."""
        try:
            state = {
                "current_weights": self._current_weights,
                "optimized_weights": self._optimized_weights,
                "total_optimizations": self._total_optimizations,
                "history_size": len(self._history),
                "records_since_update": self._records_since_update,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
            logger.info("Weight optimizer state saved to %s", path)
        except Exception as e:
            logger.error("Failed to save weight optimizer state: %s", e)

    def load(self, path: Path) -> bool:
        """Load optimizer state from JSON file.

        Returns True if successful, False otherwise.
        """
        try:
            if not path.exists():
                return False

            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            self._current_weights = state.get("current_weights", {})
            self._optimized_weights = state.get("optimized_weights", {})
            self._total_optimizations = state.get("total_optimizations", 0)
            self._records_since_update = state.get("records_since_update", 0)

            logger.info(
                "Weight optimizer state loaded: %d optimizations, %d records",
                self._total_optimizations,
                state.get("history_size", 0),
            )
            return True
        except Exception as e:
            logger.error("Failed to load weight optimizer state: %s", e)
            return False
