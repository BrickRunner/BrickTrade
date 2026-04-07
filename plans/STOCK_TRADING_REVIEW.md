# Режим торговли акциями — Обзор проблем и план развития

**Дата:** 2026-03-30  
**Статус:** После первого раунда исправлений (8 из 16 проблем)

---

## 1. Архитектура модуля

### Структура
```
stocks/
├── exchange/          # BCS broker integration
│   ├── bcs_auth.py    # OAuth2 token management  
│   ├── bcs_rest.py    # REST client (orders, portfolio, candles)
│   └── bcs_ws.py      # WebSocket (quotes, orderbook, trades, candles)
├── strategies/        # 6 trading strategies
│   ├── base.py        # StockBaseStrategy ABC
│   ├── mean_reversion.py
│   ├── trend_following.py
│   ├── breakout.py
│   ├── volume_spike.py
│   ├── divergence.py
│   └── rsi_reversal.py
├── system/            # Core engine
│   ├── engine.py      # Main cycle loop (5s interval)
│   ├── execution.py   # Order placement + fill tracking
│   ├── state.py       # Positions, equity, kill-switch
│   ├── risk.py        # Multi-layer risk validation
│   ├── config.py      # All configuration (env-driven)
│   ├── confirmation.py # Semi-auto Telegram confirmation flow
│   ├── schedule.py    # MOEX trading hours + holidays
│   ├── factory.py     # Build everything from config
│   ├── price_buffer.py # Candle ring buffers + indicator computation
│   ├── models.py      # Dataclasses (StockPosition, StockTradeIntent, etc.)
│   ├── interfaces.py  # Protocol definitions
│   └── strategy_runner.py # Parallel strategy execution
handlers/
└── stock_handlers.py  # Telegram bot commands (start/stop/settings/stats)
```

### Поток данных
```
BCS WS → quotes/candles → CandleBufferManager → indicators
                                                      ↓
                              StockTradingEngine.run_cycle()
                                                      ↓
                              For each ticker: get_snapshot()
                                                      ↓
                              StrategyRunner.generate_intents()
                                                      ↓
                              Quality filter (confidence + edge)
                                                      ↓
                              RiskEngine.validate_intent()
                                                      ↓
                              Route: monitoring | semi_auto | auto
                                                      ↓
                              SingleLegExecutionEngine
                                                      ↓
                              BCS REST → place_order → wait_fill
```

### 3 режима работы
| Режим | Описание |
|-------|----------|
| `monitoring` | Только логирование сигналов, без ордеров |
| `semi_auto` | Telegram подтверждение перед исполнением |
| `auto` | Автоматическое исполнение без подтверждения |

---

## 2. Уже исправленные проблемы

| # | Проблема | Файл | Статус |
|---|---------|------|--------|
| 1 | DivergenceStrategy использовала current_rsi вместо RSI в точках свингов | `stocks/strategies/divergence.py` | ✅ Исправлено |
| 4 | MeanReversion генерировала одну ногу вместо пары | `stocks/strategies/mean_reversion.py` | ✅ Исправлено |
| 6 | `_wait_fill` не обрабатывала partial fills, не отменяла по таймауту | `stocks/system/execution.py` | ✅ Исправлено |
| 7 | Trailing stop обновления `peak_price`/`stop_loss_price` не сохранялись | `stocks/system/engine.py` | ✅ Исправлено |
| 8 | Нет лимита одновременных confirmation запросов | `stocks/system/confirmation.py` | ✅ Исправлено |
| 10 | `run_forever` делал busy-loop при закрытом рынке | `stocks/system/engine.py` | ✅ Исправлено |
| 14 | `on_modify` пересоздавала intent вручную вместо `replace()` | `stocks/system/confirmation.py` | ✅ Исправлено |
| 16 | `max_total_exposure_pct=1.0` позволяла 100% equity в позициях | `stocks/system/config.py` | ✅ Исправлено |

---

## 3. Оставшиеся проблемы

### 🔴 Критические

#### P1. Нет ATR-based позиционного сайзинга
**Файлы:** все стратегии, `stocks/system/engine.py`  
**Проблема:** Все стратегии возвращают `quantity_lots=1` жёстко. Нет адаптивного расчёта размера позиции на основе ATR (волатильности) и риск-бюджета. На дорогих бумагах (LKOH ~7500₽) 1 лот = 7500₽. На дешёвых (VTBR ~0.02₽, lot_size=10000) 1 лот = 200₽. Позиции сильно разбалансированы.

