"""
Тесты производительности арбитражного бота
Измеряем скорость критичных операций для арбитража
"""
import asyncio
import time
import sys
import os
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from arbitrage.utils import ArbitrageConfig, ExchangeConfig
from arbitrage.test.mock_exchanges import MockOKXRestClient, MockHTXRestClient, MockOKXWebSocket, MockHTXWebSocket
from arbitrage.core.state import BotState
from arbitrage.core.arbitrage import calculate_spread
from arbitrage.core.execution import ExecutionManager
from arbitrage.core.risk import RiskManager


class PerformanceBenchmark:
    """Бенчмарк производительности бота"""

    def __init__(self):
        self.config = self._create_config()
        self.state = BotState()
        self.okx_client = MockOKXRestClient(ExchangeConfig("k", "s", False))
        self.htx_client = MockHTXRestClient(ExchangeConfig("k", "s", False))
        self.execution = ExecutionManager(
            self.config, self.state, self.okx_client, self.htx_client
        )
        self.risk = RiskManager(self.config, self.state)

    def _create_config(self):
        config = ArbitrageConfig()
        config.symbol = "BTCUSDT"
        config.position_size = 0.01
        config.leverage = 3
        config.entry_threshold = 0.25
        config.exit_threshold = 0.05
        config.mock_mode = True
        config.dry_run_mode = False  # Реальный режим для точных измерений
        config.monitoring_only = False
        config.order_timeout_ms = 200
        return config

    async def test_orderbook_update_speed(self, iterations: int = 1000) -> dict:
        """Скорость обновления ордербука"""
        orderbook = {
            "exchange": "okx",
            "symbol": "BTCUSDT",
            "bids": [[50000.0, 1.0], [49999.0, 0.5]],
            "asks": [[50001.0, 1.0], [50002.0, 0.5]],
            "timestamp": int(time.time() * 1000)
        }

        start = time.perf_counter()
        for _ in range(iterations):
            await self.state.update_orderbook(orderbook)
        end = time.perf_counter()

        total_ms = (end - start) * 1000
        avg_us = (total_ms / iterations) * 1000  # микросекунды

        return {
            "operation": "Orderbook Update",
            "iterations": iterations,
            "total_ms": round(total_ms, 2),
            "avg_us": round(avg_us, 2),
            "ops_per_sec": round(iterations / (end - start), 0)
        }

    async def test_spread_calculation_speed(self, iterations: int = 100000) -> dict:
        """Скорость расчёта спреда"""
        start = time.perf_counter()
        for _ in range(iterations):
            spread = calculate_spread(50001.0, 50000.0)
        end = time.perf_counter()

        total_ms = (end - start) * 1000
        avg_ns = (total_ms / iterations) * 1000000  # наносекунды

        return {
            "operation": "Spread Calculation",
            "iterations": iterations,
            "total_ms": round(total_ms, 2),
            "avg_ns": round(avg_ns, 2),
            "ops_per_sec": round(iterations / (end - start), 0)
        }

    async def test_risk_validation_speed(self, iterations: int = 10000) -> dict:
        """Скорость проверки рисков"""
        self.state.update_balance("okx", 10000.0)
        self.state.update_balance("htx", 10000.0)

        okx_ob = {
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50000.0, 1.0]], "asks": [[50001.0, 1.0]],
            "timestamp": int(time.time() * 1000)
        }
        htx_ob = {
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[50100.0, 1.0]], "asks": [[50101.0, 1.0]],
            "timestamp": int(time.time() * 1000)
        }

        await self.state.update_orderbook(okx_ob)
        await self.state.update_orderbook(htx_ob)

        start = time.perf_counter()
        for _ in range(iterations):
            spread = 0.20  # 0.20% spread
            self.risk.validate_spread(spread, "entry")
        end = time.perf_counter()

        total_ms = (end - start) * 1000
        avg_us = (total_ms / iterations) * 1000

        return {
            "operation": "Risk Validation",
            "iterations": iterations,
            "total_ms": round(total_ms, 2),
            "avg_us": round(avg_us, 2),
            "ops_per_sec": round(iterations / (end - start), 0)
        }

    async def test_single_order_placement_speed(self) -> dict:
        """Скорость размещения одного ордера"""
        timings = []

        for _ in range(100):
            start = time.perf_counter()
            await self.execution._place_order("okx", "buy", 50000.0, 0.01)
            end = time.perf_counter()
            timings.append((end - start) * 1000)

        avg_ms = sum(timings) / len(timings)
        min_ms = min(timings)
        max_ms = max(timings)
        p95_ms = sorted(timings)[int(len(timings) * 0.95)]

        return {
            "operation": "Single Order Placement",
            "iterations": 100,
            "avg_ms": round(avg_ms, 2),
            "min_ms": round(min_ms, 2),
            "max_ms": round(max_ms, 2),
            "p95_ms": round(p95_ms, 2)
        }

    async def test_simultaneous_order_placement_speed(self) -> dict:
        """Скорость одновременного размещения двух ордеров (OKX + HTX)"""
        timings = []

        for _ in range(100):
            start = time.perf_counter()
            # Одновременное размещение на обеих биржах
            results = await asyncio.gather(
                self.execution._place_order("okx", "buy", 50000.0, 0.01),
                self.execution._place_order("htx", "sell", 50100.0, 0.01)
            )
            end = time.perf_counter()
            timings.append((end - start) * 1000)

        avg_ms = sum(timings) / len(timings)
        min_ms = min(timings)
        max_ms = max(timings)
        p95_ms = sorted(timings)[int(len(timings) * 0.95)]

        return {
            "operation": "Simultaneous Order Placement (2 exchanges)",
            "iterations": 100,
            "avg_ms": round(avg_ms, 2),
            "min_ms": round(min_ms, 2),
            "max_ms": round(max_ms, 2),
            "p95_ms": round(p95_ms, 2),
            "note": "Parallel execution (asyncio.gather)"
        }

    async def test_full_arbitrage_cycle_speed(self) -> dict:
        """Полный цикл арбитража: обнаружение + исполнение"""
        # Подготовка состояния
        self.state.update_balance("okx", 10000.0)
        self.state.update_balance("htx", 10000.0)

        okx_ob = {
            "exchange": "okx", "symbol": "BTCUSDT",
            "bids": [[50000.0, 1.0]], "asks": [[50001.0, 1.0]],
            "timestamp": int(time.time() * 1000)
        }
        htx_ob = {
            "exchange": "htx", "symbol": "BTCUSDT",
            "bids": [[50150.0, 1.0]], "asks": [[50151.0, 1.0]],  # Большой спред для входа
            "timestamp": int(time.time() * 1000)
        }

        timings = []

        for _ in range(50):
            start = time.perf_counter()

            # 1. Обновление ордербуков
            await self.state.update_orderbook(okx_ob)
            await self.state.update_orderbook(htx_ob)

            # 2. Получение лучших цен
            okx_data, htx_data = self.state.get_orderbooks()

            # 3. Расчёт спредов
            spread1 = calculate_spread(htx_data.best_bid, okx_data.best_ask)
            spread2 = calculate_spread(okx_data.best_bid, htx_data.best_ask)

            # 4. Проверка порога входа
            if spread1 >= self.config.entry_threshold:
                # 5. Проверка рисков
                risk_ok = self.risk.validate_spread(spread1, "entry")

                if risk_ok:
                    # 6. Исполнение (одновременное размещение ордеров)
                    success = await self.execution.execute_arbitrage_entry(
                        long_exchange="okx",
                        short_exchange="htx",
                        long_price=okx_data.best_ask,
                        short_price=htx_data.best_bid,
                        size=self.config.position_size
                    )

            end = time.perf_counter()
            timings.append((end - start) * 1000)

        avg_ms = sum(timings) / len(timings)
        min_ms = min(timings)
        max_ms = max(timings)
        p95_ms = sorted(timings)[int(len(timings) * 0.95)]

        return {
            "operation": "Full Arbitrage Cycle (detection + execution)",
            "iterations": 50,
            "avg_ms": round(avg_ms, 2),
            "min_ms": round(min_ms, 2),
            "max_ms": round(max_ms, 2),
            "p95_ms": round(p95_ms, 2),
            "note": "Includes: orderbook update, spread calc, risk check, order placement"
        }

    async def test_websocket_callback_latency(self) -> dict:
        """Задержка обработки WebSocket callback"""
        latencies = []
        received = []

        async def callback(ob):
            receive_time = time.perf_counter()
            latency = (receive_time - ob["perf_start"]) * 1000
            latencies.append(latency)
            received.append(ob)
            if len(received) >= 100:
                ws.running = False

        ws = MockOKXWebSocket("BTCUSDT")

        # Патчим для добавления timestamp
        original_connect = ws.connect
        async def patched_connect(cb):
            async def wrapped_callback(ob):
                ob["perf_start"] = time.perf_counter()
                await cb(ob)
            return await original_connect(wrapped_callback)
        ws.connect = patched_connect

        task = asyncio.create_task(ws.connect(callback))
        try:
            await asyncio.wait_for(task, timeout=60)
        except asyncio.TimeoutError:
            pass
        finally:
            await ws.disconnect()
            task.cancel()
            try:
                await task
            except:
                pass

        if latencies:
            avg_ms = sum(latencies) / len(latencies)
            min_ms = min(latencies)
            max_ms = max(latencies)
            p95_ms = sorted(latencies)[int(len(latencies) * 0.95)]

            return {
                "operation": "WebSocket Callback Latency",
                "iterations": len(latencies),
                "avg_ms": round(avg_ms, 2),
                "min_ms": round(min_ms, 2),
                "max_ms": round(max_ms, 2),
                "p95_ms": round(p95_ms, 2),
                "note": "Time from orderbook generation to callback execution"
            }
        else:
            return {"operation": "WebSocket Callback Latency", "error": "No data collected"}


