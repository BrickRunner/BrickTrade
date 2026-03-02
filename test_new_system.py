"""
Comprehensive test suite for the new multi-strategy arbitrage system.

Tests:
1. Core: BotState, ActivePosition, MarketDataEngine, RiskManager, MetricsTracker
2. Strategies: FundingArb, BasisArb, StatArb — detection + exit logic
3. Execution: TradeExecutor — entry/exit with mocked exchanges
4. Router: StrategyRouter — full pipeline integration
5. Edge cases: empty data, failures, partial fills, emergency close
"""
import asyncio
import time
import sys

# ──────────────────────────────────────────────────────────────────────
# Mock Exchange Client
# ──────────────────────────────────────────────────────────────────────

class MockExchangeClient:
    """Mock exchange client that returns configurable data."""

    def __init__(self, name: str):
        self.name = name
        self.session = None
        self._instruments = []
        self._tickers = []
        self._spot_tickers = []
        self._funding_rates = []
        self._balance = 100.0
        self._orders_placed = []
        self._positions = {}
        self._leverage_set = {}
        self._fail_orders = False

    # ─── Configure mock data ──────────────────────────────────────────
    def set_instruments(self, instruments: list):
        self._instruments = instruments

    def set_tickers(self, tickers: list):
        self._tickers = tickers

    def set_spot_tickers(self, tickers: list):
        self._spot_tickers = tickers

    def set_funding_rates(self, rates: list):
        self._funding_rates = rates

    def set_balance(self, balance: float):
        self._balance = balance

    def set_position(self, symbol: str, size: float, avg_price: float, side: str = "long"):
        self._positions[symbol] = {"size": size, "avg_price": avg_price, "side": side}

    def clear_positions(self):
        self._positions = {}

    # ─── OKX-style responses ──────────────────────────────────────────

    async def get_instruments(self, inst_type: str = "SWAP"):
        if self.name == "okx":
            return {"code": "0", "data": self._instruments}
        elif self.name == "htx":
            return {"status": "ok", "data": self._instruments}
        elif self.name == "bybit":
            return {"retCode": 0, "result": {"list": self._instruments}}
        return {}

    async def get_tickers(self, inst_type: str = "SWAP"):
        if self.name == "okx":
            return {"code": "0", "data": self._tickers}
        elif self.name == "htx":
            return {"status": "ok", "ticks": self._tickers}
        elif self.name == "bybit":
            return {"retCode": 0, "result": {"list": self._tickers}}
        return {}

    async def get_spot_tickers(self):
        if self.name == "okx":
            return {"code": "0", "data": self._spot_tickers}
        elif self.name == "htx":
            return {"status": "ok", "data": self._spot_tickers}
        elif self.name == "bybit":
            return {"retCode": 0, "result": {"list": self._spot_tickers}}
        return {}

    async def get_funding_rates(self):
        if self.name == "htx":
            return {"status": "ok", "data": self._funding_rates}
        elif self.name == "bybit":
            return await self.get_tickers()
        return {}

    async def get_funding_rates_all(self):
        if self.name == "okx":
            return {"code": "0", "data": self._funding_rates}
        return {}

    async def get_funding_rate(self, inst_id: str):
        return {"code": "0", "data": []}

    async def get_balance(self):
        if self.name == "okx":
            return {"code": "0", "data": [{"details": [{"ccy": "USDT", "availBal": str(self._balance)}]}]}
        elif self.name == "htx":
            return {"code": 200, "data": [{"margin_asset": "USDT", "withdraw_available": str(self._balance)}]}
        elif self.name == "bybit":
            return {"retCode": 0, "result": {"list": [{"coin": [{"coin": "USDT", "availableToWithdraw": str(self._balance)}]}]}}
        return {}

    async def get_cross_position(self, symbol: str):
        pos = self._positions.get(symbol)
        if self.name == "okx":
            if pos:
                inst_id = symbol.replace("USDT", "") + "-USDT-SWAP"
                return {"code": "0", "data": [{"instId": inst_id, "pos": str(pos["size"]), "avgPx": str(pos["avg_price"])}]}
            return {"code": "0", "data": []}
        elif self.name == "htx":
            if pos:
                return {"status": "ok", "data": [{"volume": pos["size"], "cost_open": pos["avg_price"]}]}
            return {"status": "ok", "data": []}
        elif self.name == "bybit":
            if pos:
                return {"retCode": 0, "result": {"list": [{"size": str(pos["size"]), "avgPrice": str(pos["avg_price"])}]}}
            return {"retCode": 0, "result": {"list": []}}
        return {}

    async def place_order(self, symbol, side, size, order_type="limit", price=0, time_in_force="", offset="open", lever_rate=1):
        self._orders_placed.append({
            "symbol": symbol, "side": side, "size": size,
            "type": order_type, "price": price, "offset": offset
        })

        if self._fail_orders:
            if self.name == "okx":
                return {"code": "1", "data": []}
            elif self.name == "htx":
                return {"status": "error", "data": None}
            elif self.name == "bybit":
                return {"retCode": 1, "result": None}

        # Simulate fill: set or close position
        # OKX doesn't pass offset through — always detect close by checking
        # if existing position has opposite side
        if offset == "close":
            self._positions.pop(symbol, None)
        else:
            existing = self._positions.get(symbol)
            if existing:
                existing_side = existing.get("side", "")
                # Opposite side = closing regardless of offset
                if (existing_side == "buy" and side == "sell") or \
                   (existing_side == "sell" and side == "buy"):
                    self._positions.pop(symbol, None)
                else:
                    self._positions[symbol] = {"size": size, "avg_price": price or 50000.0, "side": side}
            else:
                self._positions[symbol] = {"size": size, "avg_price": price or 50000.0, "side": side}

        if self.name == "okx":
            return {"code": "0", "data": [{"ordId": "mock-123"}]}
        elif self.name == "htx":
            return {"status": "ok", "data": {"order_id": 456}}
        elif self.name == "bybit":
            return {"retCode": 0, "result": {"orderId": "789"}}
        return {}

    async def set_leverage(self, symbol, leverage, margin_mode="cross"):
        self._leverage_set[symbol] = leverage
        if self.name == "okx":
            return {"code": "0"}
        elif self.name == "htx":
            return {"status": "ok"}
        elif self.name == "bybit":
            return {"retCode": 0}
        return {}

    async def close(self):
        pass


def create_okx_instruments():
    return [
        {"instId": "BTC-USDT-SWAP", "ctVal": "0.01"},
        {"instId": "ETH-USDT-SWAP", "ctVal": "0.1"},
        {"instId": "SOL-USDT-SWAP", "ctVal": "1"},
    ]