**Решение:** Добавить `PositionSizer` класс:
```python
class StockPositionSizer:
    def calculate_lots(self, price, lot_size, atr, equity, risk_per_trade_pct) -> int:
        risk_budget = equity * risk_per_trade_pct  # e.g. 1% of 100000 = 1000₽
        risk_per_share = atr * sl_multiplier        # e.g. 50₽ per share
        shares = risk_budget / risk_per_share        # 1000/50 = 20 shares  
        lots = int(shares / lot_size)                # 20/10 = 2 lots
        return max(1, lots)
```

#### P2. Нет бэктестинга стратегий
**Проблема:** Невозможно оценить историческую прибыльность стратегий. Нет данных для принятия решений о параметрах. Все настройки подобраны вслепую.

**Решение:** Создать `stocks/system/backtest/engine.py`:
- Загрузка исторических свечей из BCS REST (до 3 месяцев)
- Прогон каждой стратегии через исторические данные
- Вычисление метрик: Sharpe, max drawdown, win rate, profit factor
- Telegram команда `/stock_backtest` с результатами

#### P3. MeanReversion: engine дедупликация блокирует вторую ногу пары
**Файл:** `stocks/system/engine.py:182-189`  
**Проблема:** Engine проверяет `positions_for_ticker()` и пропускает intent если позиция уже есть. Для pair trading MeanReversion генерирует 2 intenta (sell SBER + buy SBERP). Если SBER исполнился первым, позиция на SBER уже существует, и повторный intent будет заблокирован. Но более важно: второй ticker (SBERP) может быть также заблокирован если у нас уже есть позиция на SBERP от другого сигнала.

**Решение:** 
- Проверять `pair_id` в metadata — если обе ноги одной пары, пропускать дедупликацию
- Или: генерировать оба intenta как единую транзакцию (`CompositeIntent`)

### 🟡 Серьёзные

#### P4. Нет синхронизации внутренних позиций с BCS
**Файл:** `stocks/system/state.py`, `stocks/system/factory.py`  
**Проблема:** State хранит позиции в `data/stock_positions.json`. Если бот перезапускается или ордер исполняется вне бота, внутреннее состояние расходится с реальными позициями на BCS. Бот может открыть дублирующую позицию или не закрыть существующую.

**Решение:** Периодическая сверка:
```python
async def reconcile_positions(self):
    bcs_positions = await self.venue.get_positions()  # {ticker: qty}
    internal = await self.state.list_positions()
    # Warn about orphans (BCS has it, we don't track it)
    # Warn about ghosts (we track it, BCS doesn't have it)
```

#### P5. Стратегии не знают о текущей сессии (morning/main/evening)
**Файл:** `stocks/strategies/`, `stocks/system/engine.py`  
**Проблема:** Утренняя сессия (06:50-09:50 MSK) имеет пониженную ликвидность — только blue chips. Вечерняя сессия (19:05-23:50) ещё менее ликвидна. Стратегии не адаптируют параметры к типу сессии.

**Решение:**  
- Добавить `session_type: str` в `StockSnapshot`
- Стратегии могут менять thresholds по сессии (шире SL утром, отключать скальпинг вечером)
- VolumeSpike и Breakout лучше работают в main session с полной ликвидностью

#### P6. Нет отслеживания реального P&L vs ожидаемого
**Проблема:** Бот логирует сигналы и исполнения, но нет агрегированной статистики:
- Win rate по стратегии
- Средний P&L по стратегии  
- Sharpe ratio за период
- Сравнение ожидаемого edge с реальным

**Решение:** Добавить `StockPerformanceTracker`:
```python
class StockPerformanceTracker:
    async def on_trade_closed(self, strategy_id, pnl, expected_edge, holding_time):
        # Accumulate per-strategy statistics
    async def report(self) -> Dict[str, PerformanceMetrics]:
        # Compute win_rate, avg_pnl, sharpe, profit_factor per strategy
```

#### P7. Нет graceful shutdown позиций
**Проблема:** Когда пользователь нажимает "Остановить" (`stock_stop`), бот отменяет task, но открытые позиции остаются без присмотра. Trailing stop/SL/TP больше не мониторятся.

**Решение:**  
- При остановке: предложить "Закрыть все позиции?" / "Оставить открытыми"
- Если оставляет открытыми: запустить minimal watchdog (только SL/TP мониторинг)
- Предупреждение в Telegram если есть незакрытые позиции при остановке

#### P8. `_persist_positions()` — synchronous I/O в async контексте
**Файл:** `stocks/system/state.py:106`  
**Проблема:** `_persist_positions()` вызывает `json.dump()` синхронно, блокируя event loop. При большом количестве позиций и частых trailing stop обновлениях это может вызвать задержки.

