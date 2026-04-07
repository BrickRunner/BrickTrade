# Полный аудит кода BrickTrade — Апрель 2026

> Аудит проведён с позиции **лучшего трейдера и программиста**. Каждый раздел оценён по трём категориям: сильные стороны, недостатки и критические ошибки.

---

## 📊 Общая оценка

| Раздел | Оценка | Вердикт |
|---|---|---|
| Core arbitrage | 7/10 | Хорошая база, но устарел |
| Exchange WS/REST | 6/10 | Рабочие, но хрупкие |
| System/Engine | 5/10 | Переусложнён, два параллельных движка |
| Strategies | 7/10 | Правильные идеи, нереалистичные пороги |
| Execution | 6/10 | V1 и V2 сосуществуют — это техдолг №1 |
| Market Intelligence | 8/10 | Лучший раздел |
| Handlers/Bot UI | 6/10 | Рабочий, но хрупкий |
| Utils/Config | 7/10 | Хорошо организованы |

---

## 1. CORE ARBITRAGE MODULES (`arbitrage/core/`)

### ✅ Сильные стороны

- **[`MarketDataEngine`](arbitrage/core/market_data.py:34)** — единый унифицированный интерфейс для всех бирж. Хорошо структурированные dataclass (`TickerData`, `FundingData`). Кэширование contract_sizes, tick_sizes, min_order_sizes — профессиональный подход.
- **[`RiskManager`](arbitrage/core/risk.py:41)** — лучшая часть кодовой базы. NaN/Inf валидация на каждом шаге, circuit breaker с автоматическим сбросом, daily drawdown tracking, delta-check с маркет-ценами (не entry), notional-based exposure (не margin-based). Это уровень production hedge fund.
- **[`BotState`](arbitrage/core/state.py:79)** — asyncio.Lock thread-safety корректно реализована. Поддержка двух типов позиций (`Position` + `ActivePosition`). Stale orderbook detection (`_MAX_ORDERBOOK_AGE = 30s`).
- **[`MetricsTracker`](arbitrage/core/metrics.py:13)** — Snapshot-based computation устраняет гонки при чтении. Sharp ratio с динамической annualization по фактическим трейд timestamp, cap на 36,500 trades/year предотвращает абсурдную экстраполяцию. Background task referencing (`_track_task`) предотвращает GC.

### ⚠️ Недостатки

- **[`MarketDataEngine`](arbitrage/core/market_data.py:119)** — REST polling для всех данных. При 4 биржах × 3 типа данных = 12 REST вызовов за цикл. Это создаёт латентность ~500-2000ms на цикл. WebSocket orderbook cache [`WsOrderbookCache`](arbitrage/system/ws_orderbooks.py) есть, но не используется для всех типов данных.
- **[`RiskManager.can_open_position()`](arbitrage/core/risk.py:69)** — Сложная логика с `_max_position_pct`, `_max_exposure_pct` перемешана. `proposed_size * 2` в строке 115 — магический множитель, нуждается в комментарии.
- **[`ActivePosition`](arbitrage/core/state.py:29)** — dataclass слишком раздут (18 полей). Нужен вынос в отдельные классы: `PositionLeg`, `PositionEntry`, `PositionMetrics`.
- **[`MetricsTracker._compute_sharpe()`](arbitrage/core/metrics.py:158)** — Использует необработанные PnL без нормализации на размер позиции. Маленький трейд $0.01 и большой $50 имеют одинаковый вес в Sharpe расчёте.

### 🔴 Критические ошибки

1. **[`RiskManager._update_daily_drawdown()`](arbitrage/core/risk.py:304)** — Reset каждые 24h от начального запуска, а не от midnight UTC. Бот запущенный в 23:50 получит reset через 10 минут. Это может обнулить drawdown tracking в самый рискованный период.
2. **[`BotState._orderbooks`](arbitrage/core/state.py:90)** — Словарь с tuple-ключами `(exchange, symbol)` без ограничения размера. При большом количестве символов это растёт бесконечно без TTL cleanup.
3. **[`MarketDataEngine.initialize()`](arbitrage/core/market_data.py:67)** — При отсутствии common_pairs бот продолжает работу. Нет fallback на manual symbol selection и нет чёткого сигнала пользователю.

---

## 2. EXCHANGE CONNECTORS (`arbitrage/exchanges/`)

### ✅ Сильные стороны

