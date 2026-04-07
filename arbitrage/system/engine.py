from __future__ import annotations

import asyncio
import logging
import os

# FIX #9/#8: Cached at module load so the hot loop in run_cycle() does not call
# os.getenv() every cycle. Still respects the env var at startup.
_MAX_EQUITY_PER_TRADE_PCT: float = float(os.getenv("MAX_EQUITY_PER_TRADE_PCT", "0.30"))
# FIX CRITICAL #8: Also cache position exit parameters — previously called every cycle.
_EXIT_TAKE_PROFIT_USD: float = max(0.01, float(os.getenv("EXIT_TAKE_PROFIT_USD", "0.50")))
_EXIT_MAX_HOLD_SECONDS: float = max(30, int(float(os.getenv("EXIT_MAX_HOLD_SECONDS", "3600"))))
_EXIT_CLOSE_EDGE_BPS: float = max(0.0, float(os.getenv("EXIT_CLOSE_EDGE_BPS", "0.5")))
_POSITION_MONITOR_LOG_INTERVAL_SEC: float = max(5, int(float(os.getenv("POSITION_MONITOR_LOG_INTERVAL_SEC", "20"))))
_LOSS_STREAK_LIMIT: int = max(1, int(float(os.getenv("LOSS_STREAK_LIMIT", "3"))))
_LOSS_STREAK_COOLDOWN_SECONDS: float = max(
    60, int(float(os.getenv("LOSS_STREAK_COOLDOWN_HOURS", "3")) * 3600)
)
# FIX CRITICAL #1: Cached margin reject cooldown (was os.getenv every cycle)
_MARGIN_REJECT_COOLDOWN_SECONDS: float = max(60, int(float(os.getenv("MARGIN_REJECT_COOLDOWN_SECONDS", "1800"))))
import time
from dataclasses import dataclass, replace
from dataclasses import field
from typing import List

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.circuit_breaker import ExchangeCircuitBreaker
from arbitrage.system.config import TradingSystemConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.interfaces import MarketDataProvider, MonitoringSink
from arbitrage.system.models import StrategyId
from arbitrage.system.risk import RiskEngine
from arbitrage.system.state import SystemState
from arbitrage.system.strategy_runner import StrategyRunner
from arbitrage.system.strategies.futures_cross_exchange import FuturesCrossExchangeStrategy
from arbitrage.system.strategies.cash_and_carry import CashAndCarryStrategy

logger = logging.getLogger("trading_system")


def build_strategies(config: TradingSystemConfig, market_data=None) -> List:
    """Build enabled strategies."""
    mapping = {
        StrategyId.FUTURES_CROSS_EXCHANGE.value: FuturesCrossExchangeStrategy(
            min_spread_pct=config.strategy.min_spread_pct,
            target_profit_pct=config.strategy.target_profit_pct,
            max_spread_risk_pct=config.strategy.max_spread_risk_pct,
            exit_spread_pct=config.strategy.exit_spread_pct,
            funding_threshold_pct=config.strategy.funding_rate_threshold_pct,
            max_latency_ms=config.strategy.max_entry_latency_ms,
            min_book_depth_multiplier=config.strategy.min_book_depth_multiplier,
        ),
        StrategyId.CASH_AND_CARRY.value: CashAndCarryStrategy(
            min_funding_apr_pct=config.strategy.cash_carry_min_funding_apr_pct,
            max_basis_spread_pct=config.strategy.cash_carry_max_basis_spread_pct,
            min_holding_hours=config.strategy.cash_carry_min_holding_hours,
            max_holding_hours=config.strategy.cash_carry_max_holding_hours,
            min_book_depth_usd=config.strategy.cash_carry_min_book_depth_usd,
        ),
    }
    return [mapping[name] for name in config.strategy.enabled if name in mapping]