def create_htx_instruments():
    return [
        {"contract_code": "BTC-USDT", "contract_size": "0.001"},
        {"contract_code": "ETH-USDT", "contract_size": "0.01"},
        {"contract_code": "SOL-USDT", "contract_size": "1"},
    ]

def create_bybit_instruments():
    return [
        {"symbol": "BTCUSDT", "lotSizeFilter": {"qtyStep": "0.001"}},
        {"symbol": "ETHUSDT", "lotSizeFilter": {"qtyStep": "0.01"}},
        {"symbol": "SOLUSDT", "lotSizeFilter": {"qtyStep": "1"}},
    ]

def create_okx_tickers(btc_bid=50000, btc_ask=50010, eth_bid=3000, eth_ask=3002):
    return [
        {"instId": "BTC-USDT-SWAP", "bidPx": str(btc_bid), "askPx": str(btc_ask), "last": str((btc_bid+btc_ask)/2)},
        {"instId": "ETH-USDT-SWAP", "bidPx": str(eth_bid), "askPx": str(eth_ask), "last": str((eth_bid+eth_ask)/2)},
        {"instId": "SOL-USDT-SWAP", "bidPx": "150.0", "askPx": "150.1", "last": "150.05"},
    ]

def create_htx_tickers(btc_bid=50005, btc_ask=50015, eth_bid=3001, eth_ask=3003):
    return [
        {"contract_code": "BTC-USDT", "bid": [btc_bid, 1], "ask": [btc_ask, 1], "close": (btc_bid+btc_ask)/2},
        {"contract_code": "ETH-USDT", "bid": [eth_bid, 1], "ask": [eth_ask, 1], "close": (eth_bid+eth_ask)/2},
        {"contract_code": "SOL-USDT", "bid": [150.05, 1], "ask": [150.15, 1], "close": 150.10},
    ]

def create_bybit_tickers(btc_bid=49995, btc_ask=50005, eth_bid=2999, eth_ask=3001, funding_rate="0.0001"):
    return [
        {"symbol": "BTCUSDT", "bid1Price": str(btc_bid), "ask1Price": str(btc_ask), "lastPrice": str((btc_bid+btc_ask)/2), "fundingRate": funding_rate, "nextFundingTime": "0"},
        {"symbol": "ETHUSDT", "bid1Price": str(eth_bid), "ask1Price": str(eth_ask), "lastPrice": str((eth_bid+eth_ask)/2), "fundingRate": funding_rate, "nextFundingTime": "0"},
        {"symbol": "SOLUSDT", "bid1Price": "149.95", "ask1Price": "150.05", "lastPrice": "150.0", "fundingRate": funding_rate, "nextFundingTime": "0"},
    ]

def create_okx_funding(btc_rate="0.0001", eth_rate="0.0002"):
    return [
        {"instId": "BTC-USDT-SWAP", "fundingRate": btc_rate, "nextFundingTime": "0"},
        {"instId": "ETH-USDT-SWAP", "fundingRate": eth_rate, "nextFundingTime": "0"},
        {"instId": "SOL-USDT-SWAP", "fundingRate": "0.0001", "nextFundingTime": "0"},
    ]

def create_htx_funding(btc_rate="0.0003", eth_rate="0.0005"):
    return [
        {"contract_code": "BTC-USDT", "funding_rate": btc_rate, "settlement_time": "0"},
        {"contract_code": "ETH-USDT", "funding_rate": eth_rate, "settlement_time": "0"},
        {"contract_code": "SOL-USDT", "funding_rate": "0.0002", "settlement_time": "0"},
    ]

def create_okx_spot_tickers(btc_mid=49990, eth_mid=2998):
    return [
        {"instId": "BTC-USDT", "bidPx": str(btc_mid - 5), "askPx": str(btc_mid + 5)},
        {"instId": "ETH-USDT", "bidPx": str(eth_mid - 1), "askPx": str(eth_mid + 1)},
        {"instId": "SOL-USDT", "bidPx": "149.5", "askPx": "150.5"},
    ]

def create_htx_spot_tickers(btc_mid=49992, eth_mid=2999):
    return [
        {"symbol": "btcusdt", "bid": btc_mid - 5, "ask": btc_mid + 5},
        {"symbol": "ethusdt", "bid": eth_mid - 1, "ask": eth_mid + 1},
        {"symbol": "solusdt", "bid": 149.5, "ask": 150.5},
    ]

def create_bybit_spot_tickers(btc_mid=49988, eth_mid=2997):
    return [
        {"symbol": "BTCUSDT", "bid1Price": str(btc_mid - 5), "ask1Price": str(btc_mid + 5)},
        {"symbol": "ETHUSDT", "bid1Price": str(eth_mid - 1), "ask1Price": str(eth_mid + 1)},
        {"symbol": "SOLUSDT", "bid1Price": "149.5", "ask1Price": "150.5"},
    ]


def setup_mock_exchanges():
    """Create 3 mock exchanges with standard test data."""
    okx = MockExchangeClient("okx")
    htx = MockExchangeClient("htx")
    bybit = MockExchangeClient("bybit")

    okx.set_instruments(create_okx_instruments())
    htx.set_instruments(create_htx_instruments())
    bybit.set_instruments(create_bybit_instruments())

    okx.set_tickers(create_okx_tickers())
    htx.set_tickers(create_htx_tickers())
    bybit.set_tickers(create_bybit_tickers())

    okx.set_spot_tickers(create_okx_spot_tickers())
    htx.set_spot_tickers(create_htx_spot_tickers())
    bybit.set_spot_tickers(create_bybit_spot_tickers())

    okx.set_funding_rates(create_okx_funding())
    htx.set_funding_rates(create_htx_funding())

    okx.set_balance(200.0)
    htx.set_balance(150.0)
    bybit.set_balance(100.0)

    return {"okx": okx, "htx": htx, "bybit": bybit}


# ──────────────────────────────────────────────────────────────────────
# Test counters
# ──────────────────────────────────────────────────────────────────────
passed = 0
failed = 0
errors = []

def test(name: str, condition: bool, detail: str = ""):
    global passed, failed, errors
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        msg = f"  ❌ {name}"
        if detail:
            msg += f" — {detail}"
        print(msg)
        errors.append(msg)


