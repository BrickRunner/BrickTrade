from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from dataclasses import field
from typing import List

from arbitrage.system.capital_allocator import CapitalAllocator
from arbitrage.system.config import TradingSystemConfig
from arbitrage.system.execution import AtomicExecutionEngine
from arbitrage.system.interfaces import MarketDataProvider, MonitoringSink
from arbitrage.system.models import StrategyId
from arbitrage.system.risk import RiskEngine
from arbitrage.system.state import SystemState
from arbitrage.system.strategy_runner import StrategyRunner
from arbitrage.system.strategies.cash_carry import CashCarryStrategy
from arbitrage.system.strategies.funding_arbitrage import FundingArbitrageStrategy
from arbitrage.system.strategies.funding_spread import FundingSpreadStrategy
from arbitrage.system.strategies.grid import GridStrategy
from arbitrage.system.strategies.indicator import IndicatorStrategy
from arbitrage.system.strategies.spot_arbitrage import SpotArbitrageStrategy
from arbitrage.system.strategies.triangular_arbitrage import (
    MultiTriangularArbitrageStrategy,
    TriangularArbitrageStrategy,
)
from arbitrage.system.strategies.prefunded_arbitrage import PreFundedArbitrageStrategy
from arbitrage.system.strategies.orderbook_imbalance import OrderbookImbalanceStrategy
from arbitrage.system.strategies.spread_capture import SpreadCaptureStrategy

logger = logging.getLogger("trading_system")


