"""
Metrics tracking for arbitrage strategies.
PnL, Sharpe, drawdown, latency, per-strategy stats.
"""
import time
import math
from typing import Dict, List, Any
from collections import deque


class MetricsTracker:
    """Tracks trading metrics across all strategies."""

    def __init__(self):
        # Per-strategy trade records
        self._trades: Dict[str, List[Dict[str, Any]]] = {}
        # PnL history for Sharpe/drawdown (each element: (timestamp, pnl))
        self._pnl_history: deque = deque(maxlen=1000)
        self._cumulative_pnl: float = 0.0
        self._peak_pnl: float = 0.0
        self._max_drawdown: float = 0.0
        # Cycle times
        self._cycle_times: deque = deque(maxlen=100)
        # Entry/exit counts
        self._entries: int = 0
        self._exits: int = 0

    def record_entry(self, strategy: str, symbol: str) -> None:
        self._entries += 1

    def record_exit(self, strategy: str, symbol: str, pnl: float, reason: str) -> None:
        self._exits += 1
        self._pnl_history.append((time.time(), pnl))
        self._cumulative_pnl += pnl

        # Track peak and drawdown
        if self._cumulative_pnl > self._peak_pnl:
            self._peak_pnl = self._cumulative_pnl
        dd = self._peak_pnl - self._cumulative_pnl
        if dd > self._max_drawdown:
            self._max_drawdown = dd

        # Per-strategy
        if strategy not in self._trades:
            self._trades[strategy] = []
        self._trades[strategy].append({
            "symbol": symbol,
            "pnl": pnl,
            "reason": reason,
            "time": time.time(),
        })

    def record_cycle_time(self, elapsed: float) -> None:
        self._cycle_times.append(elapsed)

    def sharpe_ratio(self) -> float:
        """Annualized Sharpe ratio from trade PnLs.

        FIX H1: Previously assumed a hardcoded "~3 trades per day" regardless of
        actual data. Now computes the real observation window from timestamps
        and annualizes correctly: annual_factor = sqrt(365 / days_observed).
        """
        if len(self._pnl_history) < 5:
            return 0.0

        records = list(self._pnl_history)  # (timestamp, pnl)
        pnls = [r[1] for r in records]
        mean = sum(pnls) / len(pnls)

        if len(pnls) < 2:
            return 0.0
        variance = sum((p - mean) ** 2 for p in pnls) / (len(pnls) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std < 1e-10:
            return 0.0

        # Compute actual observation window from timestamps
        first_ts = records[0][0]
        last_ts = records[-1][0]
        days_observed = max((last_ts - first_ts) / 86400.0, 1.0)  # at least 1 day
        annual_factor = math.sqrt(365.0 / days_observed)

        return (mean / std) * annual_factor

    def summary(self) -> Dict[str, Any]:
        avg_cycle = (
            sum(self._cycle_times) / len(self._cycle_times)
            if self._cycle_times else 0.0
        )
        return {
            "entries": self._entries,
            "exits": self._exits,
            "cumulative_pnl": self._cumulative_pnl,
            "max_drawdown": self._max_drawdown,
            "sharpe": self.sharpe_ratio(),
            "avg_cycle_ms": avg_cycle * 1000,
            "per_strategy": {
                name: {
                    "trades": len(trades),
                    "pnl": sum(t["pnl"] for t in trades),
                    "wins": sum(1 for t in trades if t["pnl"] > 0),
                }
                for name, trades in self._trades.items()
            },
        }