async def main():
    """Запуск всех бенчмарков"""
    print("=" * 80)
    print("ARBITRAGE BOT PERFORMANCE BENCHMARK")
    print("=" * 80)
    print()

    bench = PerformanceBenchmark()

    tests = [
        bench.test_orderbook_update_speed,
        bench.test_spread_calculation_speed,
        bench.test_risk_validation_speed,
        bench.test_single_order_placement_speed,
        bench.test_simultaneous_order_placement_speed,
        bench.test_websocket_callback_latency,
        bench.test_full_arbitrage_cycle_speed,
    ]

    results = []
    for test in tests:
        print(f"Running: {test.__name__}...")
        result = await test()
        results.append(result)
        print(f"  [OK] Complete")

    print()
    print("=" * 80)
    print("RESULTS")
    print("=" * 80)
    print()

    for result in results:
        print(f">> {result['operation']}")
        print(f"   Iterations: {result.get('iterations', 'N/A')}")

        if 'avg_ms' in result:
            print(f"   Average:    {result['avg_ms']} ms")
            print(f"   Min:        {result['min_ms']} ms")
            print(f"   Max:        {result['max_ms']} ms")
            print(f"   P95:        {result['p95_ms']} ms")
        elif 'avg_us' in result:
            print(f"   Average:    {result['avg_us']} us")
            print(f"   Ops/sec:    {result['ops_per_sec']:,}")
        elif 'avg_ns' in result:
            print(f"   Average:    {result['avg_ns']} ns")
            print(f"   Ops/sec:    {result['ops_per_sec']:,}")

        if 'note' in result:
            print(f"   Note:       {result['note']}")

        print()

    # Вывод итоговой оценки
    print("=" * 80)
    print("SUMMARY: CRITICAL LATENCIES FOR ARBITRAGE")
    print("=" * 80)
    print()

    # Находим результат полного цикла
    full_cycle = next((r for r in results if "Full Arbitrage Cycle" in r['operation']), None)
    if full_cycle:
        avg = full_cycle['avg_ms']
        p95 = full_cycle['p95_ms']

        print(f">> AVERAGE TRADE EXECUTION TIME: {avg} ms")
        print(f">> P95 TRADE EXECUTION TIME:     {p95} ms")
        print()

        # Оценка конкурентоспособности
        if avg < 50:
            rating = "[EXCELLENT] Competitive with professional HFT systems"
        elif avg < 100:
            rating = "[VERY GOOD] Fast enough for most arbitrage opportunities"
        elif avg < 200:
            rating = "[GOOD] Adequate for medium-frequency arbitrage"
        elif avg < 500:
            rating = "[MODERATE] May miss fast-disappearing opportunities"
        else:
            rating = "[SLOW] High risk of missing arbitrage windows"

        print(f"Rating: {rating}")
        print()
        print("Context:")
        print("  - Professional HFT systems: < 1 ms")
        print("  - Retail algorithmic trading: 10-100 ms")
        print("  - Manual trading: 500-2000 ms")
        print("  - Typical arbitrage window: 100-1000 ms")

    print()
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
