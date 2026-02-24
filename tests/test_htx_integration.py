"""
Комплексные тесты для HTX интеграции (OKX <-> HTX арбитраж)
Запуск: python -m pytest tests/ -v
"""
import asyncio
import pytest
import sys
import os

# Добавляем корень проекта в путь
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ══════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════

@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def mock_config():
    """Конфигурация в mock режиме"""
    from arbitrage.utils import ArbitrageConfig
    config = ArbitrageConfig()
    config.symbol = "BTCUSDT"
    config.position_size = 0.01
    config.leverage = 3
    config.entry_threshold = 0.25
    config.exit_threshold = 0.05
    config.mock_mode = True
    config.dry_run_mode = True
    config.monitoring_only = False
    config.min_spread = 0.05
    config.min_opportunity_lifetime = 1
    config.update_interval = 1
    config.spread_change_threshold = 0.3
    config.renotify_interval = 60
    config.order_timeout_ms = 200
    config.htx_api_key = "test_key"
    config.htx_api_secret = "test_secret"
    config.htx_testnet = False
    config.okx_api_key = "test_key"
    config.okx_api_secret = "test_secret"
    config.okx_passphrase = "test_pass"
    config.okx_testnet = False
    return config


@pytest.fixture
def mock_okx_client(mock_config):
    from arbitrage.test.mock_exchanges import MockOKXRestClient
    from arbitrage.utils import ExchangeConfig
    cfg = ExchangeConfig(api_key="k", api_secret="s", testnet=False)
    return MockOKXRestClient(cfg)


@pytest.fixture
def mock_htx_client(mock_config):
    from arbitrage.test.mock_exchanges import MockHTXRestClient
    from arbitrage.utils import ExchangeConfig
    cfg = ExchangeConfig(api_key="k", api_secret="s", testnet=False)
    return MockHTXRestClient(cfg)


@pytest.fixture
def bot_state():
    from arbitrage.core.state import BotState
    return BotState()


# ══════════════════════════════════════════════════════════
# 1. Тесты HTX WebSocket
# ══════════════════════════════════════════════════════════

class TestHTXWebSocket:
    def test_import(self):
        """HTX WebSocket импортируется без ошибок"""
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        assert HTXWebSocket is not None

    def test_init(self):
        """HTX WebSocket создаётся корректно"""
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        ws = HTXWebSocket("BTCUSDT", testnet=False)
        assert ws.symbol == "BTCUSDT"
        assert ws.htx_symbol == "BTC-USDT"
        assert not ws.running
        assert ws.ws is None

    def test_symbol_conversion(self):
        """Конвертация BTCUSDT -> BTC-USDT"""
        from arbitrage.exchanges.htx_ws import _usdt_to_htx
        assert _usdt_to_htx("BTCUSDT") == "BTC-USDT"
        assert _usdt_to_htx("ETHUSDT") == "ETH-USDT"
        assert _usdt_to_htx("BTC-USDT") == "BTC-USDT"  # Уже в правильном формате

    def test_is_connected_initial(self):
        """Начальное состояние: не подключено"""
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        ws = HTXWebSocket("BTCUSDT")
        assert not ws.is_connected()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self):
        """Отключение не вызывает ошибок если не подключено"""
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        ws = HTXWebSocket("BTCUSDT")
        await ws.disconnect()  # Не должно бросать исключение

    def test_decompress_json(self):
        """Тест декомпрессии gzip сообщений"""
        import gzip
        import json
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        ws = HTXWebSocket("BTCUSDT")

        data = {"ping": 1234567890}
        raw = gzip.compress(json.dumps(data).encode())
        result = ws._decompress(raw)
        assert result == data

    def test_decompress_string(self):
        """Тест парсинга строкового JSON"""
        import json
        from arbitrage.exchanges.htx_ws import HTXWebSocket
        ws = HTXWebSocket("BTCUSDT")

        data = {"pong": 123}
        raw = json.dumps(data)
        result = ws._decompress(raw)
        assert result == data


# ══════════════════════════════════════════════════════════
# 2. Тесты Mock HTX WebSocket
# ══════════════════════════════════════════════════════════