**Решение:**
```python
async def _persist_positions_async(self):
    data = json.dumps(records, indent=2)
    await asyncio.to_thread(self._write_file, data)
```

### 🟠 Средние

#### P9. Нет лимитных ордеров с проскальзыванием
**Файл:** `stocks/system/execution.py`  
**Проблема:** Все стратегии используют `order_type="market"`. Рыночные ордера на MOEX подвержены проскальзыванию, особенно на менее ликвидных бумагах (RUAL, POLY, AFLT).

**Решение:**
- Для ликвидных бумаг (SBER, GAZP, LKOH): market ордера OK
- Для менее ликвидных: limit ордера с offset от mid price (bid + spread * 0.3)
- Добавить `LiquidityClassifier` на основе среднего дневного объёма

#### P10. Нет multi-timeframe анализа
**Проблема:** Все стратегии работают на одном таймфрейме (M5). Нет подтверждения от старших таймфреймов. Например, TrendFollowing может показать buy на M5, но H1 показывает нисходящий тренд.

**Решение:**
- Добавить второй CandleBuffer для H1
- Стратегии получают `indicators_h1` в snapshot
- Фильтр: торговать только в направлении тренда старшего ТФ

#### P11. WebSocket reconnection не восстанавливает подписки корректно
**Файл:** `stocks/exchange/bcs_ws.py:100-133`  
**Проблема:** При reconnect `_connect_and_stream` заново отправляет все подписки из `self._subscriptions`. Но если подписки добавлялись динамически (что не реализовано сейчас), они будут потеряны. Также нет heartbeat/health-check для определения зависших соединений.

**Решение:**
- Добавить `last_message_ts` и watchdog task, который переподключается если нет данных > 60 секунд
- Log reconnection events в Telegram notification

#### P12. Нет поддержки MOEX Derivatives Market (FORTS)
**Проблема:** Модуль работает только с акциями TQBR. Нет поддержки фьючерсов (SPBFUT) и опционов. Фьючерсы на индексы (RTS, MOEX) и на акции дают возможность хеджирования и более капиталоэффективных стратегий.

#### P13. Config validation неполная
**Файл:** `stocks/system/config.py:196-204`  
**Проблема:** `validate()` проверяет только базовые вещи. Не проверяет:
- `commission_pct` > 0
- `trailing_stop_pct` в разумных пределах (0-10%)
- `time_stop_hours` > 0
- Тикеры из конфига реально существуют на MOEX
- `mr_pairs` содержат валидные пары

#### P14. Индикаторы пересчитываются на каждый snapshot
**Файл:** `stocks/system/price_buffer.py:105-178`  
**Проблема:** `compute_indicators()` вычисляет ~15 индикаторов при каждом вызове. Кэш сбрасывается при каждом новом candle (`on_candle_update`). Для 25 тикеров × 5 секунд цикл = 300 пересчётов/минуту. При текущих объёмах это не проблема, но при масштабировании будет.

---

## 4. План развития

### Приоритет 1 — Необходимо для продакшна (1-2 недели)

| Задача | Описание | Сложность |
|--------|----------|-----------|
| ATR-based сайзинг (P1) | `StockPositionSizer` с risk-per-trade бюджетом | Средняя |
| Pair dedup fix (P3) | Пропускать дедупликацию для linked pair legs | Лёгкая |
| BCS position reconciliation (P4) | Периодическая сверка state ↔ BCS каждые 5 минут | Средняя |
| Graceful shutdown (P7) | Предложить закрыть позиции при остановке | Лёгкая |
| Session awareness (P5) | Передавать session_type стратегиям | Лёгкая |
| Async persist (P8) | `asyncio.to_thread` для JSON write | Лёгкая |

### Приоритет 2 — Повышение прибыльности (2-4 недели)

| Задача | Описание | Сложность |
|--------|----------|-----------|
| Performance tracker (P6) | Win rate, avg PnL, Sharpe per strategy | Средняя |
| Backtest engine (P2) | Прогон стратегий на исторических данных | Высокая |
| Limit orders (P9) | Лимитные ордера для illiquid тикеров | Средняя |
| Multi-timeframe (P10) | H1 confirmation для M5 сигналов | Средняя |
| WS health watchdog (P11) | Reconnect при зависших соединениях | Лёгкая |
| Config validation (P13) | Полная проверка всех параметров | Лёгкая |

### Приоритет 3 — Масштабирование (1-2 месяца)