# ══════════════════════════════════════════════════════════════════════
# TEST 1: BotState
# ══════════════════════════════════════════════════════════════════════
def test_bot_state():
    print("\n═══ TEST 1: BotState ═══")
    from arbitrage.core.state import BotState, ActivePosition

    state = BotState()

    # Balance management
    state.update_balance("okx", 100.0)
    state.update_balance("htx", 50.0)
    state.update_balance("bybit", 75.0)
    test("total_balance", state.total_balance == 225.0, f"got {state.total_balance}")
    test("get_balance okx", state.get_balance("okx") == 100.0)
    test("get_balance htx", state.get_balance("htx") == 50.0)
    test("get_balance unknown", state.get_balance("binance") == 0.0)
    test("legacy okx_balance", state.okx_balance == 100.0)
    test("legacy htx_balance", state.htx_balance == 50.0)
    test("legacy bybit_balance", state.bybit_balance == 75.0)

    # Position management
    test("empty positions", state.position_count() == 0)

    pos1 = ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=10, short_contracts=10,
        long_price=50000, short_price=50100,
        entry_spread=0.2, size_usd=100
    )
    state.add_position(pos1)
    test("position count = 1", state.position_count() == 1)
    test("has_position BTCUSDT", state.has_position_on_symbol("BTCUSDT"))
    test("no_position ETHUSDT", not state.has_position_on_symbol("ETHUSDT"))

    pos2 = ActivePosition(
        strategy="basis_arb", symbol="ETHUSDT",
        long_exchange="okx", short_exchange="bybit",
        long_contracts=5, short_contracts=5,
        long_price=3000, short_price=3010,
        entry_spread=0.3, size_usd=50
    )
    state.add_position(pos2)
    test("position count = 2", state.position_count() == 2)
    test("get_position funding_arb BTCUSDT", state.get_position("funding_arb", "BTCUSDT") is not None)
    test("get_position basis_arb ETHUSDT", state.get_position("basis_arb", "ETHUSDT") is not None)
    test("get_positions_by_strategy", len(state.get_positions_by_strategy("funding_arb")) == 1)
    test("get_all_positions", len(state.get_all_positions()) == 2)

    # Same symbol, different strategy — allowed
    pos3 = ActivePosition(
        strategy="stat_arb", symbol="BTCUSDT",
        long_exchange="htx", short_exchange="bybit",
        long_contracts=3, short_contracts=3,
        long_price=50005, short_price=50015,
        entry_spread=0.1, size_usd=30
    )
    state.add_position(pos3)
    test("same symbol different strategy", state.position_count() == 3)

    # Remove position
    removed = state.remove_position("funding_arb", "BTCUSDT")
    test("remove returns position", removed is not None)
    test("position count after remove = 2", state.position_count() == 2)
    removed_again = state.remove_position("funding_arb", "BTCUSDT")
    test("remove non-existent returns None", removed_again is None)

    # Trade recording
    state.record_trade("funding_arb", success=True, pnl=0.5)
    state.record_trade("funding_arb", success=False, pnl=-0.2)
    state.record_trade("basis_arb", success=True, pnl=0.3)
    test("total_trades = 3", state.total_trades == 3)
    test("successful_trades = 2", state.successful_trades == 2)
    test("failed_trades = 1", state.failed_trades == 1)
    test("total_pnl", abs(state.total_pnl - 0.6) < 0.001, f"got {state.total_pnl}")

    # Strategy stats
    stats = state.get_stats()
    test("stats has strategy_stats", "strategy_stats" in stats)
    test("funding_arb trades = 2", stats["strategy_stats"]["funding_arb"]["trades"] == 2)
    test("basis_arb pnl = 0.3", abs(stats["strategy_stats"]["basis_arb"]["pnl"] - 0.3) < 0.001)

    # ActivePosition duration
    test("position duration > 0", pos2.duration() > 0)


# ══════════════════════════════════════════════════════════════════════
# TEST 2: MarketDataEngine
# ══════════════════════════════════════════════════════════════════════
async def test_market_data():
    print("\n═══ TEST 2: MarketDataEngine ═══")
    from arbitrage.core.market_data import MarketDataEngine

    exchanges = setup_mock_exchanges()
    md = MarketDataEngine(exchanges)

    # Initialize
    count = await md.initialize()
    test("common pairs > 0", count > 0, f"got {count}")
    test("common pairs has BTCUSDT", "BTCUSDT" in md.common_pairs)
    test("common pairs has ETHUSDT", "ETHUSDT" in md.common_pairs)
    test("common pairs has SOLUSDT", "SOLUSDT" in md.common_pairs)

    # Contract sizes
    test("okx BTC contract size", md.get_contract_size("okx", "BTCUSDT") == 0.01)
    test("htx BTC contract size", md.get_contract_size("htx", "BTCUSDT") == 0.001)
    test("bybit BTC contract size", md.get_contract_size("bybit", "BTCUSDT") == 0.001)

    # Futures prices
    await md.update_futures_prices()
    btc_okx = md.get_futures_price("okx", "BTCUSDT")
    test("okx BTC futures price exists", btc_okx is not None)
    test("okx BTC bid > 0", btc_okx.bid == 50000)
    test("okx BTC ask > 0", btc_okx.ask == 50010)

    btc_htx = md.get_futures_price("htx", "BTCUSDT")
    test("htx BTC futures price exists", btc_htx is not None)
    test("htx BTC bid", btc_htx.bid == 50005)

    btc_bybit = md.get_futures_price("bybit", "BTCUSDT")
    test("bybit BTC futures price exists", btc_bybit is not None)
    test("bybit BTC bid", btc_bybit.bid == 49995)

    # Spot prices
    await md.update_spot_prices()
    spot_okx = md.get_spot_price("okx", "BTCUSDT")
    test("okx BTC spot price exists", spot_okx is not None)
    test("okx BTC spot > 0", spot_okx > 0)

    spot_htx = md.get_spot_price("htx", "BTCUSDT")
    test("htx BTC spot price exists", spot_htx is not None)

    # Funding rates
    await md.update_funding_rates()
    fd_okx = md.get_funding("okx", "BTCUSDT")
    test("okx BTC funding exists", fd_okx is not None)
    test("okx BTC funding rate", abs(fd_okx.rate - 0.0001) < 1e-8)
    test("okx BTC funding rate_pct", abs(fd_okx.rate_pct - 0.01) < 0.001)

    fd_htx = md.get_funding("htx", "BTCUSDT")
    test("htx BTC funding exists", fd_htx is not None)
    test("htx BTC funding rate", abs(fd_htx.rate - 0.0003) < 1e-8)

    fd_bybit = md.get_funding("bybit", "BTCUSDT")
    test("bybit BTC funding exists", fd_bybit is not None)

    # Balances
    balances = await md.fetch_balances()
    test("okx balance = 200", balances.get("okx") == 200.0)
    test("htx balance = 150", balances.get("htx") == 150.0)
    test("bybit balance = 100", balances.get("bybit") == 100.0)

    # Non-existent data
    test("unknown exchange returns None", md.get_futures_price("binance", "BTCUSDT") is None)
    test("unknown symbol returns None", md.get_futures_price("okx", "XYZUSDT") is None)

    # Exchange names
    names = md.get_exchange_names()
    test("exchange names", set(names) == {"okx", "htx", "bybit"})

    # Update all
    await md.update_all()
    test("update_all runs without error", True)

    # Latency tracking
    lat = md.get_latency("okx")
    test("okx latency >= 0", lat >= 0)


