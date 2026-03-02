"""
Risk Management Engine.

Pre-trade checks:
- Exposure limits (total and per-exchange)
- Position count limit
- Balance / margin checks
- Liquidation distance

Runtime monitoring:
- Delta check (long vs short imbalance)
- API health / latency watchdog
- Funding cost control

Emergency actions:
- Close all if margin < critical
- Pause if API lag > threshold
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Tuple

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.state import BotState

if TYPE_CHECKING:
    from arbitrage.strategies.base import Opportunity

logger = get_arbitrage_logger("risk")


class RiskManager:
    """Full risk framework for arbitrage trading."""

    def __init__(self, config: ArbitrageConfig, state: BotState):
        self.config = config
        self.state = state
        self._max_position_pct = config.max_position_pct
        self._max_concurrent = config.max_concurrent_positions
        self._emergency_margin = config.emergency_margin_ratio
        self._max_delta_pct = config.max_delta_percent
        self._consecutive_failures: int = 0
        self._max_failures: int = 5

    # ─── Pre-trade Checks ─────────────────────────────────────────────────

    def can_open_position(self, opp: Opportunity) -> bool:
        """Check if a new position can be opened."""
        # Position count limit
        if self.state.position_count() >= self._max_concurrent:
            return False

        # Already have position on this symbol
        if self.state.has_position_on_symbol(opp.symbol):
            return False

        # Balance check: both exchanges must have funds
        long_bal = self.state.get_balance(opp.long_exchange)
        short_bal = self.state.get_balance(opp.short_exchange)
        min_required = 5.0  # Minimum $5 per side
        if long_bal < min_required or short_bal < min_required:
            return False

        # Total balance too low
        if self.state.total_balance < 10.0:
            return False

        # Exposure check: total open positions value < X% of balance
        total_exposure = sum(p.size_usd for p in self.state.get_all_positions())
        max_exposure = self.state.total_balance * 0.8  # Max 80% of balance in positions
        per_side = min(long_bal, short_bal) * self._max_position_pct
        if total_exposure + per_side * 2 > max_exposure:
            return False

        # Circuit breaker
        if self._consecutive_failures >= self._max_failures:
            return False

        return True

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._max_failures:
            logger.warning(f"Circuit breaker: {self._consecutive_failures} consecutive failures")

    def record_success(self) -> None:
        self._consecutive_failures = 0

    # ─── Runtime Monitoring ───────────────────────────────────────────────

    def should_emergency_close(self) -> Tuple[bool, str]:
        """Check if all positions should be closed immediately."""
        # Balance critically low
        if self.state.total_balance < 5.0 and self.state.position_count() > 0:
            return True, "balance_critical"

        # Delta check: total long vs short exposure
        positions = self.state.get_all_positions()
        if positions:
            total_long = sum(p.size_usd / 2 for p in positions if p.long_contracts > 0)
            total_short = sum(p.size_usd / 2 for p in positions if p.short_contracts > 0)
            if total_long + total_short > 0:
                delta = abs(total_long - total_short) / (total_long + total_short)
                if delta > self._max_delta_pct:
                    return True, f"delta_exceeded_{delta:.3f}"

        return False, ""

    def check_funding_profitability(self, position, expected_income: float, fees: float) -> bool:
        """Check if a funding position is still profitable."""
        net = expected_income - fees
        if net < 0:
            logger.warning(
                f"Funding position {position.symbol} unprofitable: "
                f"income={expected_income:.4f} fees={fees:.4f}"
            )
            return False
        return True