| Задача | Описание | Сложность |
|--------|----------|-----------|
| FORTS support (P12) | Фьючерсы RTS/MOEX для хеджирования | Высокая |
| Indicator caching (P14) | Инкрементальное обновление индикаторов | Средняя |
| ML signal filter | ML-модель для фильтрации ложных сигналов на основе собранной статистики | Высокая |
| Portfolio optimization | Ребалансировка между стратегиями на основе Sharpe | Высокая |

### Приоритет 4 — Новые возможности

| Задача | Описание |
|--------|----------|
| Корзинная торговля | Long-short basket из топ/bottom N акций по momentum |
| Event-driven стратегия | Торговля на дивидендных отсечках и квартальных отчётах |
| Cross-market арбитраж | MOEX spot vs FORTS futures (cash-and-carry) |
| Pre-market scanning | Анализ gap-ов до открытия основной сессии |
| Risk parity | Распределение капитала пропорционально inverse volatility |

---

## 5. Оценка качества кода

### Что хорошо ✅

1. **Чёткое разделение ответственности** — engine/execution/risk/state/strategies разнесены по отдельным модулям
2. **Frozen dataclasses** для immutable моделей (StockTradeIntent, StockSnapshot) — предотвращает случайные мутации
3. **3 режима работы** (monitoring → semi_auto → auto) — правильный путь от тестирования к продакшну
4. **Kill-switch** с daily и portfolio drawdown thresholds
5. **Trailing stop** с persistence обновлений на диск
6. **Env-driven config** — все параметры настраиваются через переменные окружения
7. **Position persistence** — позиции выживают перезапуск бота
8. **Telegram UI** для управления настройками в реальном времени
9. **Indicator pipeline** использует `market_intelligence.indicators` — переиспользование кода между модулями

### Что требует улучшения ⚠️

1. **Тестовое покрытие** — unit-тесты для stock модуля минимальны (только models + state)
2. **Нет интеграционных тестов** с mock BCS API
3. **Logging** — много INFO-уровня, мало structured logging для парсинга
4. **Нет метрик** (Prometheus/StatsD) для мониторинга в реальном времени
5. **Type annotations** — `provider: object` в engine вместо `StockMarketDataProvider`
6. **Error recovery** — один сбой в strategy_runner.generate_intents не должен влиять на остальные (сейчас это обрабатывается через gather+return_exceptions, но ошибка теряется)

---

## 6. Конкретные рекомендации по коду

### 6.1 engine.py — type hint для provider
```python
# Текущее:
provider: object  # StockMarketDataProvider (duck-typed)
# Рекомендация:
provider: StockMarketDataProvider  # из stocks.system.interfaces
```

### 6.2 engine.py — dedup должна пропускать pair legs
```python
# Текущее (строки 182-189):
existing = await self.state.positions_for_ticker(intent.ticker)
if existing:
    continue  # ← блокирует вторую ногу pair trade

# Рекомендация:
pair_id = intent.metadata.get("pair_id")
if existing and not pair_id:
    continue
```

### 6.3 execution.py — _calc_pnl возвращает 0 если lot_size отсутствует
```python
# Текущее:
lot_size = getattr(position, "lot_size", 1) or 1  # or 1 защита от None/0
# OK, но лучше:
lot_size = position.lot_size if position.lot_size > 0 else 1
```

### 6.4 state.py — _persist_positions блокирует event loop
```python
# Текущее:
def _persist_positions(self) -> None:
    with open(_POSITIONS_FILE, "w") as f:
        json.dump(records, f, indent=2)
# Рекомендация:
async def _persist_positions(self) -> None:
    data = json.dumps(records, indent=2)
    await asyncio.to_thread(lambda: open(_POSITIONS_FILE, "w").write(data))
```

### 6.5 factory.py — load_lot_sizes не вызывается автоматически
```python
# В build_stock_engine() нет вызова provider.load_lot_sizes()
# Рекомендация: добавить
await provider.load_lot_sizes(config.tickers)
# Но build_stock_engine синхронная функция — нужен async wrapper
```

---

## 7. Сводка по приоритетности

```
КРИТИЧНО (блокирует profit):
  P1 — ATR сайзинг (без него позиции разбалансированы)
  P3 — Pair dedup (MeanReversion работает неправильно)

ВАЖНО (production readiness):
  P4 — BCS reconciliation
  P5 — Session awareness  
  P7 — Graceful shutdown
  P8 — Async persist

ЖЕЛАТЕЛЬНО (profit improvement):
  P2 — Backtest engine
  P6 — Performance tracking
  P9 — Limit orders
  P10 — Multi-timeframe

БУДУЩЕЕ (scaling):
  P12 — FORTS
  ML signal filter
  Portfolio optimization
```