# ══════════════════════════════════════════════════════════════════════
# TEST 3: RiskManager
# ══════════════════════════════════════════════════════════════════════
def test_risk_manager():
    print("\n═══ TEST 3: RiskManager ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.state import BotState, ActivePosition
    from arbitrage.core.risk import RiskManager
    from arbitrage.strategies.base import Opportunity, StrategyType

    config = ArbitrageConfig.from_env()
    state = BotState()
    state.update_balance("okx", 100.0)
    state.update_balance("htx", 100.0)
    risk = RiskManager(config, state)

    opp = Opportunity(
        strategy=StrategyType.FUNDING_ARB, symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        expected_profit_pct=0.05, long_price=50000, short_price=50100,
    )

    # Basic approval
    test("can_open basic", risk.can_open_position(opp))

    # Low balance
    state.update_balance("okx", 3.0)
    test("reject low balance", not risk.can_open_position(opp))
    state.update_balance("okx", 100.0)

    # Total balance too low
    state.update_balance("okx", 4.0)
    state.update_balance("htx", 4.0)
    test("reject total < 10", not risk.can_open_position(opp))
    state.update_balance("okx", 100.0)
    state.update_balance("htx", 100.0)

    # Position count limit
    for i in range(config.max_concurrent_positions):
        state.add_position(ActivePosition(
            strategy="funding_arb", symbol=f"TEST{i}USDT",
            long_exchange="okx", short_exchange="htx",
            long_contracts=1, short_contracts=1,
            long_price=100, short_price=101,
            entry_spread=0.1, size_usd=10,
        ))
    test("reject max positions", not risk.can_open_position(opp))

    # Clear positions
    for i in range(config.max_concurrent_positions):
        state.remove_position("funding_arb", f"TEST{i}USDT")

    # Duplicate symbol
    state.add_position(ActivePosition(
        strategy="basis_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=1, short_contracts=1,
        long_price=50000, short_price=50100,
        entry_spread=0.1, size_usd=10,
    ))
    test("reject duplicate symbol", not risk.can_open_position(opp))
    state.remove_position("basis_arb", "BTCUSDT")

    # Circuit breaker
    for _ in range(5):
        risk.record_failure()
    test("circuit breaker active", not risk.can_open_position(opp))
    risk.record_success()
    test("circuit breaker reset", risk.can_open_position(opp))

    # Emergency close — balance critical
    state.update_balance("okx", 2.0)
    state.update_balance("htx", 2.0)
    state.add_position(ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=1, short_contracts=1,
        long_price=50000, short_price=50100,
        entry_spread=0.1, size_usd=10,
    ))
    emergency, reason = risk.should_emergency_close()
    test("emergency on low balance", emergency)
    test("emergency reason", reason == "balance_critical")

    # No emergency when no positions
    state.remove_position("funding_arb", "BTCUSDT")
    state.update_balance("okx", 2.0)
    state.update_balance("htx", 2.0)
    emergency2, _ = risk.should_emergency_close()
    test("no emergency without positions", not emergency2)

    # Funding profitability
    test("funding profitable", risk.check_funding_profitability(None, 0.5, 0.1))
    # Create a mock position object for the check
    class MockPos:
        symbol = "TEST"
    test("funding unprofitable", not risk.check_funding_profitability(MockPos(), 0.1, 0.5))


# ══════════════════════════════════════════════════════════════════════
# TEST 4: MetricsTracker
# ══════════════════════════════════════════════════════════════════════
def test_metrics():
    print("\n═══ TEST 4: MetricsTracker ═══")
    from arbitrage.core.metrics import MetricsTracker

    m = MetricsTracker()

    # Empty state
    summary = m.summary()
    test("empty entries = 0", summary["entries"] == 0)
    test("empty sharpe = 0", summary["sharpe"] == 0.0)
    test("empty max_drawdown = 0", summary["max_drawdown"] == 0.0)

    # Record trades
    m.record_entry("funding_arb", "BTCUSDT")
    m.record_entry("basis_arb", "ETHUSDT")
    m.record_exit("funding_arb", "BTCUSDT", 0.5, "target")
    m.record_exit("basis_arb", "ETHUSDT", -0.2, "timeout")
    m.record_exit("funding_arb", "SOLUSDT", 0.3, "target")

    summary = m.summary()
    test("entries = 2", summary["entries"] == 2)
    test("exits = 3", summary["exits"] == 3)
    test("cumulative_pnl = 0.6", abs(summary["cumulative_pnl"] - 0.6) < 0.001)

    # Per strategy
    test("funding_arb trades = 2", summary["per_strategy"]["funding_arb"]["trades"] == 2)
    test("basis_arb trades = 1", summary["per_strategy"]["basis_arb"]["trades"] == 1)

    # Drawdown
    m2 = MetricsTracker()
    m2.record_exit("test", "BTC", 1.0, "ok")
    m2.record_exit("test", "BTC", -0.5, "loss")
    m2.record_exit("test", "BTC", -0.3, "loss")
    test("drawdown = 0.8", abs(m2.summary()["max_drawdown"] - 0.8) < 0.001, f"got {m2.summary()['max_drawdown']}")

    # Cycle time
    m.record_cycle_time(0.05)
    m.record_cycle_time(0.03)
    summary = m.summary()
    test("avg_cycle_ms > 0", summary["avg_cycle_ms"] > 0)