- **Единый паттерн reconnect** — Все WS-клиенты используют `while self.running:` loop с exponential backoff и jitter (Binance добавляет `random.uniform`). Это правильный подход.
- **[`is_alive()`](arbitrage/exchanges/binance_ws.py:138)** — Liveness detection через `_last_message_ts` timestamp. Detects silent death соединения.
- **[`BinanceRestClient`](arbitrage/exchanges/binance_rest.py:25)** — aiohttp session management с `TCPConnector`, rate limiting, 429 backoff. Production-quality REST клиент.
- **HTX gzip decompression** — Правильная обработка сжатых сообщений HTX.

### ⚠️ Недостатки

- **[`BinanceWebSocket`](arbitrage/exchanges/binance_ws.py:136)** — `logger.info("Disconnected...")` находится ВНУТРИ `disconnect()` после `self.ws = None`, но до `finally` блока. Это не выполняется так как ожидается — строка 136 вне метода `disconnect()`.
- **Все WS-клиенты** — Используют `websockets.connect()` внутри `while` loop с `async with`. Это значит что при reconnect весь `connect` method пересоздаётся, включая новую подписку. Нет сохранения состояния между reconnect'ами (например, pending subscriptions).
- **[`HTXWebSocket`](arbitrage/exchanges/htx_ws.py:73)** — Подписка на `depth.step6` (20 levels snapshots) — хороший выбор вместо step0 delta, но нет fallback если step6 недоступен.
- **Bybit [`is_connected()`](arbitrage/exchanges/bybit_ws.py:160)** — Использует `ws_is_open()` из `_compat`, но не проверяет `_last_message_ts` для stale detection.

### 🔴 Критические ошибки

1. **[`BinanceWebSocket.disconnect()`](arbitrage/exchanges/binance_ws.py:124-136)** — Строка 136 (`logger.info("Disconnected...")`) находится **вне метода** `disconnect()` из-за неправильного отступа. Это dead code.
2. **Нет rate limiting на REST-запросах в WS-клиентах** — Все REST clients используют глобальный rate limiter, но нет backpressure при WS reconnect flood.
3. **[`OKXWebSocket`](arbitrage/exchanges/okx_ws.py:56) ping_interval=20, ping_timeout=10** — OKX известен агрессивным закрытием соединений при высокой нагрузке. При 20s ping могут быть случаи, когда OKX шлёт pong после 10s, что приведёт к ненужному disconnect. Рекомендуется `ping_interval=15, ping_timeout=15`.

---

## 3. ENGINE & STRATEGIES (`arbitrage/system/`)

### ✅ Сильные стороны

- **[`TradingSystemEngine`](arbitrage/system/engine.py:80)** — Dependency injection через `@classmethod create()`. Чёткое разделение RiskEngine, CapitalAllocator, AtomicExecutionEngine, StrategyRunner.
- **Цикл [`run_cycle()`](arbitrage/system/engine.py:164)** — Правильный retry balance sync при старте, periodic sync каждые 5 мин. Per-symbol cooldown.
- **[`FuturesCrossExchangeStrategy`](arbitrage/system/strategies/futures_cross_exchange.py:38)** — Walk-the-book slippage model, maker-taker hybrid execution fees, reliability rank для определения maker-ноги. Реалистичный учёт комиссий.
- **[`CashAndCarryStrategy`](arbitrage/system/strategies/cash_and_carry.py:52)** — Однобиржевой cash & carry — самый безопасный вид арбитража. APR-после-комиссий расчёт корректен. Cooldown 60s предотвращает re-entry spam.

### ⚠️ Недостатки

- **`min_spread_pct = 0.08` ([config.py:115](arbitrage/system/config.py:115))** — Для cross-exchange арбитража спред 0.08% = 8bps. При комиссиях 0.08% + 0.04% × 2 (entry) + 0.08% × 2 (exit) ≈ 0.24% round trip. NET edge будет отрицательным. Порог слишком низкий.
- **[`StrategyRunner.generate_intents()`](arbitrage/system/strategy_runner.py:17)** — Все стратегии запускаются параллельно через `asyncio.gather()`. Если две стратегии генерируют intents на один символ — нужен механизм дедупликации/приоритизации на уровне engine.
- **[`TriangularArbitrageStrategy`](arbitrage/system/strategies/triangular_arbitrage.py)** — Минимальный profit 15bps — нереалистично для 3-ножного арбитража с комиссиями ~0.1% × 3 = 0.3%. Этот strategy практически никогда не сработает.
- **[`FundingHarvestingStrategy`](arbitrage/system/strategies/funding_harvesting.py)** — Дублирует функциональность CashAndCarry с более низким порогом. Конфликтующие сигналы.