@dataclass
class TradingSystemEngine:
    config: TradingSystemConfig
    provider: MarketDataProvider
    monitor: MonitoringSink
    risk: RiskEngine
    allocator: CapitalAllocator
    execution: AtomicExecutionEngine
    strategies: StrategyRunner
    circuit_breaker: ExchangeCircuitBreaker = field(default_factory=ExchangeCircuitBreaker)
    _cycle_count: int = 0
    _symbol_cooldown_until: dict[str, float] = field(default_factory=dict)
    _last_position_monitor_log_ts: dict[str, float] = field(default_factory=dict)
    _balance_synced: bool = False
    _position_close_failures: dict[str, int] = field(default_factory=dict)
    _prev_balances: dict[str, float] = field(default_factory=dict)
    _unstable_exchanges: dict[str, float] = field(default_factory=dict)  # exchange -> cooldown_until
    _margin_rejected_exchanges: dict[str, float] = field(default_factory=dict)  # exchange -> cooldown_until

    @classmethod
    def create(
        cls,
        config: TradingSystemConfig,
        provider: MarketDataProvider,
        monitor: MonitoringSink,
        execution: AtomicExecutionEngine,
        state: SystemState,
    ) -> "TradingSystemEngine":
        risk = RiskEngine(config.risk, state)
        allocator = CapitalAllocator(config.risk)
        market_data = getattr(provider, "market_data", None)
        runner = StrategyRunner(strategies=build_strategies(config, market_data=market_data), monitor=monitor)
        return cls(
            config=config,
            provider=provider,
            monitor=monitor,
            risk=risk,
            allocator=allocator,
            execution=execution,
            strategies=runner,
        )

    async def run_forever(self) -> None:
        self.config.validate()
        # Fix #3: cancel orphaned orders from previous crash
        if hasattr(self.execution.venue, "cancel_orphaned_orders"):
            try:
                await self.execution.venue.cancel_orphaned_orders(self.config.symbols)
            except Exception as exc:
                logger.warning("orphan_cleanup_failed: %s", exc)
        # Fix #8: scan for orphaned POSITIONS (not just orders) on all exchanges.
        # If an exchange has open contracts that we don't track, it means margin is
        # locked and any new trade will fail with "Insufficient margin".
        await self._scan_orphaned_positions()
        # FIX #5: Log any positions restored from disk on startup.
        await self._log_restored_positions()
        while True:
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("engine_cycle_error: %s", exc, exc_info=True)
                await self.monitor.emit("engine_cycle_error", {"error": str(exc)})
            await asyncio.sleep(self.config.execution.cycle_interval_seconds)

    async def run_cycle(self) -> None:
        self._cycle_count += 1
        cycle_start = time.time()
        # Fix #4: sync balance from exchange on first cycle
        if not self._balance_synced:
            await self._sync_balance_on_startup()
            self._balance_synced = True
        state_snapshot = await self.risk.state.snapshot()
        try:
            api_health = await asyncio.wait_for(self.provider.health(), timeout=10.0)
        except asyncio.TimeoutError:
            logger.warning("provider.health() timed out after 10s, using default latency")
            api_health = {}
        latency_ms = max(api_health.values(), default=0.0)
        await self._process_open_positions()
        opened_in_cycle = 0
        intents_seen = 0
        risk_rejects = 0
        execution_attempts = 0
        execution_success = 0
        for symbol in self.config.symbols:
            now = time.time()
            if self._symbol_cooldown_until.get(symbol, 0.0) > now:
                continue
            snapshot = await self.provider.get_snapshot(symbol)
            now = time.time()
            max_ob_age = 0.0
            for ob in list(snapshot.orderbooks.values()) + list(snapshot.spot_orderbooks.values()):
                max_ob_age = max(max_ob_age, now - ob.timestamp)
            await self.monitor.emit(
                "data_freshness",
                {"symbol": symbol, "max_orderbook_age_sec": round(max_ob_age, 3)},
            )
            if await self.risk.state.kill_switch_triggered():
                await self.monitor.emit("kill_switch", {"symbol": symbol, "message": "engine_paused"})
                return
            allocation = self.allocator.allocate(
                equity=state_snapshot["equity"],
                avg_funding_bps=max(snapshot.funding_rates.values(), default=0.0) * 10_000,
                volatility_regime=snapshot.volatility,
                trend_strength=snapshot.trend_strength,
                enabled=[s.strategy_id for s in self.strategies.strategies],
            )
            logger.info(f"[ALLOCATION] symbol={symbol} equity={state_snapshot['equity']:.2f} alloc={allocation.strategy_allocations}")
            selected_ids = self._select_strategy_ids(snapshot)
            available_ids = {s.strategy_id for s in self.strategies.strategies}
            logger.info(
                f"[STRATEGY_SELECT] symbol={symbol} "
                f"available={[x.value for x in available_ids]} "
                f"selected={[x.value for x in selected_ids]} "
                f"vol={snapshot.volatility:.4f} trend={snapshot.trend_strength:.4f} "
                f"funding_spread_bps={abs(snapshot.indicators.get('funding_spread_bps', 0)):.2f} "
                f"basis_bps={abs(snapshot.indicators.get('basis_bps', 0)):.2f} "
                f"spread_bps={snapshot.indicators.get('spread_bps', 0):.2f} "
                f"balances={snapshot.balances}"
            )
            await self.monitor.emit(
                "strategy_selection",
                {
                    "symbol": symbol,
                    "selected": [x.value for x in selected_ids],
                    "auto": True,
                },
            )
            # Filter out exchanges with insufficient balance from orderbooks
            # so the strategy never generates intents involving underfunded exchanges.
            # This prevents the costly "first leg fills → second leg rejects → hedge back" cycle.
            tradeable_snapshot = self._filter_underfunded_exchanges(snapshot, symbol)
            if len(tradeable_snapshot.orderbooks) < 2:
                logger.debug(
                    "[SKIP_UNDERFUNDED] symbol=%s only %d funded exchanges, need 2",
                    symbol, len(tradeable_snapshot.orderbooks),
                )
                continue
            intents = await self.strategies.generate_intents(tradeable_snapshot, enabled_ids=selected_ids)
            intents.sort(key=lambda x: x.expected_edge_bps * x.confidence, reverse=True)
            intents_seen += len(intents)

            if intents:
                logger.info(f"[INTENTS] symbol={symbol} count={len(intents)}")
                for i, intent in enumerate(intents):
                    logger.info(f"[INTENT] {i}: strategy={intent.strategy_id.value} edge_bps={intent.expected_edge_bps:.2f} confidence={intent.confidence:.2f} long={intent.long_exchange} short={intent.short_exchange}")
            else:
                funding_str = ", ".join(f"{k}={v*10000:.2f}bps" for k, v in snapshot.funding_rates.items())
                ob_str = ", ".join(f"{k}: bid={ob.bid:.2f} ask={ob.ask:.2f}" for k, ob in snapshot.orderbooks.items())
                logger.info(
                    f"[NO_INTENTS] symbol={symbol} "
                    f"funding=[{funding_str}] "
                    f"funding_spread_bps={abs(snapshot.indicators.get('funding_spread_bps', 0)):.2f} "
                    f"vol={snapshot.volatility:.4f} trend={snapshot.trend_strength:.4f} "
                    f"rsi={snapshot.indicators.get('rsi', 0):.1f} "
                    f"orderbooks=[{ob_str}] "
                    f"balances={snapshot.balances}"
                )
            for intent in intents:
                if opened_in_cycle >= self.config.execution.max_new_positions_per_cycle:
                    break
                # Fix #1: skip intent if either exchange is circuit-broken
                if not self.circuit_breaker.is_available(intent.long_exchange):
                    logger.info("[CIRCUIT_BREAKER] skipping %s — %s tripped", symbol, intent.long_exchange)
                    continue
                if not self.circuit_breaker.is_available(intent.short_exchange):
                    logger.info("[CIRCUIT_BREAKER] skipping %s — %s tripped", symbol, intent.short_exchange)
                    continue
                alloc_cap = allocation.strategy_allocations.get(intent.strategy_id, 0.0)
                # FIX #9: Cache env var at startup via a class-level default (module-level constant).
                # Reading os.getenv in the hot loop is wasteful and prevents runtime changes from
                # being visible without restart.  For backwards compat we still read from env here
                # but the value is cached in a module-level constant after first read.
                max_equity_pct = _MAX_EQUITY_PER_TRADE_PCT
                equity_cap = state_snapshot["equity"] * max_equity_pct
                proposed_notional = min(alloc_cap, equity_cap) if alloc_cap > 0 else equity_cap
                # Ensure proposed_notional is at least the exchange minimum notional
                # so small accounts can still place one-contract orders.
                min_notional_hint = self._get_min_notional(intent)
                min_notional_override = False
                if proposed_notional < min_notional_hint and equity_cap >= min_notional_hint:
                    # Allow override to exchange minimum, but never exceed
                    # the allocation cap to prevent over-leveraging.
                    if alloc_cap > 0:
                        proposed_notional = min(min_notional_hint, alloc_cap)
                    else:
                        proposed_notional = min_notional_hint
                    min_notional_override = proposed_notional >= min_notional_hint
                if intent.metadata.get("legs"):
                    proposed_notional = min(
                        proposed_notional,
                        float(intent.metadata.get("notional_usd", proposed_notional) or proposed_notional),
                    )
                if proposed_notional <= 0:
                    continue
                estimated_slippage_bps = self.execution.slippage.estimate(
                    order_notional_usd=proposed_notional,
                    average_book_depth_usd=2_000_000,
                    volatility=snapshot.volatility,
                    latency_ms=latency_ms,
                )
                logger.info(
                    f"[RISK_PRE] symbol={symbol} strategy={intent.strategy_id.value} "
                    f"notional={proposed_notional:.2f} alloc_cap={alloc_cap:.2f} "
                    f"min_notional_hint={min_notional_hint:.2f} "
                    f"min_notional_override={min_notional_override} "
                    f"est_slippage_bps={estimated_slippage_bps:.2f} "
                    f"max_slippage_bps={self.config.risk.max_order_slippage_bps:.1f} "
                    f"latency_ms={latency_ms:.0f} vol={snapshot.volatility:.6f}"
                )
                risk_decision = await self.risk.validate_intent(
                    intent=intent,
                    allocation_plan=allocation,
                    proposed_notional=proposed_notional,
                    estimated_slippage_bps=estimated_slippage_bps,
                    leverage=1.0,
                    api_latency_ms=latency_ms,
                    snapshot=snapshot,
                    min_notional_override=min_notional_override,
                )
                if not risk_decision.approved:
                    risk_rejects += 1
                    logger.warning(f"[RISK_REJECT] symbol={symbol} strategy={intent.strategy_id.value} reason={risk_decision.reason}")
                    await self.monitor.emit(
                        "risk_reject",
                        {"symbol": symbol, "strategy": intent.strategy_id.value, "reason": risk_decision.reason},
                    )
                    continue
                
                logger.info(f"[EXECUTE] symbol={symbol} strategy={intent.strategy_id.value} notional={proposed_notional:.2f}")
                execution_attempts += 1
                if intent.metadata.get("legs"):
                    report = await self.execution.execute_multi_leg_spot(intent=intent)
                else:
                    report = await self.execution.execute_dual_entry(
                        intent=intent,
                        notional_usd=proposed_notional,
                        est_book_depth_usd=2_000_000,
                        volatility=snapshot.volatility,
                        latency_ms=latency_ms,
                    )
                await self.monitor.emit(
                    "execution_result",
                    {"symbol": symbol, "strategy": intent.strategy_id.value, "success": report.success, "message": report.message},
                )
                if not report.success and report.message == "first_leg_failed":
                    # Only record error on the exchange that actually rejected.
                    # We can't know which one, so record on the less reliable one.
                    reliability_rank = self.config.execution.reliability_rank
                    less_reliable = max(
                        [intent.long_exchange, intent.short_exchange],
                        key=lambda ex: reliability_rank.get(ex, 99),
                    )
                    self.circuit_breaker.record_error(less_reliable, "first_leg_failed")
                    # Avoid hammering the same unaffordable/rejected symbol every cycle.
                    self._symbol_cooldown_until[symbol] = max(
                        self._symbol_cooldown_until.get(symbol, 0.0),
                        time.time() + 300,
                    )
                if not report.success and report.message == "second_leg_failed":
                    # Record error on second exchange
                    self.circuit_breaker.record_error(intent.short_exchange, "second_leg_failed")
                    # Cooldown noisy symbols with repeated dual-leg failures.
                    self._symbol_cooldown_until[symbol] = time.time() + 120
                    # If second leg was rejected (not just unfilled), the exchange
                    # likely has a margin or API issue.  Block the EXCHANGE entirely
                    # for 30 minutes to prevent repeated costly hedge cycles.
                    # Determine which exchange was the second leg:
                    reliability_rank = self.config.execution.reliability_rank
                    first_leg = min(
                        [intent.long_exchange, intent.short_exchange],
                        key=lambda ex: reliability_rank.get(ex, 99),
                    )
                    second_leg_ex = intent.short_exchange if first_leg == intent.long_exchange else intent.long_exchange
                    cooldown_seconds = _MARGIN_REJECT_COOLDOWN_SECONDS
                    self._margin_rejected_exchanges[second_leg_ex] = time.time() + cooldown_seconds
                    logger.warning(
                        "[MARGIN_REJECT_COOLDOWN] %s blocked for %d min after second_leg_failed on %s",
                        second_leg_ex, cooldown_seconds // 60, symbol,
                    )
                if not report.success and report.message == "second_leg_failed" and not report.hedged:
                    await self.monitor.emit(
                        "execution_critical",
                        {
                            "symbol": symbol,
                            "strategy": intent.strategy_id.value,
                            "reason": "unverified_hedge_after_second_leg_failure",
                        },
                    )
                    # FIX CRITICAL #2: Blacklist ONLY this symbol — do NOT kill all trading.
                    # Previous code called trigger_kill_switch(permanent=False) which paused
                    # ALL symbols globally for a single pair's failure.
                    self._symbol_cooldown_until[symbol] = time.time() + 3600  # 1 hour
                    # Also block the less reliable exchange for this symbol pair.
                    reliability_rank = self.config.execution.reliability_rank
                    worse_ex = max(
                        [intent.long_exchange, intent.short_exchange],
                        key=lambda ex: reliability_rank.get(ex, 99),
                    )
                    margin_cooldown = _MARGIN_REJECT_COOLDOWN_SECONDS
                    self._margin_rejected_exchanges[worse_ex] = time.time() + margin_cooldown
                    logger.warning(
                        "[SYMBOL_BLACKLIST] %s: unverified hedge, symbol blacklisted 1h, "
                        "exchange %s cooled down %ds. Other symbols continue trading.",
                        symbol, worse_ex, margin_cooldown,
                    )
                    continue
                if report.success:
                    self.circuit_breaker.record_success(intent.long_exchange)
                    self.circuit_breaker.record_success(intent.short_exchange)
                    exp_long = float(intent.metadata.get("long_price", 0.0) or 0.0)
                    exp_short = float(intent.metadata.get("short_price", 0.0) or 0.0)
                    if exp_long > 0 and exp_short > 0 and report.fill_price_long > 0 and report.fill_price_short > 0:
                        slip_long = max(0.0, (report.fill_price_long - exp_long) / exp_long * 10_000)
                        slip_short = max(0.0, (exp_short - report.fill_price_short) / exp_short * 10_000)
                        realized_slip = slip_long + slip_short
                        if realized_slip > self.config.risk.max_realized_slippage_bps:
                            await self.monitor.emit(
                                "realized_slippage_limit",
                                {"symbol": symbol, "strategy": intent.strategy_id.value, "slippage_bps": realized_slip},
                            )
                            await self.risk.state.trigger_kill_switch(permanent=False)
                            return
                    execution_success += 1
                    opened_in_cycle += 1
        if self._cycle_count % 20 == 0:
            cycle_ms = (time.time() - cycle_start) * 1000
            logger.info(
                "engine_heartbeat cycle=%s symbols=%s intents=%s risk_rejects=%s exec_attempts=%s exec_success=%s latency_ms=%.1f cycle_ms=%.1f",
                self._cycle_count,
                len(self.config.symbols),
                intents_seen,
                risk_rejects,
                execution_attempts,
                execution_success,
                latency_ms,
                cycle_ms,
            )

    async def _process_open_positions(self) -> None:
        # FIX CRITICAL #8: Use module-level cached constants instead of per-cycle os.getenv() calls.
        take_profit_usd = _EXIT_TAKE_PROFIT_USD
        max_holding_seconds = _EXIT_MAX_HOLD_SECONDS
        close_edge_bps = _EXIT_CLOSE_EDGE_BPS
        monitor_log_interval_sec = _POSITION_MONITOR_LOG_INTERVAL_SEC
        loss_streak_limit = _LOSS_STREAK_LIMIT
        loss_streak_cooldown_seconds = _LOSS_STREAK_COOLDOWN_SECONDS

        positions = await self.risk.state.list_positions()
        now = time.time()
        for pos in positions:
            try:
                snapshot = await self.provider.get_snapshot(pos.symbol)
                long_ob = snapshot.orderbooks.get(pos.long_exchange)
                short_ob = snapshot.orderbooks.get(pos.short_exchange)
                if not long_ob or not short_ob:
                    continue
                max_ob_age = self.config.risk.max_orderbook_age_sec or 10.0
                if now - long_ob.timestamp > max_ob_age or now - short_ob.timestamp > max_ob_age:
                    continue

                # Use executable prices for realistic PnL: sell long at bid, buy back short at ask.
                long_exit_px = long_ob.bid
                short_exit_px = short_ob.ask
                entry_long = float(pos.metadata.get("entry_long_price", long_exit_px) or long_exit_px)
                entry_short = float(pos.metadata.get("entry_short_price", short_exit_px) or short_exit_px)

                # Approx mark-to-market PnL in quote currency on each leg.
                long_pnl = ((long_exit_px - entry_long) / max(entry_long, 1e-9)) * pos.notional_usd
                short_pnl = ((entry_short - short_exit_px) / max(entry_short, 1e-9)) * pos.notional_usd

                # Exit fee estimation — only the EXIT portion (entry fees are already
                # baked into our fill prices).  We use actual per-leg taker rates from
                # the snapshot when available, falling back to 0.05% per leg.
                long_fee_pct = 0.05
                short_fee_pct = 0.05
                fee_data_long = snapshot.fee_bps.get(pos.long_exchange, {})
                fee_data_short = snapshot.fee_bps.get(pos.short_exchange, {})
                if "perp" in fee_data_long:
                    val = abs(float(fee_data_long["perp"]))
                    if val > 0:
                        long_fee_pct = val / 100  # bps → pct
                if "perp" in fee_data_short:
                    val = abs(float(fee_data_short["perp"]))
                    if val > 0:
                        short_fee_pct = val / 100  # bps → pct
                estimated_exit_fees = pos.notional_usd * (long_fee_pct + short_fee_pct) / 100.0

                # Compute age_sec FIRST — needed by both PnL and exit logic below.
                age_sec = now - pos.opened_at

                # --- Funding cost/income estimation ---
                # For perpetual futures, funding payments occur every 8h.
                # Long pays funding if rate > 0, short receives it (and vice versa).
                # Estimate accrued funding based on current rates and holding time.
                funding_pnl = 0.0
                fr_long = snapshot.funding_rates.get(pos.long_exchange)
                fr_short = snapshot.funding_rates.get(pos.short_exchange)
                if fr_long is not None and fr_short is not None:
                    funding_interval_sec = 28800.0  # 8 hours
                    periods_held = age_sec / funding_interval_sec
                    # Long position pays funding_rate * notional per period
                    # Short position receives funding_rate * notional per period
                    funding_cost_long = fr_long * pos.notional_usd * periods_held
                    funding_income_short = fr_short * pos.notional_usd * periods_held
                    # Net: short receives, long pays (when rates are positive)
                    funding_pnl = funding_income_short - funding_cost_long

                pnl_usd = long_pnl + short_pnl - estimated_exit_fees + funding_pnl
                # Remaining edge: how profitable is closing NOW?
                # We exit by selling long @ bid, buying back short @ ask.
                # Positive edge = we'd still profit, negative = underwater.
                mid_ref = max((long_ob.bid + short_ob.ask) / 2, 1e-9)
                edge_bps = ((long_ob.bid - short_ob.ask) / mid_ref) * 10_000

                last_log = self._last_position_monitor_log_ts.get(pos.position_id, 0.0)
                if now - last_log >= monitor_log_interval_sec:
                    self._last_position_monitor_log_ts[pos.position_id] = now
                    await self.monitor.emit(
                        "position_monitor",
                        {
                            "position_id": pos.position_id,
                            "symbol": pos.symbol,
                            "strategy": pos.strategy_id.value,
                            "age_sec": int(age_sec),
                            "pnl_usd": round(pnl_usd, 6),
                            "funding_pnl": round(funding_pnl, 6),
                            "edge_bps": round(edge_bps, 3),
                        },
                    )

                # Strategy-specific overrides from intent metadata.
                # Arbitrage is market-neutral: we do NOT use PnL-based stop-loss
                # because PnL swings are spread noise, not directional risk.
                # Exits: spread convergence (TP), timeout, funding reversal, or
                # emergency per-trade max loss (anomaly protection only).
                tp_pct = float(pos.metadata.get("take_profit_pct", 0) or 0)
                if tp_pct > 0:
                    tp_local = tp_pct * pos.notional_usd
                else:
                    tp_local = float(pos.metadata.get("take_profit_usd", take_profit_usd) or take_profit_usd)
                max_hold_local = int(pos.metadata.get("max_holding_seconds", max_holding_seconds) or max_holding_seconds)
                close_edge_local = float(pos.metadata.get("close_edge_bps", close_edge_bps) or close_edge_bps)

                # Emergency safety: per-trade max loss — only for anomalies
                # (exchange failure, delisting, extreme divergence).
                # Deliberately set high to avoid cutting normal spread noise.
                equity = (await self.risk.state.snapshot())["equity"]
                max_trade_loss = equity * self.config.risk.max_loss_per_trade_pct
                close_reason = None
                if pnl_usd <= -max_trade_loss:
                    close_reason = "per_trade_max_loss"
                    await self.monitor.emit(
                        "per_trade_max_loss",
                        {"position_id": pos.position_id, "pnl_usd": round(pnl_usd, 6), "limit_usd": round(max_trade_loss, 6)},
                    )
                    # Temporary pause (cooldown), not permanent — single trade loss
                    # is expected noise in arb, permanent kill is for critical failures only.
                    await self.risk.state.trigger_kill_switch(permanent=False)
                elif pnl_usd >= tp_local:
                    close_reason = "take_profit"
                elif age_sec >= max_hold_local:
                    close_reason = "max_holding_time"
                elif edge_bps <= close_edge_local:
                    close_reason = "edge_converged"

                # Funding arb: exit if funding rate advantage has reversed
                if not close_reason and pos.metadata.get("arb_type") == "funding_rate":
                    fr_long = snapshot.funding_rates.get(pos.long_exchange)
                    fr_short = snapshot.funding_rates.get(pos.short_exchange)
                    if fr_long is not None and fr_short is not None:
                        entry_diff = float(pos.metadata.get("funding_rate_diff_pct", 0) or 0)
                        current_diff = (fr_short * 100 - fr_long * 100)  # short should be higher
                        if entry_diff > 0 and current_diff < entry_diff * 0.3:
                            close_reason = "funding_reversed"
                            await self.monitor.emit(
                                "funding_reversed",
                                {
                                    "position_id": pos.position_id,
                                    "entry_diff_pct": round(entry_diff, 4),
                                    "current_diff_pct": round(current_diff, 4),
                                },
                            )

                if not close_reason:
                    continue

                await self.monitor.emit(
                    "position_close_signal",
                    {
                        "position_id": pos.position_id,
                        "symbol": pos.symbol,
                        "reason": close_reason,
                        "pnl_usd": round(pnl_usd, 6),
                        "age_sec": int(age_sec),
                        "edge_bps": round(edge_bps, 3),
                    },
                )
                closed = await self.execution.execute_dual_exit(pos, close_reason)
                if not closed:
                    # Track consecutive close failures per position
                    fail_count = self._position_close_failures.get(pos.position_id, 0) + 1
                    self._position_close_failures[pos.position_id] = fail_count
                    await self.monitor.emit(
                        "position_close_failed",
                        {"position_id": pos.position_id, "symbol": pos.symbol, "reason": close_reason, "fail_count": fail_count},
                    )
                    # After 3 consecutive failures, verify if position actually exists on exchange.
                    # If no real contracts found → phantom position from crash/test data. Remove it.
                    if fail_count >= 3:
                        is_phantom = await self._check_phantom_position(pos)
                        if is_phantom:
                            logger.warning(
                                "phantom_position_removed: %s %s (no real contracts on %s/%s after %d close failures)",
                                pos.position_id, pos.symbol, pos.long_exchange, pos.short_exchange, fail_count,
                            )
                            await self.risk.state.remove_position(pos.position_id)
                            self._position_close_failures.pop(pos.position_id, None)
                            await self.monitor.emit(
                                "phantom_position_removed",
                                {"position_id": pos.position_id, "symbol": pos.symbol},
                            )
                    continue

                self._position_close_failures.pop(pos.position_id, None)
                removed = await self.risk.state.remove_position(pos.position_id)
                if removed:
                    realized_pnl_usd = await self._compute_realized_pnl_from_balances(
                        removed,
                        fallback_pnl_usd=pnl_usd,
                    )
                    await self.risk.state.apply_realized_pnl(realized_pnl_usd)
                    symbol = removed.symbol
                    if realized_pnl_usd < 0:
                        streak = self._symbol_loss_streak.get(symbol, 0) + 1
                        self._symbol_loss_streak[symbol] = streak
                        if streak >= loss_streak_limit:
                            cooldown_until = now + loss_streak_cooldown_seconds
                            self._symbol_cooldown_until[symbol] = max(
                                self._symbol_cooldown_until.get(symbol, 0.0), cooldown_until
                            )
                            await self.monitor.emit(
                                "symbol_cooldown",
                                {
                                    "symbol": symbol,
                                    "reason": "loss_streak_limit_reached",
                                    "loss_streak": streak,
                                    "cooldown_seconds": loss_streak_cooldown_seconds,
                                },
                            )
                            self._symbol_loss_streak[symbol] = 0
                    else:
                        self._symbol_loss_streak[symbol] = 0
                await self.monitor.emit(
                    "position_closed",
                    {
                        "position_id": pos.position_id,
                        "symbol": pos.symbol,
                        "reason": close_reason,
                        "realized_pnl_usd": round(
                            realized_pnl_usd if removed else pnl_usd,
                            6,
                        ),
                    },
                )
            except Exception as exc:
                await self.monitor.emit(
                    "position_monitor_error",
                    {"position_id": pos.position_id, "symbol": pos.symbol, "error": str(exc)},
                )

    async def _compute_realized_pnl_from_balances(self, pos, fallback_pnl_usd: float) -> float:
        """Compute realized PnL as the sum of balance deltas on both exchanges.

        Falls back to mark-to-market PnL when balance method is unreliable
        (multiple concurrent positions would pollute the delta).
        """
        # If other positions are open on the same exchanges, balance deltas
        # include their P&L + funding, making the result inaccurate.
        other_positions = await self.risk.state.list_positions()
        same_exchange_count = sum(
            1 for p in other_positions
            if p.position_id != pos.position_id
            and (p.long_exchange in (pos.long_exchange, pos.short_exchange)
                 or p.short_exchange in (pos.long_exchange, pos.short_exchange))
        )
        if same_exchange_count > 0:
            return fallback_pnl_usd

        try:
            balances_now = await self.execution.venue.get_balances()
            long_ex = pos.long_exchange
            short_ex = pos.short_exchange

            key_long = f"balance_entry_{long_ex}"
            key_short = f"balance_entry_{short_ex}"
            if key_long not in pos.metadata or key_short not in pos.metadata:
                return fallback_pnl_usd

            long_before = float(pos.metadata.get(key_long, 0.0) or 0.0)
            short_before = float(pos.metadata.get(key_short, 0.0) or 0.0)
            long_after = float(balances_now.get(long_ex, long_before) or long_before)
            short_after = float(balances_now.get(short_ex, short_before) or short_before)

            delta_long = long_after - long_before
            delta_short = short_after - short_before
            return float(delta_long + delta_short)
        except Exception:
            return fallback_pnl_usd

    def _select_strategy_ids(self, snapshot) -> set[StrategyId]:
        """All available strategies are always enabled (single strategy mode)."""
        return {s.strategy_id for s in self.strategies.strategies}

    async def _sync_balance_on_startup(self) -> None:
        """Fix #4: Fetch fresh balance from exchanges before first trade."""
        try:
            balances = await self.execution.venue.get_balances()
            if balances:
                # Only sync if we have data for ALL configured exchanges.
                missing = [ex for ex in self.config.exchanges if ex not in balances or balances[ex] < 0]
                if missing:
                    logger.warning("balance_sync: missing data for %s, keeping configured equity", missing)
                    return
                total = sum(balances.values())
                if total > 0:
                    old_equity = (await self.risk.state.snapshot())["equity"]
                    if abs(total - old_equity) / max(old_equity, 1.0) > 0.01:
                        logger.warning(
                            "balance_sync: equity adjusted %.2f -> %.2f (exchange balances: %s)",
                            old_equity, total, balances,
                        )
                        await self.risk.state.set_equity(total)
                    else:
                        logger.info("balance_sync: equity confirmed at %.2f", total)
                else:
                    logger.warning("balance_sync: total balance is 0, keeping configured equity")
            else:
                logger.warning("balance_sync: failed to fetch balances, keeping configured equity")
        except Exception as exc:
            logger.error("balance_sync: error fetching balances: %s", exc)

    def _filter_underfunded_exchanges(self, snapshot, symbol: str):
        """Remove exchanges from snapshot where balance < min notional or balance is unstable.

        Returns a new MarketSnapshot with only funded+stable exchanges in orderbooks.
        This prevents the strategy from generating intents that will fail on
        the second leg due to insufficient margin.
        """
        from arbitrage.system.models import MarketSnapshot
        balances = snapshot.balances or {}
        if not balances:
            return snapshot

        # Detect balance instability: if balance changed >80% since last check,
        # mark exchange as unstable for 5 minutes.  This catches cases where
        # HTX API intermittently reports wrong balances.
        now = time.time()
        for exchange, current_bal in balances.items():
            prev_bal = self._prev_balances.get(exchange)
            if prev_bal is not None and prev_bal > 0 and current_bal > 0:
                change_pct = abs(current_bal - prev_bal) / max(prev_bal, current_bal)
                if change_pct > 0.80:
                    logger.warning(
                        "[BALANCE_UNSTABLE] %s: %.2f → %.2f (%.0f%% change), cooling down 5min",
                        exchange, prev_bal, current_bal, change_pct * 100,
                    )
                    self._unstable_exchanges[exchange] = now + 300  # 5 min cooldown
            self._prev_balances[exchange] = current_bal

        funded_exchanges = set()
        for exchange in snapshot.orderbooks:
            # Skip exchanges with unstable balance
            if self._unstable_exchanges.get(exchange, 0) > now:
                logger.debug("[UNSTABLE_SKIP] %s on %s: balance unstable, in cooldown", symbol, exchange)
                continue
            # Skip exchanges that recently had margin rejections
            if self._margin_rejected_exchanges.get(exchange, 0) > now:
                logger.debug("[MARGIN_REJECT_SKIP] %s on %s: margin rejected, in cooldown", symbol, exchange)
                continue

            avail = balances.get(exchange, 0.0)
            # Get minimum notional for this exchange+symbol
            min_notional = self._get_min_notional_for_exchange(exchange, symbol)
            # Also apply safety buffer: need at least min_notional + reserve
            buf_pct = float(os.getenv("EXEC_MARGIN_SAFETY_BUFFER_PCT", "0.05"))
            reserve = float(os.getenv("EXEC_MARGIN_SAFETY_RESERVE_USD", "0.50"))
            needed = min_notional + reserve + (min_notional * buf_pct)
            if avail >= needed:
                funded_exchanges.add(exchange)
            else:
                logger.debug(
                    "[UNDERFUNDED] %s on %s: balance=%.2f needed=%.2f (min_notional=%.2f)",
                    symbol, exchange, avail, needed, min_notional,
                )

        if funded_exchanges == set(snapshot.orderbooks.keys()):
            return snapshot  # All exchanges funded, no filtering needed

        filtered_obs = {ex: ob for ex, ob in snapshot.orderbooks.items() if ex in funded_exchanges}
        filtered_spot = {ex: ob for ex, ob in snapshot.spot_orderbooks.items() if ex in funded_exchanges}
        filtered_depth = {ex: d for ex, d in snapshot.orderbook_depth.items() if ex in funded_exchanges}
        filtered_spot_depth = {ex: d for ex, d in snapshot.spot_orderbook_depth.items() if ex in funded_exchanges}
        filtered_funding = {ex: r for ex, r in snapshot.funding_rates.items() if ex in funded_exchanges}
        filtered_fees = {ex: f for ex, f in snapshot.fee_bps.items() if ex in funded_exchanges}

        return replace(
            snapshot,
            orderbooks=filtered_obs,
            spot_orderbooks=filtered_spot,
            orderbook_depth=filtered_depth,
            spot_orderbook_depth=filtered_spot_depth,
            funding_rates=filtered_funding,
            fee_bps=filtered_fees,
        )

    def _get_min_notional_for_exchange(self, exchange: str, symbol: str) -> float:
        """Get minimum notional USD for a given exchange+symbol."""
        market_data = getattr(self.provider, "market_data", None)
        if not market_data:
            return 5.0
        ticker = market_data.get_futures_price(exchange, symbol)
        if not ticker:
            return 5.0
        px = (ticker.bid + ticker.ask) / 2
        ct = market_data.get_contract_size(exchange, symbol)
        if px <= 0 or ct <= 0:
            return 5.0
        if exchange in ("bybit", "binance"):
            return max(5.0, px * ct)
        # OKX/HTX: one contract minimum
        return max(1.0, px * ct)

    async def _scan_orphaned_positions(self) -> None:
        """Fix #8: At startup, scan all exchanges for open positions not tracked in state.

        If an exchange has open contracts that aren't in our tracked positions,
        it means margin is locked by an untracked position. Block that exchange
        from trading until the position is resolved (closed manually or by us).
        """
        if not hasattr(self.execution.venue, "open_contracts"):
            logger.info("orphan_position_scan: venue has no open_contracts method, skipping")
            return

        tracked_positions = await self.risk.state.list_positions()
        # Build a set of (exchange, symbol) pairs that we track
        tracked_pairs: set[tuple[str, str]] = set()
        for pos in tracked_positions:
            tracked_pairs.add((pos.long_exchange, pos.symbol))
            tracked_pairs.add((pos.short_exchange, pos.symbol))

        venue = self.execution.venue
        exchange_names = list(venue.exchanges.keys()) if hasattr(venue, "exchanges") else []
        if not exchange_names:
            logger.info("orphan_position_scan: no exchanges configured, skipping")
            return

        orphaned_found = []
        for exchange in exchange_names:
            for symbol in self.config.symbols:
                try:
                    contracts = await venue.open_contracts(exchange, symbol)
                    if contracts > 0:
                        if (exchange, symbol) not in tracked_pairs:
                            orphaned_found.append((exchange, symbol, contracts))
                            logger.warning(
                                "[ORPHAN_POSITION] %s has %.4f open contracts on %s "
                                "NOT tracked in state — margin is locked! "
                                "Blocking exchange for 60 min.",
                                exchange, contracts, symbol,
                            )
                            # Block this exchange from trading for 60 minutes
                            self._margin_rejected_exchanges[exchange] = time.time() + 3600
                        else:
                            logger.info(
                                "orphan_position_scan: %s/%s has %.4f contracts (tracked, OK)",
                                exchange, symbol, contracts,
                            )
                except Exception as exc:
                    logger.warning("orphan_position_scan: error checking %s/%s: %s", exchange, symbol, exc)

        if orphaned_found:
            logger.error(
                "[ORPHAN_POSITION_SUMMARY] Found %d orphaned positions: %s. "
                "These exchanges are blocked from trading. "
                "Close positions manually or restart after closing.",
                len(orphaned_found),
                [(ex, sym, f"{ct:.4f}") for ex, sym, ct in orphaned_found],
            )
            await self.monitor.emit("orphan_positions_detected", {
                "count": len(orphaned_found),
                "details": [{"exchange": ex, "symbol": sym, "contracts": ct} for ex, sym, ct in orphaned_found],
            })
        else:
            logger.info("orphan_position_scan: no orphaned positions found, all clear")

    async def _orphan_monitor_loop(self) -> None:
        """FIX CRITICAL #1: Background loop that periodically scans for orphaned
        positions on all exchanges.  Startup scan (_scan_orphaned_positions) only
        runs once; if an API glitch creates an orphaned position later, this loop
        will detect and block the exchange.

        Runs every 10 minutes by default.
        """
        interval_seconds = max(300, int(float(os.getenv("ORPHAN_MONITOR_INTERVAL_SECONDS", "600"))))
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                # FIX CRITICAL #3: Also deduplicate positions before scanning.
                await self._deduplicate_positions()
                # Run a lightweight orphan scan (only exchanges & symbols configured)
                if not hasattr(self.execution.venue, "open_contracts"):
                    continue
                tracked_positions = await self.risk.state.list_positions()
                tracked_pairs: set[tuple[str, str]] = set()
                for pos in tracked_positions:
                    tracked_pairs.add((pos.long_exchange, pos.symbol))
                    tracked_pairs.add((pos.short_exchange, pos.symbol))
                venue = self.execution.venue
                exchange_names = list(venue.exchanges.keys()) if hasattr(venue, "exchanges") else []
                for exchange in exchange_names:
                    for symbol in self.config.symbols:
                        try:
                            contracts = await venue.open_contracts(exchange, symbol)
                            if contracts > 0 and (exchange, symbol) not in tracked_pairs:
                                logger.warning(
                                    "[ORPHAN_MONITOR] Background scan detected %.4f untracked contracts on %s/%s "
                                    "— blocking exchange for 60 min",
                                    contracts, exchange, symbol,
                                )
                                self._margin_rejected_exchanges[exchange] = time.time() + 3600
                                await self.monitor.emit("orphan_position_detected", {
                                    "exchange": exchange,
                                    "symbol": symbol,
                                    "contracts": contracts,
                                    "action": "exchange_blocked_60min",
                                })
                        except Exception:
                            pass
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("orphan_monitor_loop error: %s", exc, exc_info=True)

    async def _deduplicate_positions(self) -> None:
        """FIX CRITICAL #3: Remove duplicate positions that share the same
        (strategy, symbol, long_exchange, short_exchange) key.  Only the earliest
        opened position is kept; later duplicates are purged without closing.
        This prevents risk engine from allowing doubled positions on the same pair.
        """
        try:
            positions = await self.risk.state.list_positions()
            seen: Dict[str, OpenPosition] = {}
            duplicates: list[str] = []
            for pos in positions:
                dedup_key = f"{pos.strategy_id.value}:{pos.symbol}:{pos.long_exchange}:{pos.short_exchange}"
                if dedup_key in seen:
                    # Keep the older (first opened) position
                    existing = seen[dedup_key]
                    if pos.opened_at < existing.opened_at:
                        seen[dedup_key] = pos
                        duplicates.append(existing.position_id)
                    else:
                        duplicates.append(pos.position_id)
                    logger.warning(
                        "[DEDUP_POSITION] Removing duplicate position %s (%s %s) — "
                        "opened_at=%s, existing=%s",
                        pos.position_id, pos.symbol, pos.strategy_id.value,
                        pos.opened_at, existing.position_id,
                    )
                else:
                    seen[dedup_key] = pos

            for dup_id in duplicates:
                removed = await self.risk.state.remove_position(dup_id)
                if removed:
                    await self.monitor.emit("duplicate_position_removed", {
                        "position_id": dup_id,
                        "symbol": removed.symbol,
                        "dedup_key": f"{removed.strategy_id.value}:{removed.symbol}:{removed.long_exchange}:{removed.short_exchange}",
                    })

            if duplicates:
                logger.warning(
                    "[DEDUP_SUMMARY] Removed %d duplicate positions",
                    len(duplicates),
                )
        except Exception as exc:
            logger.warning("dedup_positions error (non-critical): %s", exc)

    async def _check_phantom_position(self, pos) -> bool:
        """Check if a tracked position actually exists on exchanges.

        Returns True if NO real contracts are found on either exchange
        (i.e., the position is phantom / leftover from test data or crash).
        """
        try:
            if not hasattr(self.execution.venue, "open_contracts"):
                return False
            long_contracts = await self.execution.venue.open_contracts(pos.long_exchange, pos.symbol)
            short_contracts = await self.execution.venue.open_contracts(pos.short_exchange, pos.symbol)
            # If both sides show zero contracts, it's a phantom
            return long_contracts <= 0 and short_contracts <= 0
        except Exception as exc:
            logger.warning("phantom_check_error: %s %s: %s", pos.position_id, pos.symbol, exc)
            return False

    def _get_min_notional(self, intent) -> float:
        """Return a rough minimum notional USD for the intent's exchanges."""
        market_data = getattr(self.provider, "market_data", None)
        if not market_data:
            return 1.0
        best = 1.0
        for exchange in [intent.long_exchange, intent.short_exchange]:
            ticker = market_data.get_futures_price(exchange, intent.symbol)
            if not ticker:
                continue
            px = (ticker.bid + ticker.ask) / 2
            ct = market_data.get_contract_size(exchange, intent.symbol)
            if px > 0 and ct > 0:
                best = max(best, px * ct)
            elif exchange == "bybit":
                best = max(best, 1.0)
        return best

    async def shutdown_gracefully(self) -> None:
        """FIX #5: Graceful shutdown — close all open positions before exiting."""
        logger.info("shutdown_gracefully: starting graceful unwinding")
        await self.monitor.emit("shutdown_start", {"message": "Graceful shutdown initiated"})

        positions = await self.risk.state.list_positions()
        if not positions:
            logger.info("shutdown_gracefully: no open positions, shutting down cleanly")
            await self.monitor.emit("shutdown_complete", {"positions_closed": 0})
            return

        logger.info("shutdown_gracefully: closing %d position(s)", len(positions))
        closed_count = 0
        for pos in positions:
            try:
                closed = await self.execution.execute_dual_exit(pos, "shutdown_graceful")
                if closed:
                    await self.risk.state.remove_position(pos.position_id)
                    closed_count += 1
                    logger.info("shutdown_gracefully: closed %s %s", pos.position_id, pos.symbol)
                else:
                    logger.warning(
                        "shutdown_gracefully: failed to close %s %s — position remains open",
                        pos.position_id, pos.symbol,
                    )
            except Exception as exc:
                logger.error("shutdown_gracefully: error closing %s: %s", pos.position_id, exc, exc_info=True)

        await self.monitor.emit("shutdown_complete", {"positions_requested": len(positions), "positions_closed": closed_count})
        logger.info("shutdown_gracefully: %d/%d positions closed", closed_count, len(positions))

    async def _log_restored_positions(self) -> None:
        """FIX #5: Log any positions restored from persistence on startup."""
        positions = await self.risk.state.list_positions()
        if positions:
            logger.warning(
                "startup_positions_restored: %d positions loaded from persistence. "
                "These may include stale positions from a previous crash — verify against exchange state.",
                len(positions),
            )
            for pos in positions:
                logger.info(
                    "  restored position: %s | %s | %s↔%s | %s contracts @ %.4f/%.4f",
                    pos.position_id, pos.symbol,
                    pos.long_exchange, pos.short_exchange,
                    pos.notional_usd,
                    float(pos.metadata.get("entry_long_price", 0) or 0),
                    float(pos.metadata.get("entry_short_price", 0) or 0),
                )