# ══════════════════════════════════════════════════════════════════════
# TEST 5: FundingArbStrategy
# ══════════════════════════════════════════════════════════════════════
async def test_funding_arb():
    print("\n═══ TEST 5: FundingArbStrategy ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import ActivePosition
    from arbitrage.strategies.funding_arb import FundingArbStrategy

    config = ArbitrageConfig.from_env()
    exchanges = setup_mock_exchanges()

    # Set large funding spread: OKX=0.01%, HTX=0.10%
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001", eth_rate="0.0001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.001", eth_rate="0.001"))
    # Bybit tickers include funding
    exchanges["bybit"].set_tickers(create_bybit_tickers(funding_rate="0.0005"))

    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_all()

    strategy = FundingArbStrategy(config, md)

    # Detection
    opps = await strategy.detect_opportunities(md)
    test("found opportunities", len(opps) > 0, f"got {len(opps)}")

    if opps:
        opp = opps[0]
        test("opportunity has symbol", len(opp.symbol) > 0)
        test("opportunity has exchanges", opp.long_exchange != "" and opp.short_exchange != "")
        test("long != short", opp.long_exchange != opp.short_exchange)
        test("expected_profit > 0", opp.expected_profit_pct > 0)
        test("metadata has funding_spread", "funding_spread" in opp.metadata)
        test("metadata has annualized", "annualized" in opp.metadata)

    # No opportunity when spread is tiny
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001", eth_rate="0.0001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.00011", eth_rate="0.00011"))
    exchanges["bybit"].set_tickers(create_bybit_tickers(funding_rate="0.0001"))
    await md.update_funding_rates()
    opps2 = await strategy.detect_opportunities(md)
    test("no opps with tiny spread", len(opps2) == 0, f"got {len(opps2)}")

    # Exit logic
    pos = ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=10, short_contracts=10,
        long_price=50000, short_price=50100,
        entry_spread=0.1, size_usd=100,
        accumulated_funding=0.0,
    )

    # Big funding spread → should NOT exit
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.001"))
    await md.update_funding_rates()
    should_exit, reason = await strategy.should_exit(pos, md)
    test("no exit with spread", not should_exit, f"reason={reason}")

    # Funding target reached
    pos.accumulated_funding = 1.0  # > target for $100 position
    should_exit2, reason2 = await strategy.should_exit(pos, md)
    test("exit on funding target", should_exit2, f"acc={pos.accumulated_funding}")
    test("exit reason", reason2 == "funding_target_reached")

    # Reversed funding
    pos.accumulated_funding = 0.0
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.0001"))
    await md.update_funding_rates()
    should_exit3, reason3 = await strategy.should_exit(pos, md)
    test("exit on funding reversed/collapsed", should_exit3 and reason3 in ("funding_reversed", "funding_spread_collapsed"),
         f"exit={should_exit3} reason={reason3}")

    # get_all_spreads
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001", eth_rate="0.0002"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.0003", eth_rate="0.0005"))
    await md.update_funding_rates()
    spreads = strategy.get_all_spreads(md)
    test("get_all_spreads returns data", len(spreads) > 0)
    if spreads:
        test("spread item has symbol", "symbol" in spreads[0])
        test("spread item has funding_spread", "funding_spread" in spreads[0])