### 🔴 Критические ошибки

1. **Две стратегии могут открыть позиции на одном symbol одновременно** — Engine проверяет cooldown per symbol, но не проверяет уже открытую position per strategy. Strategy A может открыть long/short, Strategy B через секунду откроет ещё одну — удваивая exposure. Нужен `is_symbol_in_use()` check перед open.
2. **[`TradingSystemEngine.run_cycle()`](arbitrage/system/engine.py:194)** — Цикл по `config.symbols` — но snapshot берётся один раз на symbol. Если стратегия Cash & Carry хочет spot OB на том же exchange — она может получить stale данные.
3. **Нет position sizing по volatility** — Все стратегии используют одинаковый notional. Высоковолатильный BTC и тихий USDC должны иметь разные размеры позиций через ATR-scaling.

---

## 4. EXECUTION (`arbitrage/system/execution.py` + `execution_v2.py`)

### ✅ Сильные стороны

- **V2 [`AtomicExecutionEngine`](arbitrage/system/execution_v2.py:70)** — Двухфазный commit (preflight → entry → verification → hedge). Emergency hedge при partial fill — правильно. Per-exchange margin requirements.
- **V1 [`AtomicExecutionEngine`](arbitrage/system/execution.py:30)** — Idempotency nonces с TTL. Per-exchange locks для atomic balance read+reserve. Preflight margin check ДО размещения ордеров.
- **[`SlippageModel`](arbitrage/system/slippage.py)** — Реалистичная модель walk-the-book для оценки impact cost.

### ⚠️ Недостатки

- **ДВЕ системы исполнения** — V1 и V2 существуют параллельно. V2 не тестирован в production. Это огромный техдолг — нужно выбрать одно.
- **[`ExecutionConfig`](arbitrage/system/config.py:76)** — `hedge_retries: int = 3` в config, но V2 использует `max_hedge_attempts: int = 5` по умолчанию. Рассинхрон.
- **V1 ордер-тайп `ioc`** — Immediate-or-Cancel не гарантирует исполнение. В волатильном рынке IOC отклоняется → позиция открывается только на одной бирже.

### 🔴 Критические ошибки

1. **V2 [`_execute_both_legs()`](arbitrage/system/execution_v2.py)** — Обе ноги MARKET order одновременно. При расхождении >500ms в исполнении — спред уже другой. Нет slippage tolerance check ДО отправки.
2. **V1 [`execute_dual_entry()`](arbitrage/system/execution.py:77)** — При неудаче второго leg, первый leg НЕ закрывается автоматически. Hedge есть но он запускается ПОСЛЕ того как мы уже в позиции. В этот момент loss уже реальный.
3. **V2 position verification delay** — `POSITION_CHECK_DELAY = 1.0-2.0s`. HTX может отражать позицию через 3-5 секунд после fill. Это значит, что `Need hedge` может быть вызван ложно, и V2 попытается hedge-ить позицию, которой ещё нет.

---

## 5. MARKET INTELLIGENCE (`market_intelligence/`)

### ✅ Сильные стороны

- **Pipeline architecture** — [`run_once()`](market_intelligence/engine.py:113) чётко структурирован: collect → validate → features → regime → score → portfolio. Отличная модульность.
- **[`PortfolioAnalyzer`](market_intelligence/portfolio.py:9)** — Multi-factor risk: regime distribution, volatility regime, correlation, ATR, transition probability, drawdown penalty. VaR 95th percentile. Pairwise correlation penalty. Это institutional-grade.
- **Regime detection** — RegimeModel с multiple inputs (EMA cross, ADX, BB, RSI, vol). Blow-off и capitulation режимы — правильное распознавание экстремумов.
- **Adaptive ML weighting** — `_record_ml_feedback()` для обратной связи между циклами. Historical regime distribution корректирует risk multiplier.
- **Structured logging** — Cycle context с `new_cycle_context()`. JSONL persistence для audit trail.

### ⚠️ Недостатки

- **[`_candles_refresh_interval`](market_intelligence/engine.py:111)** — 2 минуты для MTF candles. 1H/4H/1D свечи обновляются каждые 2 минуты — это избыточно. 1H можно раз в 5 мин, 4H раз в 30 мин.
- **[`OpportunityScorer`](market_intelligence/scorer.py)** — Weight-параметры hardcoded. Нет динамической калибровки на основе фактической PnL.
- **Нет real-time regime updates** — Regime пересчитывается только при `run_once()`. При быстром переходе в panic regime, бот может продолжить trading с устаревшим режимом до следующего цикла.

