import asyncio
import time

from arbitrage.system.config import RiskConfig
from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot, TradeIntent, StrategyId, AllocationPlan
from arbitrage.system.risk import RiskEngine
from arbitrage.system.state import SystemState


def _snapshot(stale: bool = False, imbalance: bool = False):
    ts = time.time() - (10 if stale else 0)
    orderbooks = {
        "okx": OrderBookSnapshot(exchange="okx", symbol="BTCUSDT", bid=100, ask=101, timestamp=ts),
        "htx": OrderBookSnapshot(exchange="htx", symbol="BTCUSDT", bid=100.5, ask=101.5, timestamp=ts),
    }
    return MarketSnapshot(
        symbol="BTCUSDT",
        orderbooks=orderbooks,
        spot_orderbooks={},
        orderbook_depth={},
        spot_orderbook_depth={},
        balances={"okx": 1000.0, "htx": 10.0 if imbalance else 1000.0},
        fee_bps={},
        funding_rates={},
        volatility=0.1,
        trend_strength=0.0,
        atr=10.0,
        atr_rolling=10.0,
        indicators={},
    )


def test_stale_book_killswitch():
    state = SystemState(starting_equity=1000.0)
    risk = RiskEngine(RiskConfig(max_orderbook_age_sec=1.0), state)
    intent = TradeIntent(
        strategy_id=StrategyId.PREFUNDED_ARBITRAGE,
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="htx",
        side="prefunded",
        confidence=0.5,
        expected_edge_bps=10.0,
        stop_loss_bps=5.0,
    )
    plan = AllocationPlan(strategy_allocations={StrategyId.PREFUNDED_ARBITRAGE: 100.0}, total_allocatable_capital=100.0)
    decision = asyncio.run(
        risk.validate_intent(
            intent=intent,
            allocation_plan=plan,
            proposed_notional=50.0,
            estimated_slippage_bps=1.0,
            leverage=1.0,
            api_latency_ms=10.0,
            snapshot=_snapshot(stale=True),
        )
    )
    assert decision.approved is False


def test_inventory_imbalance_rejects():
    state = SystemState(starting_equity=1000.0)
    risk = RiskEngine(RiskConfig(max_inventory_imbalance_pct=0.2), state)
    intent = TradeIntent(
        strategy_id=StrategyId.PREFUNDED_ARBITRAGE,
        symbol="BTCUSDT",
        long_exchange="okx",
        short_exchange="htx",
        side="prefunded",
        confidence=0.5,
        expected_edge_bps=10.0,
        stop_loss_bps=5.0,
    )
    plan = AllocationPlan(strategy_allocations={StrategyId.PREFUNDED_ARBITRAGE: 100.0}, total_allocatable_capital=100.0)
    decision = asyncio.run(
        risk.validate_intent(
            intent=intent,
            allocation_plan=plan,
            proposed_notional=50.0,
            estimated_slippage_bps=1.0,
            leverage=1.0,
            api_latency_ms=10.0,
            snapshot=_snapshot(imbalance=True),
        )
    )
    assert decision.approved is False