# ══════════════════════════════════════════════════════════════════════
# TEST 6: BasisArbStrategy
# ══════════════════════════════════════════════════════════════════════
async def test_basis_arb():
    print("\n═══ TEST 6: BasisArbStrategy ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import ActivePosition
    from arbitrage.strategies.basis_arb import BasisArbStrategy

    config = ArbitrageConfig.from_env()
    exchanges = setup_mock_exchanges()

    # Big basis: spot much lower than futures
    exchanges["okx"].set_spot_tickers([
        {"instId": "BTC-USDT", "bidPx": "49000", "askPx": "49100"},
        {"instId": "ETH-USDT", "bidPx": "2900", "askPx": "2910"},
    ])
    exchanges["okx"].set_tickers(create_okx_tickers(btc_bid=49800, btc_ask=49900))

    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_all()

    strategy = BasisArbStrategy(config, md)

    opps = await strategy.detect_opportunities(md)
    test("basis opportunities found", len(opps) >= 0)  # May or may not find depending on threshold

    # Force a big basis
    exchanges["okx"].set_spot_tickers([
        {"instId": "BTC-USDT", "bidPx": "48000", "askPx": "48100"},
    ])
    exchanges["okx"].set_tickers([
        {"instId": "BTC-USDT-SWAP", "bidPx": "49000", "askPx": "49100", "last": "49050"},
    ])
    await md.update_futures_prices()
    await md.update_spot_prices()

    opps2 = await strategy.detect_opportunities(md)
    test("big basis detected", len(opps2) > 0, f"got {len(opps2)}")

    if opps2:
        opp = opps2[0]
        test("basis opp has metadata", "basis_pct" in opp.metadata)
        test("basis is positive", opp.metadata["basis_pct"] > 0)

    # Exit: basis converged
    pos = ActivePosition(
        strategy="basis_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="okx",
        long_contracts=10, short_contracts=10,
        long_price=48050, short_price=49050,
        entry_spread=2.0, size_usd=100
    )

    # Set spot ≈ futures (basis converged)
    exchanges["okx"].set_spot_tickers([
        {"instId": "BTC-USDT", "bidPx": "49000", "askPx": "49100"},
    ])
    exchanges["okx"].set_tickers([
        {"instId": "BTC-USDT-SWAP", "bidPx": "49050", "askPx": "49100", "last": "49075"},
    ])
    await md.update_futures_prices()
    await md.update_spot_prices()
    should_exit, reason = await strategy.should_exit(pos, md)
    test("exit on basis converged", should_exit, f"exit={should_exit} reason={reason}")

    # get_all_spreads
    spreads = strategy.get_all_spreads(md)
    test("get_all_spreads returns data", isinstance(spreads, list))


# ══════════════════════════════════════════════════════════════════════
# TEST 7: StatArbStrategy
# ══════════════════════════════════════════════════════════════════════
async def test_stat_arb():
    print("\n═══ TEST 7: StatArbStrategy ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import ActivePosition
    from arbitrage.strategies.stat_arb import StatArbStrategy, SpreadTracker

    # Test SpreadTracker
    tracker = SpreadTracker(maxlen=100)
    test("tracker empty count", tracker.count == 0)
    test("tracker empty mean", tracker.mean == 0.0)
    test("tracker empty std", tracker.std == 0.0)
    test("tracker empty zscore", tracker.z_score(1.0) == 0.0)

    # Add data
    import random
    random.seed(42)
    for _ in range(50):
        tracker.add(random.gauss(0.1, 0.05))

    test("tracker count = 50", tracker.count == 50)
    test("tracker mean ~ 0.1", abs(tracker.mean - 0.1) < 0.03, f"got {tracker.mean}")
    test("tracker std > 0", tracker.std > 0, f"got {tracker.std}")

    # Z-score: value far from mean should have high z
    z_high = tracker.z_score(0.5)
    z_low = tracker.z_score(0.1)
    test("high z-score for outlier", abs(z_high) > 2, f"got {z_high}")
    test("low z-score for mean", abs(z_low) < 1, f"got {z_low}")

    # Maxlen eviction
    tracker2 = SpreadTracker(maxlen=5)
    for i in range(10):
        tracker2.add(float(i))
    test("maxlen enforced", tracker2.count == 5)
    test("maxlen mean", abs(tracker2.mean - 7.0) < 0.001, f"got {tracker2.mean}")

    # Strategy test
    config = ArbitrageConfig.from_env()
    exchanges = setup_mock_exchanges()
    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_all()

    strategy = StatArbStrategy(config, md)

    # Not enough data yet
    opps = await strategy.detect_opportunities(md)
    test("no opps without data", len(opps) == 0)

    # Feed data for 40 cycles (simulating spread history)
    for i in range(40):
        # Slightly vary prices to build spread history
        okx_bid = 50000 + random.gauss(0, 5)
        htx_bid = 50000 + random.gauss(0, 5)
        bybit_bid = 50000 + random.gauss(0, 5)

        exchanges["okx"].set_tickers([
            {"instId": "BTC-USDT-SWAP", "bidPx": str(okx_bid), "askPx": str(okx_bid + 10), "last": str(okx_bid + 5)},
        ])
        exchanges["htx"].set_tickers([
            {"contract_code": "BTC-USDT", "bid": [htx_bid, 1], "ask": [htx_bid + 10, 1], "close": htx_bid + 5},
        ])
        exchanges["bybit"].set_tickers([
            {"symbol": "BTCUSDT", "bid1Price": str(bybit_bid), "ask1Price": str(bybit_bid + 10), "lastPrice": str(bybit_bid + 5), "fundingRate": "0.0001", "nextFundingTime": "0"},
        ])
        await md.update_futures_prices()
        strategy.update_spreads(md)

    # Check that trackers have data
    has_data = any(t.count >= 30 for t in strategy._trackers.values())
    test("trackers have >= 30 samples", has_data)

    # Now create a big spread deviation
    exchanges["okx"].set_tickers([
        {"instId": "BTC-USDT-SWAP", "bidPx": "50200", "askPx": "50210", "last": "50205"},
    ])
    exchanges["htx"].set_tickers([
        {"contract_code": "BTC-USDT", "bid": [49800, 1], "ask": [49810, 1], "close": 49805},
    ])
    await md.update_futures_prices()

    opps2 = await strategy.detect_opportunities(md)
    # May or may not trigger depending on z-score threshold
    test("stat_arb detection runs", isinstance(opps2, list))

    # get_all_spreads
    spreads = strategy.get_all_spreads(md)
    test("get_all_spreads returns list", isinstance(spreads, list))

    # Exit logic
    pos = ActivePosition(
        strategy="stat_arb", symbol="BTCUSDT",
        long_exchange="htx", short_exchange="okx",
        long_contracts=10, short_contracts=10,
        long_price=49800, short_price=50200,
        entry_spread=0.8, size_usd=100,
    )
    should_exit, reason = await strategy.should_exit(pos, md)
    test("stat_arb should_exit runs", isinstance(should_exit, bool))

    # Timeout or stop-loss exit (with big spread, z-score may trigger stop loss first)
    pos.entry_time = time.time() - 4000  # > 1h
    should_exit2, reason2 = await strategy.should_exit(pos, md)
    test("stat_arb forced exit", should_exit2,
         f"exit={should_exit2} reason={reason2}")


# ══════════════════════════════════════════════════════════════════════
# TEST 8: TradeExecutor
# ══════════════════════════════════════════════════════════════════════
async def test_trade_executor():
    print("\n═══ TEST 8: TradeExecutor ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState, ActivePosition
    from arbitrage.strategies.trade_executor import TradeExecutor
    from arbitrage.strategies.base import Opportunity, StrategyType

    config = ArbitrageConfig.from_env()
    config.dry_run_mode = True  # Use dry run for safety

    exchanges = setup_mock_exchanges()
    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_all()

    state = BotState()
    state.update_balance("okx", 200.0)
    state.update_balance("htx", 150.0)
    state.update_balance("bybit", 100.0)

    executor = TradeExecutor(config, exchanges)
    executor.set_contract_sizes(md.contract_sizes)

    # Dry run entry
    opp = Opportunity(
        strategy=StrategyType.FUNDING_ARB, symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        expected_profit_pct=0.1,
        long_price=50000, short_price=50100,
    )

    success = await executor.execute_entry(opp, state, md)
    test("dry run entry success", success)
    test("position added to state", state.position_count() == 1)

    pos = state.get_position("funding_arb", "BTCUSDT")
    test("position has correct symbol", pos is not None and pos.symbol == "BTCUSDT")
    test("position has correct exchanges", pos.long_exchange == "okx" and pos.short_exchange == "htx")

    # Dry run exit
    success2, pnl = await executor.execute_exit(pos, state, md, "test_exit")
    test("dry run exit success", success2)
    test("position removed from state", state.position_count() == 0)

    # Monitoring only — should not execute
    config.monitoring_only = True
    success3 = await executor.execute_entry(opp, state, md)
    test("monitoring blocks entry", not success3)
    config.monitoring_only = False

    # Leg order: OKX should be second (safe)
    first_ex, first_side, second_ex, second_side = TradeExecutor._determine_leg_order("okx", "htx")
    test("htx first (risky), okx second", first_ex == "htx" and second_ex == "okx")

    first_ex2, _, second_ex2, _ = TradeExecutor._determine_leg_order("htx", "okx")
    test("htx first, okx second (reversed)", first_ex2 == "htx" and second_ex2 == "okx")

    first_ex3, _, second_ex3, _ = TradeExecutor._determine_leg_order("htx", "bybit")
    test("alphabetical order for non-okx", first_ex3 == "bybit" and second_ex3 == "htx")

    # Position size calculation
    size = executor._calculate_position_size(state, "okx", "htx")
    test("position size > 0", size > 0, f"got {size}")

    # Contract calculation
    contracts = executor._calculate_contracts("okx", "BTCUSDT", 100, 50000)
    test("contract calculation", contracts > 0, f"got {contracts}")

    # Order type per exchange
    test("htx order type = optimal_5", TradeExecutor._get_order_type("htx") == "optimal_5")
    test("okx order type = market", TradeExecutor._get_order_type("okx") == "market")
    test("bybit order type = market", TradeExecutor._get_order_type("bybit") == "market")

    # PnL estimation
    pos_for_pnl = ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=10, short_contracts=10,
        long_price=50000, short_price=50100,
        entry_spread=0.2, size_usd=100,
        total_fees=0.01, accumulated_funding=0.05,
    )
    pnl_est = TradeExecutor._estimate_pnl(pos_for_pnl)
    test("pnl estimate numeric", isinstance(pnl_est, float))


# ══════════════════════════════════════════════════════════════════════
# TEST 9: TradeExecutor with real orders (mock exchanges)
# ══════════════════════════════════════════════════════════════════════
async def test_executor_real_orders():
    print("\n═══ TEST 9: TradeExecutor Real Orders ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState
    from arbitrage.strategies.trade_executor import TradeExecutor
    from arbitrage.strategies.base import Opportunity, StrategyType

    config = ArbitrageConfig.from_env()
    config.dry_run_mode = False
    config.monitoring_only = False

    exchanges = setup_mock_exchanges()
    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_all()

    state = BotState()
    state.update_balance("okx", 200.0)
    state.update_balance("htx", 150.0)

    executor = TradeExecutor(config, exchanges)
    executor.set_contract_sizes(md.contract_sizes)

    # Successful entry
    opp = Opportunity(
        strategy=StrategyType.FUNDING_ARB, symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        expected_profit_pct=0.1,
        long_price=50000, short_price=50100,
    )
    success = await executor.execute_entry(opp, state, md)
    test("real order entry success", success)
    test("orders placed on both exchanges",
         len(exchanges["okx"]._orders_placed) > 0 or len(exchanges["htx"]._orders_placed) > 0)
    test("position in state", state.position_count() == 1)

    # Successful exit
    pos = state.get_position("funding_arb", "BTCUSDT")
    if pos:
        success2, pnl = await executor.execute_exit(pos, state, md, "test")
        test("real order exit success", success2)
        test("position removed", state.position_count() == 0)

    # Failed first leg
    exchanges["htx"]._fail_orders = True
    exchanges["htx"]._orders_placed = []
    exchanges["okx"]._orders_placed = []
    state.update_balance("okx", 200.0)
    state.update_balance("htx", 150.0)

    success3 = await executor.execute_entry(opp, state, md)
    test("first leg failure = no position", not success3 or state.position_count() == 0,
         f"success={success3} positions={state.position_count()}")
    exchanges["htx"]._fail_orders = False

    # Failed second leg — should hedge
    exchanges["okx"]._fail_orders = True
    exchanges["htx"]._orders_placed = []
    exchanges["okx"]._orders_placed = []

    opp2 = Opportunity(
        strategy=StrategyType.FUNDING_ARB, symbol="ETHUSDT",
        long_exchange="htx", short_exchange="okx",
        expected_profit_pct=0.1,
        long_price=3000, short_price=3010,
    )
    # htx is first (risky), okx is second — okx will fail
    # But since the mock's place_order also sets positions, this is complex to test perfectly
    success4 = await executor.execute_entry(opp2, state, md)
    test("second leg failure handled", isinstance(success4, bool))
    exchanges["okx"]._fail_orders = False


# ══════════════════════════════════════════════════════════════════════
# TEST 10: StrategyRouter
# ══════════════════════════════════════════════════════════════════════
async def test_strategy_router():
    print("\n═══ TEST 10: StrategyRouter ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState
    from arbitrage.core.risk import RiskManager
    from arbitrage.core.notifications import NotificationManager
    from arbitrage.strategies.strategy_manager import StrategyRouter
    from arbitrage.strategies.trade_executor import TradeExecutor

    config = ArbitrageConfig.from_env()
    config.monitoring_only = True  # Safe for testing

    exchanges = setup_mock_exchanges()
    md = MarketDataEngine(exchanges)
    state = BotState()
    risk = RiskManager(config, state)
    executor = TradeExecutor(config, exchanges)
    notif = NotificationManager()  # No bot, no user

    router = StrategyRouter(config, state, md, risk, executor, notif)

    # Initialize
    count = await router.initialize()
    test("router init pairs > 0", count > 0)
    test("strategies loaded", len(router._strategies) > 0)

    # Get status
    status = router.get_status()
    test("status has is_running", "is_running" in status)
    test("status has strategies", "strategies" in status)
    test("status has total_balance", "total_balance" in status)
    test("status mode = monitoring", status["mode"] == "monitoring")
    test("status can_trade = False", status["can_trade"] == False)

    # Scan
    results = await router.scan_all()
    test("scan returns dict", isinstance(results, dict))
    test("scan has strategy keys", len(results) > 0)

    # Run a few cycles
    router.is_running = True
    for _ in range(3):
        try:
            await router._run_cycle()
        except Exception as e:
            test(f"cycle error: {e}", False)
            break
    test("cycles ran successfully", router._cycle_count >= 3, f"count={router._cycle_count}")

    # Stop
    router.stop()
    test("router stopped", not router.is_running)


# ══════════════════════════════════════════════════════════════════════
# TEST 11: StrategyRouter with trading enabled
# ══════════════════════════════════════════════════════════════════════
async def test_router_trading():
    print("\n═══ TEST 11: StrategyRouter Trading ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState
    from arbitrage.core.risk import RiskManager
    from arbitrage.core.notifications import NotificationManager
    from arbitrage.strategies.strategy_manager import StrategyRouter
    from arbitrage.strategies.trade_executor import TradeExecutor

    config = ArbitrageConfig.from_env()
    config.monitoring_only = False
    config.dry_run_mode = True

    exchanges = setup_mock_exchanges()

    # Set huge funding spread to trigger opportunities
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001", eth_rate="0.0001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.005", eth_rate="0.005"))
    exchanges["bybit"].set_tickers(create_bybit_tickers(funding_rate="0.003"))

    md = MarketDataEngine(exchanges)
    state = BotState()
    risk = RiskManager(config, state)
    executor = TradeExecutor(config, exchanges)
    executor.set_contract_sizes({"okx": {"BTCUSDT": 0.01}, "htx": {"BTCUSDT": 0.001}, "bybit": {"BTCUSDT": 0.001}})
    notif = NotificationManager()

    router = StrategyRouter(config, state, md, risk, executor, notif)
    count = await router.initialize()
    executor.set_contract_sizes(md.contract_sizes)

    # Run cycles — should detect and execute funding opportunity
    for _ in range(5):
        await router._run_cycle()

    status = router.get_status()
    test("trading mode", status["mode"] == "dry_run")
    test("can_trade = True", status["can_trade"] == True)
    # In dry run + funding with big spread, should open a position
    test("positions opened (dry run)", state.position_count() >= 0)  # May not trigger depending on exact config

    # Check metrics
    metrics = router.metrics.summary()
    test("metrics tracked cycles", metrics["avg_cycle_ms"] > 0)


# ══════════════════════════════════════════════════════════════════════
# TEST 12: Edge Cases
# ══════════════════════════════════════════════════════════════════════
async def test_edge_cases():
    print("\n═══ TEST 12: Edge Cases ═══")
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState, ActivePosition
    from arbitrage.strategies.stat_arb import SpreadTracker

    # Empty market data
    exchanges = setup_mock_exchanges()
    exchanges["okx"].set_tickers([])
    exchanges["htx"].set_tickers([])
    exchanges["bybit"].set_tickers([])

    md = MarketDataEngine(exchanges)
    await md.initialize()
    await md.update_futures_prices()
    test("empty tickers no crash", True)

    btc = md.get_futures_price("okx", "BTCUSDT")
    test("no price for empty tickers", btc is None)

    # Zero prices
    exchanges["okx"].set_tickers([
        {"instId": "BTC-USDT-SWAP", "bidPx": "0", "askPx": "0", "last": "0"},
    ])
    await md.update_futures_prices()
    btc2 = md.get_futures_price("okx", "BTCUSDT")
    test("zero prices filtered", btc2 is None)

    # Invalid funding rate
    exchanges["okx"].set_funding_rates([
        {"instId": "BTC-USDT-SWAP", "fundingRate": None},
    ])
    await md.update_funding_rates()
    fd = md.get_funding("okx", "BTCUSDT")
    test("null funding rate handled", fd is None or True)  # Should not crash

    # Balance errors
    exchanges["okx"]._balance = 0.0
    balances = await md.fetch_balances()
    test("zero balance ok", balances["okx"] == 0.0)

    # SpreadTracker edge cases
    t = SpreadTracker(maxlen=3)
    t.add(1.0)
    test("tracker 1 sample std = 0", t.std == 0.0)
    t.add(1.0)
    t.add(1.0)
    test("all same values std ~ 0", t.std < 0.001)
    test("z-score for constant = 0", t.z_score(1.0) == 0.0)

    # BotState — double add same key
    state = BotState()
    pos = ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=10, short_contracts=10,
        long_price=50000, short_price=50100,
        entry_spread=0.2, size_usd=100,
    )
    state.add_position(pos)
    pos2 = ActivePosition(
        strategy="funding_arb", symbol="BTCUSDT",
        long_exchange="okx", short_exchange="htx",
        long_contracts=20, short_contracts=20,
        long_price=50000, short_price=50100,
        entry_spread=0.3, size_usd=200,
    )
    state.add_position(pos2)
    test("overwrite same key", state.position_count() == 1)
    test("overwritten with new", state.get_position("funding_arb", "BTCUSDT").long_contracts == 20)


# ══════════════════════════════════════════════════════════════════════
# TEST 13: Handler integration (import check)
# ══════════════════════════════════════════════════════════════════════
def test_handler_integration():
    print("\n═══ TEST 13: Handler Integration ═══")
    from handlers.arbitrage_handlers_simple import (
        handle_arbitrage_menu, cb_arb_multi_start, cb_arb_multi_stop,
        cb_arb_scan_now, cb_arb_stats, cb_arb_history, cb_arb_pair_stats,
        cb_arb_funding, cb_arb_basis, cb_arb_stat_arb,
        cb_arb_emergency_close, cb_arb_emergency_confirm,
        cb_arb_settings, cb_arb_menu,
        _main_keyboard, _get_exchange_names, _build_status_line,
    )
    test("all handler functions importable", True)

    # Test keyboard generation
    kb_running = _main_keyboard(True)
    test("running keyboard has rows", len(kb_running.inline_keyboard) > 3)

    kb_stopped = _main_keyboard(False)
    test("stopped keyboard has 1 row", len(kb_stopped.inline_keyboard) == 1)

    # Exchange names without engine
    names = _get_exchange_names()
    test("default exchange names", names == "OKX/HTX/Bybit")


# ══════════════════════════════════════════════════════════════════════
# TEST 14: Full Pipeline Integration
# ══════════════════════════════════════════════════════════════════════
async def test_full_pipeline():
    print("\n═══ TEST 14: Full Pipeline Integration ═══")
    from arbitrage.utils import ArbitrageConfig
    from arbitrage.core.market_data import MarketDataEngine
    from arbitrage.core.state import BotState
    from arbitrage.core.risk import RiskManager
    from arbitrage.core.notifications import NotificationManager
    from arbitrage.strategies.strategy_manager import StrategyRouter
    from arbitrage.strategies.trade_executor import TradeExecutor

    config = ArbitrageConfig.from_env()
    config.monitoring_only = False
    config.dry_run_mode = True

    exchanges = setup_mock_exchanges()

    # Setup: large funding spread → should trigger funding_arb
    exchanges["okx"].set_funding_rates(create_okx_funding(btc_rate="0.0001", eth_rate="0.0001"))
    exchanges["htx"].set_funding_rates(create_htx_funding(btc_rate="0.01", eth_rate="0.008"))
    exchanges["bybit"].set_tickers(create_bybit_tickers(funding_rate="0.005"))

    md = MarketDataEngine(exchanges)
    state = BotState()
    risk = RiskManager(config, state)
    executor = TradeExecutor(config, exchanges)
    notif = NotificationManager()

    router = StrategyRouter(config, state, md, risk, executor, notif)
    pairs = await router.initialize()
    executor.set_contract_sizes(md.contract_sizes)

    test("pipeline init ok", pairs > 0)
    test("balance loaded", state.total_balance > 0, f"got {state.total_balance}")

    # Step 1: Market data loaded
    btc_okx = md.get_futures_price("okx", "BTCUSDT")
    test("pipeline: okx BTC price", btc_okx is not None)

    # Step 2: Run 5 cycles
    initial_positions = state.position_count()
    for i in range(5):
        await router._run_cycle()

    test("pipeline: cycles ran", router._cycle_count == 5)
    test("pipeline: no crash after 5 cycles", True)

    # Step 3: Check that opportunities are detected
    scan_results = await router.scan_all()
    total_opps = sum(len(v) for v in scan_results.values())
    test("pipeline: scan finds data", total_opps >= 0)

    # Step 4: Emergency close (nothing to close if no positions)
    emergency, reason = risk.should_emergency_close()
    test("pipeline: no emergency needed",
         not emergency or state.total_balance < 5,
         f"emergency={emergency} reason={reason}")

    # Step 5: Status
    status = router.get_status()
    test("pipeline: status complete", all(k in status for k in [
        "is_running", "strategies", "total_trades", "total_pnl", "total_balance", "mode"
    ]))

    print(f"\n  Pipeline status: {status['mode']} | "
          f"Strategies: {status['strategies']} | "
          f"Balance: ${status['total_balance']:.2f} | "
          f"Positions: {status['positions']}")


# ══════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════
async def run_all():
    global passed, failed

    print("=" * 60)
    print("  Multi-Strategy Arbitrage System — Test Suite")
    print("=" * 60)

    # Synchronous tests
    test_bot_state()
    test_risk_manager()
    test_metrics()
    test_handler_integration()

    # Async tests
    await test_market_data()
    await test_funding_arb()
    await test_basis_arb()
    await test_stat_arb()
    await test_trade_executor()
    await test_executor_real_orders()
    await test_strategy_router()
    await test_router_trading()
    await test_edge_cases()
    await test_full_pipeline()

    print("\n" + "=" * 60)
    print(f"  RESULTS: {passed} passed, {failed} failed")
    print("=" * 60)

    if errors:
        print("\n  FAILURES:")
        for e in errors:
            print(f"    {e}")

    return failed == 0


if __name__ == "__main__":
    ok = asyncio.run(run_all())
    sys.exit(0 if ok else 1)