class TestMockHTXWebSocket:
    def test_import(self):
        """MockHTXWebSocket импортируется"""
        from arbitrage.test.mock_exchanges import MockHTXWebSocket
        assert MockHTXWebSocket is not None

    def test_init(self):
        """MockHTXWebSocket создаётся корректно"""
        from arbitrage.test.mock_exchanges import MockHTXWebSocket
        ws = MockHTXWebSocket("BTCUSDT")
        assert ws.symbol == "BTCUSDT"
        assert not ws.running

    @pytest.mark.asyncio
    async def test_send_orderbooks(self):
        """MockHTXWebSocket отправляет orderbook данные"""
        from arbitrage.test.mock_exchanges import MockHTXWebSocket
        ws = MockHTXWebSocket("BTCUSDT")

        received = []

        async def callback(ob):
            received.append(ob)
            if len(received) >= 2:
                ws.running = False  # Останавливаем после получения данных

        task = asyncio.create_task(ws.connect(callback))
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=3.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        finally:
            await ws.disconnect()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        assert len(received) >= 1
        ob = received[0]
        assert ob["exchange"] == "htx"
        assert "bids" in ob
        assert "asks" in ob
        assert len(ob["bids"]) > 0

    @pytest.mark.asyncio
    async def test_disconnect(self):
        """MockHTXWebSocket корректно отключается"""
        from arbitrage.test.mock_exchanges import MockHTXWebSocket
        ws = MockHTXWebSocket("BTCUSDT")

        task = asyncio.create_task(ws.connect(lambda ob: None))
        await asyncio.sleep(0.1)
        await ws.disconnect()
        assert not ws.running
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


# ══════════════════════════════════════════════════════════
# 3. Тесты Mock HTX REST
# ══════════════════════════════════════════════════════════

class TestMockHTXRestClient:
    def test_init(self, mock_htx_client):
        assert mock_htx_client is not None

    @pytest.mark.asyncio
    async def test_get_instruments(self, mock_htx_client):
        result = await mock_htx_client.get_instruments()
        assert result["status"] == "ok"
        data = result["data"]
        assert len(data) > 0
        # Проверяем формат HTX
        first = data[0]
        assert "contract_code" in first
        assert "-USDT" in first["contract_code"]

    @pytest.mark.asyncio
    async def test_get_tickers(self, mock_htx_client):
        result = await mock_htx_client.get_tickers()
        assert result["status"] == "ok"
        data = result["data"]
        assert len(data) > 0
        first = data[0]
        assert "bid" in first
        assert "ask" in first
        assert isinstance(first["bid"], list)
        assert len(first["bid"]) == 2

    @pytest.mark.asyncio
    async def test_get_balance(self, mock_htx_client):
        result = await mock_htx_client.get_balance()
        assert result["status"] == "ok"
        data = result["data"]
        assert len(data) > 0
        assert "margin_available" in data[0]
        assert data[0]["margin_asset"] == "USDT"

    @pytest.mark.asyncio
    async def test_place_order_success(self, mock_htx_client):
        """95% вероятность успеха"""
        successes = 0
        for _ in range(20):
            result = await mock_htx_client.place_order(
                "BTCUSDT", "buy", 0.01, "limit", 50000.0
            )
            if result["status"] == "ok":
                successes += 1
        # Ожидаем большинство успешных
        assert successes > 10

    @pytest.mark.asyncio
    async def test_place_order_response_format(self, mock_htx_client):
        """Проверяем формат ответа HTX"""
        for _ in range(10):
            result = await mock_htx_client.place_order(
                "BTCUSDT", "buy", 0.01, "limit", 50000.0
            )
            if result["status"] == "ok":
                assert "data" in result
                assert "order_id" in result["data"]
                break

    @pytest.mark.asyncio
    async def test_set_leverage(self, mock_htx_client):
        result = await mock_htx_client.set_leverage("BTCUSDT", 5)
        assert result["status"] == "ok"
        assert result["data"]["lever_rate"] == 5

    @pytest.mark.asyncio
    async def test_cancel_order(self, mock_htx_client):
        result = await mock_htx_client.cancel_order("BTCUSDT", "123456")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_positions(self, mock_htx_client):
        result = await mock_htx_client.get_positions()
        assert result["status"] == "ok"
        assert "data" in result

    @pytest.mark.asyncio
    async def test_get_orderbook(self, mock_htx_client):
        result = await mock_htx_client.get_orderbook("BTCUSDT")
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_get_funding_rates(self, mock_htx_client):
        result = await mock_htx_client.get_funding_rates()
        assert result["status"] == "ok"
        data = result["data"]
        assert len(data) > 0
        assert "funding_rate" in data[0]

    @pytest.mark.asyncio
    async def test_close(self, mock_htx_client):
        await mock_htx_client.close()  # Не должно бросать исключение


