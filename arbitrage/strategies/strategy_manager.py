"""
Strategy Router — central coordinator for all arbitrage strategies.

Pipeline:
    Market Data Update → Detect Opportunities → Rank → Risk Check → Execute → Monitor Exits

Runs all enabled strategies in a single async loop.
"""
import asyncio
import time
from typing import Dict, List, Set

from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.market_data import MarketDataEngine
from arbitrage.core.state import BotState, ActivePosition
from arbitrage.core.risk import RiskManager
from arbitrage.core.notifications import NotificationManager
from arbitrage.core.metrics import MetricsTracker
from arbitrage.strategies.base import BaseStrategy, Opportunity
from arbitrage.strategies.funding_arb import FundingArbStrategy
from arbitrage.strategies.basis_arb import BasisArbStrategy
from arbitrage.strategies.stat_arb import StatArbStrategy

logger = get_arbitrage_logger("strategy_router")

# Interval between full cycles (seconds)
DEFAULT_CYCLE_INTERVAL = 2.0
FUNDING_UPDATE_INTERVAL = 60  # Funding rates change slowly
SPOT_UPDATE_INTERVAL = 5      # Spot prices less critical
BALANCE_UPDATE_INTERVAL = 30


class StrategyRouter:
    """
    Runs all strategies, detects opportunities, routes to execution.
    """

    def __init__(
        self,
        config: ArbitrageConfig,
        state: BotState,
        market_data: MarketDataEngine,
        risk_manager: RiskManager,
        executor,  # TradeExecutor — imported at runtime to avoid circular
        notification_manager: NotificationManager,
    ):
        self.config = config
        self.state = state
        self.market_data = market_data
        self.risk = risk_manager
        self.executor = executor
        self.notifications = notification_manager
        self.metrics = MetricsTracker()

        self.is_running = False
        self._strategies: Dict[str, BaseStrategy] = {}
        self._enabled: Set[str] = set()
        self._cycle_count = 0
        self._last_funding_update = 0.0
        self._last_spot_update = 0.0
        self._last_balance_update = 0.0

        # Parse enabled strategies
        enabled_str = config.enabled_strategies.lower()
        for name in enabled_str.split(","):
            name = name.strip()
            if name:
                self._enabled.add(name)

        # Instantiate strategies
        if "funding" in self._enabled or "funding_arb" in self._enabled:
            self._strategies["funding_arb"] = FundingArbStrategy(config, market_data)
        if "basis" in self._enabled or "basis_arb" in self._enabled:
            self._strategies["basis_arb"] = BasisArbStrategy(config, market_data)
        if "stat_arb" in self._enabled:
            self._strategies["stat_arb"] = StatArbStrategy(config, market_data)

        logger.info(f"Strategies enabled: {list(self._strategies.keys())}")

    # ─── Lifecycle ────────────────────────────────────────────────────────

    async def initialize(self) -> int:
        """Initialize market data. Returns number of common pairs."""
        count = await self.market_data.initialize()
        # Initial data fetch
        await self.market_data.update_all()
        # Initial balance fetch
        balances = await self.market_data.fetch_balances()
        for ex, bal in balances.items():
            self.state.update_balance(ex, bal)
        logger.info(f"Router initialized: {count} common pairs, "
                    f"balance=${self.state.total_balance:.2f}")
        return count

    async def start(self) -> None:
        """Main loop — runs until stopped."""
        self.is_running = True
        logger.info("Strategy router started")

        while self.is_running:
            try:
                await self._run_cycle()
                await asyncio.sleep(self.config.update_interval)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Cycle error: {e}", exc_info=True)
                await asyncio.sleep(5)

        logger.info("Strategy router stopped")

    def stop(self) -> None:
        self.is_running = False

    # ─── Main Cycle ───────────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        """One full cycle: update data → detect → check exits → execute entries."""
        self._cycle_count += 1
        now = time.time()
        t0 = now

        # 1. Update market data (futures every cycle, funding/spot/balances less often)
        await self.market_data.update_futures_prices()

        if now - self._last_funding_update > FUNDING_UPDATE_INTERVAL:
            await self.market_data.update_funding_rates()
            self._last_funding_update = now

        if now - self._last_spot_update > SPOT_UPDATE_INTERVAL:
            await self.market_data.update_spot_prices()
            self._last_spot_update = now

        if now - self._last_balance_update > BALANCE_UPDATE_INTERVAL:
            balances = await self.market_data.fetch_balances()
            for ex, bal in balances.items():
                self.state.update_balance(ex, bal)
            self._last_balance_update = now

        # 2. Check exits on existing positions
        await self._check_all_exits()

        # 3. Check risk — emergency close if needed
        emergency, reason = self.risk.should_emergency_close()
        if emergency:
            logger.warning(f"EMERGENCY CLOSE: {reason}")
            await self._emergency_close_all(reason)
            return

        # 4. Detect opportunities from all strategies
        if not self.config.monitoring_only:
            all_opps = await self._detect_all()

            # 5. Filter & rank
            ranked = self._rank_opportunities(all_opps)

            # 6. Execute best opportunities (respecting position limits)
            for opp in ranked:
                if self.state.position_count() >= self.config.max_concurrent_positions:
                    break
                if self.state.has_position_on_symbol(opp.symbol):
                    continue
                if not self.risk.can_open_position(opp):
                    continue
                await self._execute_entry(opp)

        # Log cycle time periodically
        elapsed = time.time() - t0
        self.metrics.record_cycle_time(elapsed)
        if self._cycle_count % 30 == 0:
            self._log_status()

    # ─── Detection ────────────────────────────────────────────────────────

    async def _detect_all(self) -> List[Opportunity]:
        """Run all strategies and collect opportunities."""
        all_opps: List[Opportunity] = []

        for name, strategy in self._strategies.items():
            try:
                opps = await strategy.detect_opportunities(self.market_data)
                all_opps.extend(opps)
            except Exception as e:
                logger.error(f"Strategy {name} detect error: {e}", exc_info=True)

        return all_opps

    def _rank_opportunities(self, opportunities: List[Opportunity]) -> List[Opportunity]:
        """Rank by expected_profit * confidence. Filter duplicates."""
        # Remove duplicates (same symbol)
        best_per_symbol: Dict[str, Opportunity] = {}
        for opp in opportunities:
            key = opp.symbol
            score = opp.expected_profit_pct * opp.confidence
            existing = best_per_symbol.get(key)
            if not existing or score > existing.expected_profit_pct * existing.confidence:
                best_per_symbol[key] = opp

        ranked = sorted(
            best_per_symbol.values(),
            key=lambda o: o.expected_profit_pct * o.confidence,
            reverse=True,
        )
        return ranked

    # ─── Execution ────────────────────────────────────────────────────────

    async def _execute_entry(self, opp: Opportunity) -> bool:
        """Attempt to open a position for an opportunity."""
        try:
            success = await self.executor.execute_entry(opp, self.state, self.market_data)
            if success:
                self.metrics.record_entry(opp.strategy.value, opp.symbol)
                await self.notifications.notify_position_opened(
                    symbol=opp.symbol,
                    long_exchange=opp.long_exchange,
                    short_exchange=opp.short_exchange,
                    size=0,  # Will be set by executor
                    long_price=opp.long_price,
                    short_price=opp.short_price,
                    spread=opp.expected_profit_pct,
                )
                return True
        except Exception as e:
            logger.error(f"Entry execution error {opp.symbol}: {e}", exc_info=True)
        return False

    # ─── Exit Checking ────────────────────────────────────────────────────

    async def _check_all_exits(self) -> None:
        """Check exit conditions for all open positions."""
        positions = list(self.state.positions.items())
        for _key, pos in positions:
            strategy_name = pos.strategy
            strategy = self._strategies.get(strategy_name)
            if not strategy:
                # Unknown strategy — close on timeout
                if pos.duration() > 3600:
                    await self._execute_exit(pos, "unknown_strategy_timeout")
                continue

            try:
                should_exit, reason = await strategy.should_exit(pos, self.market_data)
                if should_exit:
                    await self._execute_exit(pos, reason)
            except Exception as e:
                logger.error(f"Exit check error {pos.symbol}: {e}", exc_info=True)

    async def _execute_exit(self, pos: ActivePosition, reason: str) -> bool:
        """Close a position."""
        try:
            success, pnl = await self.executor.execute_exit(pos, self.state, self.market_data, reason)
            if success:
                self.state.record_trade(pos.strategy, success=(pnl > 0), pnl=pnl)
                self.metrics.record_exit(pos.strategy, pos.symbol, pnl, reason)
                await self.notifications.notify_position_closed(
                    symbol=pos.symbol,
                    pnl=pnl,
                    long_exchange=pos.long_exchange,
                    short_exchange=pos.short_exchange,
                    size=pos.size_usd,
                    duration_seconds=pos.duration(),
                    entry_spread=pos.entry_spread,
                    exit_spread=0,
                )
                return True
            else:
                # Partial close — notify
                await self.notifications.notify_error(
                    "EXIT FAILED",
                    f"{pos.symbol} L:{pos.long_exchange} S:{pos.short_exchange} — retry next cycle"
                )
        except Exception as e:
            logger.error(f"Exit execution error {pos.symbol}: {e}", exc_info=True)
        return False

    # ─── Emergency ────────────────────────────────────────────────────────

    async def _emergency_close_all(self, reason: str) -> None:
        """Close ALL positions immediately."""
        positions = list(self.state.positions.values())
        if not positions:
            return

        await self.notifications.notify_error(
            "EMERGENCY CLOSE",
            f"Reason: {reason}\nClosing {len(positions)} positions"
        )

        for pos in positions:
            try:
                await self.executor.execute_exit(pos, self.state, self.market_data, f"emergency:{reason}")
            except Exception as e:
                logger.error(f"Emergency close error {pos.symbol}: {e}")

    # ─── Status / Scan ────────────────────────────────────────────────────

    async def scan_all(self) -> Dict[str, list]:
        """One-shot scan for UI display."""
        await self.market_data.update_all()
        results = {}

        for name, strategy in self._strategies.items():
            try:
                if hasattr(strategy, "get_all_spreads"):
                    results[name] = strategy.get_all_spreads(self.market_data)[:10]
            except Exception as e:
                logger.error(f"Scan {name} error: {e}")
                results[name] = []

        return results

    def get_status(self) -> Dict:
        """Current status for UI."""
        return {
            "is_running": self.is_running,
            "strategies": list(self._strategies.keys()),
            "cycle_count": self._cycle_count,
            "positions": self.state.position_count(),
            "total_trades": self.state.total_trades,
            "total_pnl": self.state.total_pnl,
            "total_balance": self.state.total_balance,
            "balances": dict(self.state.balances),
            "metrics": self.metrics.summary(),
            "strategy_stats": self.state.strategy_stats,
            "can_trade": not self.config.monitoring_only,
            "mode": (
                "monitoring" if self.config.monitoring_only
                else "dry_run" if self.config.dry_run_mode
                else "REAL"
            ),
        }

    def _log_status(self) -> None:
        bal_parts = [f"{ex.upper()}=${b:.2f}" for ex, b in self.state.balances.items()]
        pos_count = self.state.position_count()
        logger.info(
            f"Cycle #{self._cycle_count} | "
            f"Positions: {pos_count} | "
            f"Trades: {self.state.total_trades} | "
            f"PnL: ${self.state.total_pnl:+.4f} | "
            f"Balance: {', '.join(bal_parts)}"
        )