### 🔴 Критические ошибки

1. **[`MarketIntelligenceEngine._compute_pipeline()`](market_intelligence/engine.py:159)** — Вызывается через `asyncio.to_thread()`. Если thread exception — pipeline падает, но caller (`run_once()`) получает exception и поднимает `RuntimeError("No market snapshots available")` — misleading message.
2. **[`PortfolioAnalyzer._pairwise_correlation_penalty()`](market_intelligence/portfolio.py:184)** — Корреляция берётся только к BTC. Два альткоина могут иметь 95% корреляцию между собой, но оба показывать 40% к BTC. Они оба получат полную аллокацию без взаимного штрафа.

---

## 6. HANDLERS & BOT UI (`handlers/`, `main.py`)

### ✅ Сильные стороны

- **[`AuthMiddleware`](main.py:50)** — Secure-by-default: если `ALLOWED_USER_IDS` пуст — доступ запрещён всем. Это правильное поведение для trading бота.
- **Per-user engine state** ([`_user_engines`](handlers/arbitrage_handlers_simple.py:132)) — Каждый Telegram-юзер имеет изолированный `_EngineState`. Предотвращает multi-user collisions.
- **Graceful shutdown** — Правильная отмена asyncio tasks, закрытие venue, private WS, position monitor.

### ⚠️ Недостатки

- **[`_EngineState.shutdown()`](handlers/arbitrage_handlers_simple.py:82)** — 5s timeout на monitor_task, затем `cancel()`. При активной hedge-операции это может оставить позицию открытой.
- **Множество callback handlers** — Более 50 callback_query.register вызовов в [`register_handlers()`](main.py:88). Сложно поддерживать, нет автогенерации из enum.
- **Нет pagination** для `/stats`, `/arb_history` — при большом количестве сделок сообщение Telegram превышает 4096 символов.

### 🔴 Критические ошибки

1. **[Emergency close handler](handlers/arbitrage_handlers_simple.py)** — Кнопка emergency close закрывает ВСЕ позиции одного юзера через `emergency_close_all()`. Нет подтверждения с деталями PnL. Пользователь может нажать случайно — и закрыть прибыльные позиции.
2. **`_es` singleton** ([arbitrage_handlers_simple.py:133](handlers/arbitrage_handlers_simple.py:133)) — Fallback engine state — shared между всеми вызовами без user_id. Если какой-то handler вызывает `_get_engine()` без user_id, он получит shared state с загрязнёнными данными.

---

## 7. UTILS & CONFIG (`arbitrage/utils/`, `arbitrage/system/config.py`)

### ✅ Сильные стороны

- **[`ExchangeRateLimiter`](arbitrage/utils/rate_limiter.py:73)** — Token bucket algorithm с per-exchange rates. 429 handling с exponential backoff. Burst capacity `max_tokens = rate * 2`. Lazy lock init для Python 3.9 compat.
- **[`TradingSystemConfig.from_env()`](arbitrage/system/config.py)** — Все параметры конфигурируются через env vars. `_first_env()` fallback chain. `_as_bool()`, `_as_float()`, `_as_int()` с graceful defaults.
- **Per-exchange fees** — [`_DEFAULT_FEE_PCT`](arbitrage/system/strategies/futures_cross_exchange.py:30) и [`_DEFAULT_SPOT_FEE_PCT`](arbitrage/system/strategies/cash_and_carry.py:38) — реалистичные taker rates.

### ⚠️ Недостатки

- **[`DEFAULT_RATES`](arbitrage/utils/rate_limiter.py:29)** — OKX 5.0 req/s — слишком консервативно для современных лимитов OKX (20 req/s public). Бот делает вдвое меньше запросов чем может.
- **Frozen dataclass для config** — Хорошо для immutability, но [`ExecutionConfig.margin_requirements`](arbitrage/system/config.py:72) — mutable `Dict`. Это нарушает immutability frozen dataclass.
- **Нет config validation при старте** — `config.validate()` есть, но проверяет только минимальные значения. Не проверяет что `min_spread_pct < total_fees_pct` — бессмысленная конфигурация.

### 🔴 Критические ошибки