def build_strategies(config: TradingSystemConfig, market_data=None) -> List:
    mapping = {
        StrategyId.SPOT_ARBITRAGE.value: SpotArbitrageStrategy(config.strategy.min_edge_bps),
        StrategyId.CASH_CARRY.value: CashCarryStrategy(config.strategy.basis_threshold_bps),
        StrategyId.FUNDING_ARBITRAGE.value: FundingArbitrageStrategy(config.strategy.funding_threshold_bps),
        StrategyId.FUNDING_SPREAD.value: FundingSpreadStrategy(config.strategy.funding_threshold_bps),
        StrategyId.PREFUNDED_ARBITRAGE.value: PreFundedArbitrageStrategy(config.strategy.min_edge_bps),
        StrategyId.ORDERBOOK_IMBALANCE.value: OrderbookImbalanceStrategy(),
        StrategyId.SPREAD_CAPTURE.value: SpreadCaptureStrategy(),
        StrategyId.GRID.value: GridStrategy(),
        StrategyId.INDICATOR.value: IndicatorStrategy(),
    }
    if market_data is not None:
        mapping[StrategyId.TRIANGULAR_ARBITRAGE.value] = TriangularArbitrageStrategy(
            market_data=market_data,
            exchanges=config.exchanges,
        )
        mapping[StrategyId.MULTI_TRIANGULAR_ARBITRAGE.value] = MultiTriangularArbitrageStrategy(
            market_data=market_data,
            exchanges=config.exchanges,
        )
    spot_enabled = os.getenv("ENABLE_SPOT_EXECUTION", "false").strip().lower() in {"1", "true", "yes", "on"}
    if not spot_enabled:
        mapping.pop(StrategyId.SPOT_ARBITRAGE.value, None)
        mapping.pop(StrategyId.CASH_CARRY.value, None)
        mapping.pop(StrategyId.TRIANGULAR_ARBITRAGE.value, None)
        mapping.pop(StrategyId.MULTI_TRIANGULAR_ARBITRAGE.value, None)
        mapping.pop(StrategyId.SPREAD_CAPTURE.value, None)
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
    _cycle_count: int = 0
    _symbol_cooldown_until: dict[str, float] = field(default_factory=dict)
    _last_position_monitor_log_ts: dict[str, float] = field(default_factory=dict)
    _symbol_loss_streak: dict[str, int] = field(default_factory=dict)
    _auto_strategy_select: bool = field(default_factory=lambda: os.getenv("AUTO_STRATEGY_SELECT", "true").strip().lower() in {"1", "true", "yes", "on"})
    _temp_replace_spot: bool = field(default_factory=lambda: os.getenv("TEMP_REPLACE_SPOT", "true").strip().lower() in {"1", "true", "yes", "on"})

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
        state_snapshot = await self.risk.state.snapshot()
        api_health = await self.provider.health()
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
            elif self._cycle_count > 1 and hasattr(self.risk.state, '_kill_switch_ts') and self.risk.state._kill_switch_ts > 0:
                logger.info("kill_switch auto-reset after cooldown, resuming trading")
                self.risk.state._kill_switch_ts = 0.0
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
                f"[STRATEGY_SELECT] symbol={symbol} auto={self._auto_strategy_select} "
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
                    "auto": self._auto_strategy_select,
                },
            )
            intents = await self.strategies.generate_intents(snapshot, enabled_ids=selected_ids)
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
                alloc_cap = allocation.strategy_allocations.get(intent.strategy_id, 0.0)
                # For small accounts: allow up to 30% of equity per trade (capped by risk engine).
                # For larger accounts the allocator cap will be the binding constraint.
                equity_cap = state_snapshot["equity"] * 0.30
                proposed_notional = min(alloc_cap, equity_cap) if alloc_cap > 0 else equity_cap
                # Ensure proposed_notional is at least the exchange minimum notional
                # so small accounts can still place one-contract orders.
                min_notional_hint = self._get_min_notional(intent)
                min_notional_override = False
                if proposed_notional < min_notional_hint and equity_cap >= min_notional_hint:
                    proposed_notional = min_notional_hint
                    min_notional_override = True
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
                    # Avoid hammering the same unaffordable/rejected symbol every cycle.
                    self._symbol_cooldown_until[symbol] = max(
                        self._symbol_cooldown_until.get(symbol, 0.0),
                        time.time() + 300,
                    )
                if not report.success and report.message == "second_leg_failed":
                    # Cooldown noisy symbols with repeated dual-leg failures.
                    self._symbol_cooldown_until[symbol] = time.time() + 120
                if not report.success and report.message == "second_leg_failed" and not report.hedged:
                    await self.monitor.emit(
                        "execution_critical",
                        {
                            "symbol": symbol,
                            "strategy": intent.strategy_id.value,
                            "reason": "unverified_hedge_after_second_leg_failure",
                        },
                    )
                    await self.risk.state.trigger_kill_switch(permanent=True)
                    return
                if report.success:
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
                            await self.risk.state.trigger_kill_switch(permanent=True)
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
        take_profit_usd = max(0.01, float(os.getenv("EXIT_TAKE_PROFIT_USD", "0.08")))
        stop_loss_usd = max(0.01, float(os.getenv("EXIT_STOP_LOSS_USD", "0.15")))
        max_holding_seconds = max(30, int(float(os.getenv("EXIT_MAX_HOLD_SECONDS", "1800"))))
        close_edge_bps = max(0.0, float(os.getenv("EXIT_CLOSE_EDGE_BPS", "1.0")))
        monitor_log_interval_sec = max(5, int(float(os.getenv("POSITION_MONITOR_LOG_INTERVAL_SEC", "20"))))
        loss_streak_limit = max(1, int(float(os.getenv("LOSS_STREAK_LIMIT", "3"))))
        loss_streak_cooldown_seconds = max(
            60, int(float(os.getenv("LOSS_STREAK_COOLDOWN_HOURS", "3")) * 3600)
        )

        positions = await self.risk.state.list_positions()
        now = time.time()
        for pos in positions:
            try:
                snapshot = await self.provider.get_snapshot(pos.symbol)
                long_ob = snapshot.orderbooks.get(pos.long_exchange)
                short_ob = snapshot.orderbooks.get(pos.short_exchange)
                if not long_ob or not short_ob:
                    continue

                long_mid = long_ob.mid
                short_mid = short_ob.mid
                entry_long = float(pos.metadata.get("entry_long_price", long_mid) or long_mid)
                entry_short = float(pos.metadata.get("entry_short_price", short_mid) or short_mid)

                # Approx mark-to-market PnL in quote currency on each leg.
                long_pnl = ((long_mid - entry_long) / max(entry_long, 1e-9)) * pos.notional_usd
                short_pnl = ((entry_short - short_mid) / max(entry_short, 1e-9)) * pos.notional_usd
                pnl_usd = long_pnl + short_pnl
                mid_ref = max((long_mid + short_mid) / 2, 1e-9)
                edge_bps = ((short_mid - long_mid) / mid_ref) * 10_000
                age_sec = now - pos.opened_at

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
                            "edge_bps": round(edge_bps, 3),
                        },
                    )

                # Strategy-specific overrides from intent metadata.
                tp_local = float(pos.metadata.get("take_profit_usd", take_profit_usd) or take_profit_usd)
                sl_local = float(pos.metadata.get("stop_loss_usd", stop_loss_usd) or stop_loss_usd)
                max_hold_local = int(pos.metadata.get("max_holding_seconds", max_holding_seconds) or max_holding_seconds)
                close_edge_local = float(pos.metadata.get("close_edge_bps", close_edge_bps) or close_edge_bps)

                close_reason = None
                if pnl_usd >= tp_local:
                    close_reason = "take_profit"
                elif pnl_usd <= -sl_local:
                    close_reason = "stop_loss"
                elif age_sec >= max_hold_local:
                    close_reason = "max_holding_time"
                elif edge_bps <= close_edge_local:
                    close_reason = "edge_converged"
                elif pos.strategy_id == StrategyId.INDICATOR:
                    signal_side = float(pos.metadata.get("signal_side", 0.0) or 0.0)
                    ema_fast = snapshot.indicators.get("ema_fast", 0.0)
                    ema_slow = snapshot.indicators.get("ema_slow", 0.0)
                    macd = snapshot.indicators.get("macd", 0.0)
                    macd_signal = snapshot.indicators.get("macd_signal", 0.0)
                    if signal_side > 0 and (ema_fast < ema_slow or macd < macd_signal):
                        close_reason = "indicator_reversal"
                    elif signal_side < 0 and (ema_fast > ema_slow or macd > macd_signal):
                        close_reason = "indicator_reversal"

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
                    await self.monitor.emit(
                        "position_close_failed",
                        {"position_id": pos.position_id, "symbol": pos.symbol, "reason": close_reason},
                    )
                    continue

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
        # User-requested realized PnL model:
        # take balance deltas on both exchanges since entry, then compute
        # "where more came in" minus "where less came in".
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
            balance_delta_pnl = max(delta_long, delta_short) - min(delta_long, delta_short)
            return float(balance_delta_pnl)
        except Exception:
            return fallback_pnl_usd

    def _select_strategy_ids(self, snapshot) -> set[StrategyId]:
        available = {s.strategy_id for s in self.strategies.strategies}
        if not self._auto_strategy_select:
            selected = set(available)
            return self._apply_spot_replacement(selected, available)

        selected: set[StrategyId] = set()
        ind = snapshot.indicators
        vol = snapshot.volatility
        rsi = ind.get("rsi", 50.0)
        basis_bps = abs(ind.get("basis_bps", 0.0))
        funding_spread_bps = abs(ind.get("funding_spread_bps", 0.0))

        # Regime-style routing from live market data.
        # Thresholds are relaxed — each strategy has its own internal filters.
        if StrategyId.FUNDING_ARBITRAGE in available and funding_spread_bps >= self.config.strategy.funding_threshold_bps * 0.5:
            selected.add(StrategyId.FUNDING_ARBITRAGE)
        if StrategyId.FUNDING_SPREAD in available and funding_spread_bps >= self.config.strategy.funding_threshold_bps * 0.3:
            selected.add(StrategyId.FUNDING_SPREAD)
        if StrategyId.CASH_CARRY in available and basis_bps >= self.config.strategy.basis_threshold_bps * 0.5:
            selected.add(StrategyId.CASH_CARRY)

        if StrategyId.INDICATOR in available:
            if vol <= 3.0:
                selected.add(StrategyId.INDICATOR)

        if StrategyId.TRIANGULAR_ARBITRAGE in available and vol <= 3.0:
            selected.add(StrategyId.TRIANGULAR_ARBITRAGE)
        if StrategyId.MULTI_TRIANGULAR_ARBITRAGE in available and vol <= 2.0:
            selected.add(StrategyId.MULTI_TRIANGULAR_ARBITRAGE)
        if StrategyId.PREFUNDED_ARBITRAGE in available:
            selected.add(StrategyId.PREFUNDED_ARBITRAGE)
        if StrategyId.ORDERBOOK_IMBALANCE in available:
            selected.add(StrategyId.ORDERBOOK_IMBALANCE)
        if StrategyId.SPREAD_CAPTURE in available and vol <= 2.0:
            selected.add(StrategyId.SPREAD_CAPTURE)

        if StrategyId.GRID in available:
            if vol <= 2.0 and 20.0 <= rsi <= 80.0:
                selected.add(StrategyId.GRID)

        # Fallback when no specialized strategy is selected — enable all.
        if not selected:
            selected = set(available)

        return self._apply_spot_replacement(selected, available)

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

    def _apply_spot_replacement(self, selected: set[StrategyId], available: set[StrategyId]) -> set[StrategyId]:
        if not self._temp_replace_spot:
            return selected
        if StrategyId.SPOT_ARBITRAGE not in selected:
            return selected
        selected.discard(StrategyId.SPOT_ARBITRAGE)
        if StrategyId.CASH_CARRY in available:
            selected.add(StrategyId.CASH_CARRY)
        elif StrategyId.FUNDING_SPREAD in available:
            selected.add(StrategyId.FUNDING_SPREAD)
        elif StrategyId.INDICATOR in available:
            selected.add(StrategyId.INDICATOR)
        return selected