# ══════════════════════════════════════════════════════════
# 4. Тесты BotState с HTX
# ══════════════════════════════════════════════════════════

class TestBotStateHTX:
    def test_initial_state(self, bot_state):
        """Начальное состояние корректно"""
        assert bot_state.okx_orderbook is None
        assert bot_state.htx_orderbook is None
        assert not bot_state.is_connected["okx"]
        assert not bot_state.is_connected["htx"]
        assert not bot_state.is_both_connected()

    @pytest.mark.asyncio
    async def test_update_okx_orderbook(self, bot_state):
        """Обновление OKX стакана"""
        await bot_state.update_orderbook({
            "exchange": "okx",
            "symbol": "BTCUSDT",
            "bids": [[50000.0, 0.5], [49999.0, 0.3]],
            "asks": [[50001.0, 0.4], [50002.0, 0.2]],
            "timestamp": 1000000
        })
        assert bot_state.okx_orderbook is not None
        assert bot_state.okx_orderbook.best_bid == 50000.0
        assert bot_state.okx_orderbook.best_ask == 50001.0
        assert bot_state.is_connected["okx"]

    @pytest.mark.asyncio
    async def test_update_htx_orderbook(self, bot_state):
        """Обновление HTX стакана"""
        await bot_state.update_orderbook({
            "exchange": "htx",
            "symbol": "BTCUSDT",
            "bids": [[50100.0, 0.5], [50099.0, 0.3]],
            "asks": [[50101.0, 0.4], [50102.0, 0.2]],
            "timestamp": 1000000
        })
        assert bot_state.htx_orderbook is not None
        assert bot_state.htx_orderbook.best_bid == 50100.0
        assert bot_state.htx_orderbook.best_ask == 50101.0
        assert bot_state.is_connected["htx"]

    @pytest.mark.asyncio
    async def test_both_connected(self, bot_state):
        """is_both_connected True когда подключены обе биржи"""
        assert not bot_state.is_both_connected()

        await bot_state.update_orderbook({
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50000.0, 0.5]], "asks": [[50001.0, 0.5]],
            "timestamp": 1000
        })
        assert not bot_state.is_both_connected()  # Только одна

        await bot_state.update_orderbook({
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[50100.0, 0.5]], "asks": [[50101.0, 0.5]],
            "timestamp": 1000
        })
        assert bot_state.is_both_connected()  # Обе!

    def test_balance_update(self, bot_state):
        """Обновление баланса"""
        bot_state.update_balance("okx", 1000.0)
        assert bot_state.okx_balance == 1000.0
        assert bot_state.total_balance == 1000.0

        bot_state.update_balance("htx", 2000.0)
        assert bot_state.htx_balance == 2000.0
        assert bot_state.total_balance == 3000.0

    def test_get_orderbooks(self, bot_state):
        """get_orderbooks возвращает (okx, htx)"""
        okx, htx = bot_state.get_orderbooks()
        assert okx is None
        assert htx is None

    def test_stats(self, bot_state):
        """Статистика формируется корректно"""
        stats = bot_state.get_stats()
        assert "total_trades" in stats
        assert "okx_balance" in stats
        assert "htx_balance" in stats
        assert "total_balance" in stats


# ══════════════════════════════════════════════════════════
# 5. Тесты spread расчёта
# ══════════════════════════════════════════════════════════

class TestSpreadCalculation:
    def test_calculate_spread_positive(self):
        """Положительный спред"""
        from arbitrage.utils import calculate_spread
        # HTX bid = 50100, OKX ask = 50000
        spread = calculate_spread(50100.0, 50000.0)
        assert spread > 0
        assert abs(spread - 0.2) < 0.01  # ~0.2%

    def test_calculate_spread_negative(self):
        """Отрицательный спред (нет возможности)"""
        from arbitrage.utils import calculate_spread
        # HTX bid = 49900, OKX ask = 50000
        spread = calculate_spread(49900.0, 50000.0)
        assert spread < 0

    def test_calculate_spread_zero_ask(self):
        """Нулевой знаменатель"""
        from arbitrage.utils import calculate_spread
        spread = calculate_spread(50000.0, 0.0)
        assert spread == 0.0

    def test_spread_direction(self):
        """Спред 1: LONG OKX, SHORT HTX"""
        from arbitrage.utils import calculate_spread
        okx_ask = 50000.0
        htx_bid = 50200.0
        spread1 = calculate_spread(htx_bid, okx_ask)  # HTX bid > OKX ask
        assert spread1 > 0  # Прибыльная возможность

        okx_bid = 50000.0
        htx_ask = 49800.0
        spread2 = calculate_spread(okx_bid, htx_ask)  # OKX bid > HTX ask
        assert spread2 > 0  # Обратная возможность