1. **[`ExchangeRateLimiter.acquire()`](arbitrage/utils/rate_limiter.py:89)** — При активном 429 backoff, метод **ждёт** `await asyncio.sleep(wait)` внутри lock. Это блокирует ВСЕ остальные запросы к этому bucket на время backoff. Нужно ждать вне lock.
2. **[`_global_limiter`](arbitrage/utils/rate_limiter.py:159)** — Global mutable singleton. При параллельных `init_rate_limiter()` + `get_rate_limiter()` возможны race conditions.

---

## 🏆 ТОП-10 КРИТИЧЕСКИХ ПРОБЛЕМ (приоритет для исправления)

| # | Проблема | Impact | Severity |
|---|---|---|---|
| 1 | Два параллельных execution engine (V1/V2) | Путаница, дублирование кода, баги | 🔴 Critical |
| 2 | Две стратегии могут открыть 2 позиции на один символ | 2x непреднамеренный exposure | 🔴 Critical |
| 3 | Daily drawdown reset от старта бота, не midnight | Неверный DD tracking в первые/последние часы | 🔴 High |
| 4 | Rate limiter acquire() ждёт внутри lock | Блокирует все запросы к бирже | 🔴 High |
| 5 | BinanceWS line 136 — dead code вне метода | Потеря логирования отключений | 🟡 Medium |
| 6 | V2 position check delay 1-2s → false hedge | HTX отражает позицию через 3-5s | 🔴 High |
| 7 | Global mutable singleton `_global_limiter` | Race condition при инициализации | 🟡 Medium |
| 8 | min_spread_pct=0.08 < total_fees≈0.24 | Все cross-exchange сделки убыточны | 🔴 Critical |
| 9 | Нет TTL cleanup для orderbooks dict | Memory leak при большом symbol universe | 🟡 Medium |
| 10 | Correlation только к BTC, не pairwise | Не замечает корреляцию между парами | 🟡 Medium |

---

## 📈 ТРЕЙДЕРСКАЯ ОЦЕНКА

### Что работает правильно:
- **Delta-neutral подход** — long/short хеджирование на разных биржах — единственный способ "безрискового" арбитража в крипте.
- **Funding rate arbitrage** — сбор positive funding через short perp — стабильный 10-30% APR в бычьем рынке.
- **Notional-based exposure** — использование номинала, а не маржи — правильно, т.к. ликвидация происходит по номиналу.
- **Circuit breaker** — остановка после N failures — предотвращает cascade failures.

### Где стратегия нереалистична:
- **Cross-exchange арб 8bps** — На практике спред обычно 1-3bps для BTC, 5-15bps для алтов. После комиссий 24bps — edge отрицательный. Нужно либо снизить комиссии (maker orders), либо поднять `min_spread_pct` до 0.15+.
- **Triangular arbitrage** — 3 ноги × 0.1% комиссия = 0.3% cost. При profit threshold 15bps — это гарантированный убыток. Либо нужно иметь VIP tier с maker fee 0.01%, либо выключить стратегию.
- **Cash & Carry** — Это единственная стратегия с реально положительным матожиданием. APR 5-15% после комиссий, risk-free (однобиржевой).

### Рекомендации по доходности:
1. **Сфокусируйтесь на Cash & Carry** — стабильный 8-15% APR при минимальном риске.
2. **Поднимите maker fee tier** — все биржи дают 0.01-0.02% maker fee при объёмах. Это уменьшает round-trip с 0.24% до 0.06%.
3. **Выключите Triangular Arbitrage** — он никогда не будет прибыльным с текущими комиссиями.
4. **Добавьте funding rate filter** — открывать cross-exchange арб только когда funding rate > порог (дополнительный источник дохода помимо спреда).

---

## 🛠 ПРИОРИТЕТНЫЙ ПЛАН ДЕЙСТВИЙ

### Week 1 — Критические фиксы:
1. Удалить V1 или V2 (оставить один engine)
2. Заменить `min_spread_pct` на 0.15+
3. Добавить `is_symbol_in_use()` check между стратегиями
4. Fix drawdown reset на midnight UTC
5. Fix rate limiter acquire() — вынести sleep из lock

### Week 2 — Стабильность:
6. V2 position verification delay → 3-5s для HTX
7. Добавить TTL cleanup для orderbooks dict
8. Pairwise correlation penalty в portfolio analyzer
9. Fix BinanceWS line 136 indentation
10. Config validation: `min_spread_pct >= total_fees_pct`

### Week 3 — Доходность:
11. Maker-only execution для всех стратегий
12. Volatility-based position sizing (ATR scaling)
13. Funding rate filter для cross-exchange
14. Disable Triangular Arbitrage по умолчанию
15. Cash & Carry auto-exit при funding rate < 0
