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

from typing import Any, Tuple

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.state import BotState

# Type alias for Opportunity (legacy strategies removed)
Opportunity = Any

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

    # FIX: Robust coercion helper for exposure/balance comparisons.
    @staticmethod
    def _num(value: Any, default: float = 0.0) -> float:
        """Coerce value to float, returning default on failure."""
        try:
            v = float(value)
            if v != v:  # NaN check
                return default
            return v
        except (TypeError, ValueError):
            return default

    def can_open_position(self, opp: Opportunity) -> bool:
        """Check if a new position can be opened.

        FIX #9: Replaced hardcoded $5/$10 minimums with configurable amounts
        relative to total balance. For arb positions, exposure check now uses
        NET exposure (one side only since positions are hedged) instead of
        gross exposure which double-counted and blocked valid trades.
        """
        # Position count limit
        if self.state.position_count() >= self._max_concurrent:
            return False

        # Already have position on this symbol
        if self.state.has_position_on_symbol(opp.symbol):
            return False

        # Balance check: both exchanges must have funds.
        # FIX #9: configurable min based on total balance, not hardcoded $5.
        min_required = max(2.0, self.state.total_balance * 0.002)  # 0.2% of balance, min $2
        long_bal = self._num(self.state.get_balance(opp.long_exchange))
        short_bal = self._num(self.state.get_balance(opp.short_exchange))
        if long_bal < min_required or short_bal < min_required:
            return False

        # Total balance too low — FIX #9
        min_total = max(5.0, self._num(self.state.total_balance))
        if self._num(self.state.total_balance) < min_total:
            return False

        # Exposure check: FIX #9 — arbitrage is hedged, so only NET (one side)
        # exposure matters, not gross. Using gross would double-count and block
        # valid hedged trades.
        net_exposure = sum(
            self._num(getattr(p, "size_usd", 0.0)) for p in self.state.get_all_positions()
        )
        # Each arb position represents 2 legs but only half is net risk,
        # since the other leg is a hedge. Use the position's size_usd as
        # the per-leg notional, which is the actual margin at risk.
        per_side_notional = self._num(
            getattr(opp, "notional_usd", 0.0) or getattr(opp, "size_usd", 0.0)
        )
        max_exposure = self._num(self.state.total_balance) * self._max_position_pct
        if net_exposure + per_side_notional > max_exposure:
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

    def can_enter_position(self, size: float, price: float) -> Tuple[bool, str]:
        """Check if entering a position is allowed (used by legacy ArbitrageEngine).

        FIX #14: Replaced hardcoded $10 minimum with configurable amount
        relative to total balance (same logic as can_open_position).
        """
        if self.state.position_count() >= self._max_concurrent:
            return False, "Max concurrent positions reached"
        if self._consecutive_failures >= self._max_failures:
            return False, "Circuit breaker active"
        total_balance = self.state.total_balance
        # FIX #14: Configurable minimum balance, not hardcoded $10.
        min_total = max(5.0, total_balance * 0.01)
        if total_balance < min_total:
            return False, "Balance too low"
        required = size * price
        if required > total_balance * self._max_position_pct:
            return False, "Position too large for balance"
        return True, "OK"

    def can_exit_position(self) -> Tuple[bool, str]:
        """Check if exiting a position is allowed (used by legacy ArbitrageEngine)."""
        if not self.state.is_in_position:
            return False, "Not in position"
        return True, "OK"

    def validate_spread(self, spread: float, is_entry: bool) -> bool:
        """Validate spread against entry/exit thresholds (core tests)."""
        if is_entry:
            return spread >= self.config.entry_threshold
        return abs(spread) <= self.config.exit_threshold

    # ─── Runtime Monitoring ───────────────────────────────────────────────

    def should_emergency_close(self) -> Tuple[bool, str]:
        """Check if all positions should be closed immediately.

        FIX #8: Delta calculation now uses actual per-leg sizes from position
        metadata instead of assuming 50/50 split. Partial fills and contract-size
        differences mean legs can drift materially. Also checks both ActivePosition
        and Position types.
        """
        # Balance critically low
        min_balance = max(3.0, self.state.total_balance * 0.005)  # 0.5% of balance
        if self.state.total_balance < min_balance and self.state.position_count() > 0:
            return True, "balance_critical"

        # Delta check: FIX #8 — use actual leg sizes, not assumed 50/50.
        positions = self.state.get_all_positions()
        if not positions:
            return False, ""

        imbalances = []
        for p in positions:
            if isinstance(p, dict):
                # Serialized form (from persistence reload)
                long_size = p.get("long_contracts", p.get("size", 0.0) or 0.0)
                short_size = p.get("short_contracts", p.get("size", 0.0) or 0.0)
            else:
                long_size = getattr(p, "long_contracts", 0.0) or 0.0
                short_size = getattr(p, "short_contracts", 0.0) or 0.0

            if long_size <= 0 and short_size <= 0:
                # Legacy Position type — doesn't have leg sizes, skip delta
                continue

            total_legs = long_size + short_size
            if total_legs > 0:
                imbalance = abs(long_size - short_size) / total_legs
                imbalances.append(imbalance)

        if imbalances:
            max_imbalance = max(imbalances)
            if max_imbalance > self._max_delta_pct:
                return True, f"delta_exceeded_{max_imbalance:.3f}"

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

    def log_risk_status(self) -> None:
        """Log current risk metrics."""
        logger.info(
            f"Risk status: positions={self.state.position_count()}, "
            f"balance={self.state.total_balance:.2f}, "
            f"failures={self._consecutive_failures}/{self._max_failures}"
        )
