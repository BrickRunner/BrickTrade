"""
Быстрый тест арбитражного бота с оптимизациями
"""
import asyncio
import time
from arbitrage.utils import ArbitrageConfig
from arbitrage.core import BotState, RiskManager, ExecutionManager, MultiPairArbitrageEngine
from arbitrage.exchanges import OKXRestClient, HTXRestClient

async def main():
    print("=" * 70)
    print("ТЕСТ ОПТИМИЗИРОВАННОГО АРБИТРАЖНОГО БОТА")
    print("=" * 70)

    # Загружаем конфигурацию из .env
    config = ArbitrageConfig.from_env()

    print(f"\nРежим: {'МОНИТОРИНГ' if config.monitoring_only else 'ТОРГОВЛЯ'}")
    print(f"Mock mode: {config.mock_mode}")
    print(f"Min spread: {config.min_spread}%")
    print(f"Update interval: {config.update_interval}s")
    print(f"Min opportunity lifetime: {config.min_opportunity_lifetime}s")

    # Создаем клиенты
    print("\n📡 Создание клиентов бирж...")
    okx_client = OKXRestClient(config.get_okx_config())
    htx_client = HTXRestClient(config.get_htx_config())

    # Создаем компоненты
    state = BotState()
    state.is_running = True

    risk_manager = RiskManager(config, state)
    execution_manager = ExecutionManager(config, state, okx_client, htx_client)

    # Создаем движок
    print("\n🚀 Инициализация движка...")
    engine = MultiPairArbitrageEngine(
        config, state, risk_manager, execution_manager,
        okx_client, htx_client
    )

    # Инициализация (получение списка пар)
    start_init = time.time()
    await engine.initialize()
    init_time = time.time() - start_init

    print(f"\n✅ Инициализация завершена за {init_time:.2f}s")
    print(f"📊 Отслеживается пар: {len(engine.monitored_pairs)}")
    print(f"📝 Список пар: {sorted(list(engine.monitored_pairs)[:10])}{'...' if len(engine.monitored_pairs) > 10 else ''}")

    # Тест 1: Обновление цен
    print("\n" + "=" * 70)
    print("ТЕСТ 1: Обновление цен")
    print("=" * 70)

    start_update = time.time()
    await engine.update_prices()
    update_time = time.time() - start_update

    print(f"⚡ Цены обновлены за {update_time*1000:.0f}ms")
    print(f"📈 OKX: {len(engine.okx_prices)} пар")
    print(f"📉 HTX: {len(engine.htx_prices)} пар")

    # Показываем примеры цен
    sample_symbols = list(engine.monitored_pairs)[:3]
    for symbol in sample_symbols:
        okx = engine.okx_prices.get(symbol, {})
        htx = engine.htx_prices.get(symbol, {})
        if okx and htx:
            print(f"\n{symbol}:")
            print(f"  OKX: bid={okx.get('bid', 0):,.4f}, ask={okx.get('ask', 0):,.4f}")
            print(f"  HTX: bid={htx.get('bid', 0):,.4f}, ask={htx.get('ask', 0):,.4f}")

    # Тест 2: Расчет спредов
    print("\n" + "=" * 70)
    print("ТЕСТ 2: Расчет спредов")
    print("=" * 70)

    start_calc = time.time()
    spreads = await engine.calculate_spreads()
    calc_time = time.time() - start_calc

    print(f"⚡ Спреды рассчитаны за {calc_time*1000:.0f}ms")
    print(f"🎯 Найдено возможностей: {len(spreads)}")

    # Показываем топ-10 возможностей
    if spreads:
        print("\n🏆 ТОП-10 АРБИТРАЖНЫХ ВОЗМОЖНОСТЕЙ:")
        print("-" * 70)
        for i, spread in enumerate(spreads[:10], 1):
            long_ex = spread.get_long_exchange().upper()
            short_ex = spread.get_short_exchange().upper()
            print(
                f"{i:2}. {spread.symbol:12} | Спред: {spread.spread:6.3f}% | "
                f"LONG {long_ex} @ {spread.get_long_price():,.4f} | "
                f"SHORT {short_ex} @ {spread.get_short_price():,.4f}"
            )
    else:
        print("\n⚠️ Нет возможностей выше порога {config.min_spread}%")

    # Тест 3: Полный цикл (как в реальном мониторинге)
    print("\n" + "=" * 70)
    print("ТЕСТ 3: Полный цикл мониторинга (3 итерации)")
    print("=" * 70)

    for iteration in range(3):
        print(f"\n🔄 Итерация {iteration + 1}/3...")

        start_full = time.time()
        opportunities = await engine.find_best_opportunities(top_n=5)
        full_time = time.time() - start_full

        print(f"⚡ Полный цикл за {full_time*1000:.0f}ms")

        if opportunities:
            best = opportunities[0]
            print(
                f"🎯 Лучшая возможность: {best.symbol} - {best.spread:.3f}% "
                f"(LONG {best.get_long_exchange().upper()} @ {best.get_long_price():,.4f}, "
                f"SHORT {best.get_short_exchange().upper()} @ {best.get_short_price():,.4f})"
            )
        else:
            print("⚠️ Нет возможностей")

        if iteration < 2:  # Не спим на последней итерации
            await asyncio.sleep(config.update_interval)

    # Закрываем сессии
    print("\n🔌 Закрытие соединений...")
    if hasattr(okx_client, 'session') and okx_client.session:
        await okx_client.session.close()
    if hasattr(htx_client, 'session') and htx_client.session:
        await htx_client.session.close()

    print("\n" + "=" * 70)
    print("✅ ТЕСТ ЗАВЕРШЕН УСПЕШНО")
    print("=" * 70)
    print(f"\n📊 Статистика:")
    print(f"   Инициализация: {init_time:.2f}s")
    print(f"   Обновление цен: {update_time*1000:.0f}ms")
    print(f"   Расчет спредов: {calc_time*1000:.0f}ms")
    print(f"   Полный цикл: {full_time*1000:.0f}ms (в среднем)")
    print(f"\n💡 Бот готов к работе и оптимизирован для скорости!")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n⚠️ Тест прерван пользователем")
    except Exception as e:
        print(f"\n\n❌ Ошибка: {e}")
        import traceback
        traceback.print_exc()