# ══════════════════════════════════════════════════════════
# 6. Тесты ExecutionManager
# ══════════════════════════════════════════════════════════

class TestExecutionManager:
    def test_import(self):
        from arbitrage.core.execution import ExecutionManager
        assert ExecutionManager is not None

    def test_init(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        from arbitrage.core.execution import ExecutionManager
        from arbitrage.core.state import BotState
        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        assert em.okx_client is mock_okx_client
        assert em.htx_client is mock_htx_client

    @pytest.mark.asyncio
    async def test_place_order_dry_run(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """В dry_run режиме ордер не размещается (мок результат)"""
        from arbitrage.core.execution import ExecutionManager
        mock_config.dry_run_mode = True

        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        result = await em._place_order("okx", "buy", 50000.0, 0.01)
        assert result["success"] is True
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_place_order_htx_dry_run(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """Dry run для HTX"""
        from arbitrage.core.execution import ExecutionManager
        mock_config.dry_run_mode = True

        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        result = await em._place_order("htx", "sell", 50000.0, 0.01)
        assert result["success"] is True
        assert result["dry_run"] is True

    @pytest.mark.asyncio
    async def test_execute_arbitrage_entry_dry_run(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """Dry run вход в позицию"""
        from arbitrage.core.execution import ExecutionManager
        mock_config.dry_run_mode = True

        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        success, msg = await em.execute_arbitrage_entry(
            long_exchange="okx",
            short_exchange="htx",
            long_price=50000.0,
            short_price=50200.0,
            size=0.01
        )
        assert success is True
        assert "Both positions opened" in msg or "Both" in msg

    @pytest.mark.asyncio
    async def test_execute_exit_not_in_position(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """Выход без открытой позиции"""
        from arbitrage.core.execution import ExecutionManager
        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        success, msg = await em.execute_arbitrage_exit()
        assert success is False
        assert "Not in position" in msg

    @pytest.mark.asyncio
    async def test_emergency_close_no_positions(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """Аварийное закрытие без позиций не вызывает ошибок"""
        from arbitrage.core.execution import ExecutionManager
        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        await em.emergency_close_all()  # Не должно бросать исключение


# ══════════════════════════════════════════════════════════
# 7. Тесты MultiPairArbitrageEngine
# ══════════════════════════════════════════════════════════

class TestMultiPairEngine:
    def test_import(self):
        from arbitrage.core.multi_pair_arbitrage import MultiPairArbitrageEngine, PairSpread
        assert MultiPairArbitrageEngine is not None
        assert PairSpread is not None

    def test_pair_spread_okx_long(self):
        """PairSpread.get_long_exchange() для okx_long"""
        from arbitrage.core.multi_pair_arbitrage import PairSpread
        ps = PairSpread(
            symbol="BTCUSDT", spread=0.2, direction="okx_long",
            okx_bid=50000, okx_ask=50001, htx_bid=50200, htx_ask=50201
        )
        assert ps.get_long_exchange() == "okx"
        assert ps.get_short_exchange() == "htx"
        assert ps.get_long_price() == 50001
        assert ps.get_short_price() == 50200

    def test_pair_spread_htx_long(self):
        """PairSpread.get_long_exchange() для htx_long"""
        from arbitrage.core.multi_pair_arbitrage import PairSpread
        ps = PairSpread(
            symbol="BTCUSDT", spread=0.2, direction="htx_long",
            okx_bid=50200, okx_ask=50201, htx_bid=50000, htx_ask=50001
        )
        assert ps.get_long_exchange() == "htx"
        assert ps.get_short_exchange() == "okx"
        assert ps.get_long_price() == 50001
        assert ps.get_short_price() == 50200

    @pytest.mark.asyncio
    async def test_calculate_spreads(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """calculate_spreads находит арбитражные возможности"""
        from arbitrage.core.multi_pair_arbitrage import MultiPairArbitrageEngine
        from arbitrage.core.risk import RiskManager
        from arbitrage.core.execution import ExecutionManager

        mock_config.mock_mode = True
        mock_config.min_spread = 0.01  # Низкий порог для теста

        risk = RiskManager(mock_config, bot_state)
        execution = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)

        engine = MultiPairArbitrageEngine(
            mock_config, bot_state, risk, execution,
            mock_okx_client, mock_htx_client
        )

        # Подготавливаем тестовые данные
        engine.monitored_pairs = {"BTCUSDT", "ETHUSDT"}
        engine.okx_prices = {
            "BTCUSDT": {"bid": 50000.0, "ask": 50001.0},
            "ETHUSDT": {"bid": 3000.0, "ask": 3001.0}
        }
        engine.htx_prices = {
            "BTCUSDT": {"bid": 50200.0, "ask": 50201.0},  # HTX выше -> HTX SHORT, OKX LONG
            "ETHUSDT": {"bid": 2995.0, "ask": 2996.0}    # HTX ниже -> OKX SHORT, HTX LONG
        }

        spreads = await engine.calculate_spreads()
        assert len(spreads) >= 1

        # Спреды отсортированы по убыванию
        for i in range(len(spreads) - 1):
            assert spreads[i].spread >= spreads[i+1].spread

    @pytest.mark.asyncio
    async def test_get_htx_instruments_mock(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """_get_htx_instruments парсит Mock ответ"""
        from arbitrage.core.multi_pair_arbitrage import MultiPairArbitrageEngine
        from arbitrage.core.risk import RiskManager
        from arbitrage.core.execution import ExecutionManager

        risk = RiskManager(mock_config, bot_state)
        execution = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        engine = MultiPairArbitrageEngine(
            mock_config, bot_state, risk, execution,
            mock_okx_client, mock_htx_client
        )

        instruments = await engine._get_htx_instruments()
        assert len(instruments) > 0
        assert "BTCUSDT" in instruments

    @pytest.mark.asyncio
    async def test_filter_pairs_mock_mode(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """В mock режиме возвращаются популярные пары"""
        from arbitrage.core.multi_pair_arbitrage import MultiPairArbitrageEngine
        from arbitrage.core.risk import RiskManager
        from arbitrage.core.execution import ExecutionManager

        mock_config.mock_mode = True

        risk = RiskManager(mock_config, bot_state)
        execution = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        engine = MultiPairArbitrageEngine(
            mock_config, bot_state, risk, execution,
            mock_okx_client, mock_htx_client
        )

        all_pairs = {"BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "RANDOMUSDT"}
        filtered = await engine._filter_pairs(all_pairs)
        assert "BTCUSDT" in filtered
        assert "RANDOMUSDT" not in filtered


# ══════════════════════════════════════════════════════════
# 8. Тесты RiskManager
# ══════════════════════════════════════════════════════════

class TestRiskManager:
    def test_import(self):
        from arbitrage.core.risk import RiskManager
        assert RiskManager is not None

    def test_validate_spread_entry(self, mock_config, bot_state):
        """validate_spread проверяет порог входа"""
        from arbitrage.core.risk import RiskManager
        risk = RiskManager(mock_config, bot_state)
        # Выше порога
        assert risk.validate_spread(mock_config.entry_threshold + 0.1, is_entry=True)
        # Ниже порога
        assert not risk.validate_spread(mock_config.entry_threshold - 0.1, is_entry=True)

    def test_validate_spread_exit(self, mock_config, bot_state):
        """validate_spread проверяет порог выхода"""
        from arbitrage.core.risk import RiskManager
        risk = RiskManager(mock_config, bot_state)
        # Ниже или равно порогу выхода
        assert risk.validate_spread(mock_config.exit_threshold - 0.01, is_entry=False)
        # Выше порога выхода
        assert not risk.validate_spread(mock_config.exit_threshold + 0.1, is_entry=False)


# ══════════════════════════════════════════════════════════
# 9. Тесты конфигурации
# ══════════════════════════════════════════════════════════

class TestConfig:
    def test_get_htx_config(self, mock_config):
        """get_htx_config возвращает ExchangeConfig"""
        cfg = mock_config.get_htx_config()
        assert cfg is not None
        assert hasattr(cfg, 'api_key')
        assert hasattr(cfg, 'api_secret')

    def test_no_bybit_references(self, mock_config):
        """Конфиг не содержит bybit атрибутов"""
        assert not hasattr(mock_config, 'bybit_api_key')
        assert not hasattr(mock_config, 'bybit_api_secret')
        # HTX атрибуты должны быть
        assert hasattr(mock_config, 'htx_api_key')

    def test_htx_testnet_default(self, mock_config):
        """HTX testnet флаг присутствует"""
        assert hasattr(mock_config, 'htx_testnet')


# ══════════════════════════════════════════════════════════
# 10. Интеграционные тесты
# ══════════════════════════════════════════════════════════

class TestIntegration:
    @pytest.mark.asyncio
    async def test_full_mock_cycle(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """Полный цикл в mock режиме: stateconnect -> spread -> order -> close"""
        from arbitrage.core.execution import ExecutionManager

        mock_config.dry_run_mode = True
        em = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)

        # Входим в позицию
        success, msg = await em.execute_arbitrage_entry(
            long_exchange="okx", short_exchange="htx",
            long_price=50000.0, short_price=50200.0, size=0.01
        )
        assert success, f"Entry failed: {msg}"

        # Проверяем, что позиции записаны
        assert bot_state.is_in_position

        # Устанавливаем цены для расчёта PnL
        await bot_state.update_orderbook({
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50100.0, 1.0]], "asks": [[50101.0, 1.0]],
            "timestamp": 1000
        })
        await bot_state.update_orderbook({
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[50000.0, 1.0]], "asks": [[50001.0, 1.0]],
            "timestamp": 1000
        })

        # Выходим из позиции
        success, msg = await em.execute_arbitrage_exit()
        assert success, f"Exit failed: {msg}"

        # Позиции должны быть закрыты
        assert not bot_state.is_in_position

    @pytest.mark.asyncio
    async def test_multi_pair_full_scan(self, mock_config, mock_okx_client, mock_htx_client, bot_state):
        """MultiPairEngine: полный цикл сканирования с mock данными"""
        from arbitrage.core.multi_pair_arbitrage import MultiPairArbitrageEngine
        from arbitrage.core.risk import RiskManager
        from arbitrage.core.execution import ExecutionManager

        mock_config.mock_mode = True
        mock_config.min_spread = 0.01

        risk = RiskManager(mock_config, bot_state)
        execution = ExecutionManager(mock_config, bot_state, mock_okx_client, mock_htx_client)
        engine = MultiPairArbitrageEngine(
            mock_config, bot_state, risk, execution,
            mock_okx_client, mock_htx_client
        )

        # Инициализация
        await engine.initialize()
        assert len(engine.monitored_pairs) > 0

        # Обновление цен (mock)
        await engine.update_prices()
        assert len(engine.okx_prices) > 0 or len(engine.htx_prices) > 0

        # Расчёт спредов
        spreads = await engine.calculate_spreads()
        # Это mock данные — спреды могут быть очень большими
        for s in spreads:
            assert s.spread > 0
            assert s.symbol in engine.monitored_pairs

    @pytest.mark.asyncio
    async def test_bot_state_orderbook_flow(self, bot_state):
        """Полный поток данных: ws -> state -> spread calc"""
        from arbitrage.utils import calculate_spread

        # Симулируем обновления с WebSocket
        okx_data = {
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50000.0, 1.0], [49999.0, 0.5]],
            "asks": [[50001.0, 0.8], [50002.0, 0.3]],
            "timestamp": 100000
        }
        htx_data = {
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[50150.0, 1.2], [50149.0, 0.6]],
            "asks": [[50151.0, 1.0], [50152.0, 0.4]],
            "timestamp": 100000
        }

        await bot_state.update_orderbook(okx_data)
        await bot_state.update_orderbook(htx_data)

        assert bot_state.is_both_connected()

        okx_ob, htx_ob = bot_state.get_orderbooks()
        assert okx_ob.best_bid == 50000.0
        assert htx_ob.best_bid == 50150.0

        # Spread 1: LONG OKX, SHORT HTX
        spread1 = calculate_spread(htx_ob.best_bid, okx_ob.best_ask)
        # (50150 - 50001) / 50001 * 100 ≈ 0.298%
        assert spread1 > 0.25  # Выше порога входа

        # Spread 2: LONG HTX, SHORT OKX
        spread2 = calculate_spread(okx_ob.best_bid, htx_ob.best_ask)
        # (50000 - 50151) / 50151 * 100 ≈ -0.301%
        assert spread2 < 0  # Нет возможности в обратную сторону
