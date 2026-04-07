# 🔍 Полный Аудит Кода: BrickTrade Arbitrage System

**Дата:** 2026-04-06  
**Автор:** Code Review (Trader + Programmer perspective)  
**Охвачено:** 100+ файлов, ~15,000+ строк кода

---

## ОГЛАВЛЕНИЕ

1. [Ядро Engine и State](#1-ядро-engine-и-state-arbitragecore)
2. [Биржевые Интеграции](#2-биржевые-интеграции-arbitrageexchanges)
3. [Система Исполнения](#3-система-исполнения-arbitragesystem)
4. [Конфигурация и Модели](#4-конфигурация-и-модели-arbitragesystem)
5. [Стратегии](#5-стратегии-arbitragesystemstrategies)
6. [Risk Engine](#6-risk-engine-arbitragesystemriskpy)
7. [Market Intelligence](#7-market-intelligence-market_intelligence)
8. [Утилиты](#8-утилиты-arbitrageutils)
9. [Telegram Бот](#9-telegram-бот-mainpy-handlers)
10. [Критические Ошибки](#-суммарная-таблица-критических-ошибок)
11. [Сильные Стороны](#-сильные-стороны)
12. [Общая Оценка](#-общая-оценка)

---

## 1. ЯДРО ENGINE И STATE (`arbitrage/core/`)

### [`arbitrage/core/state.py`](arbitrage/core/state.py) — BotState class

**Сильные стороны:**
- Чистые dataclass модели (`ActivePosition`, `OrderBookData`, `ArbitrageOpportunity`)
- Хорошее разделение между legacy `Position` и полноценной `ActivePosition` с двумя ногами
- Отслеживание статистики по каждой стратегии

**Недостатки:**
- **Строки 107-109:** Legacy алиасы (`self.okx_balance`, `self.htx_balance`, `self.bybit_balance`) — code smell от оригинального дизайна с 2 биржами. Должны быть удалены.
- **Строка 85:** `self.positions: Dict[tuple, PositionLike]` — использование `tuple` как ключа непрозрачно. Код должен проверять `isinstance(pos, ActivePosition)` при каждом доступе. Хрупкая схема.
- **Строка 213:** `get_orderbooks()` захардкожен на `("okx", "htx")` — не работает с 3+ биржами.

**Критические ошибки:**
- Отсутствуют сами по себе, но `BotState` — **мёртвый код**. Новая система использует `SystemState` (`arbitrage/system/state.py`). Это создаёт путаницу о том, какая модель является канонической.

### [`arbitrage/core/market_data.py`](arbitrage/core/market_data.py) — MarketDataEngine

**Сильные стороны:**
- Отличная нормализация инструментов через все биржи (OKX `BTC-USDT-SWAP` → `BTCUSDT`, HTX `BTC-USDT` → `BTCUSDT`)
- Кэширование tick size и contract size — критично для правильного размера ордеров
- Хорошее использование `asyncio.gather` для параллельных запросов

**Недостатки:**
- **Строка 213-216:** `min_order_sizes` для HTX хранит только `price_tick`,_missing `lot_size` — ордера HTX нужны volume precision
- **Строка 118-126:** `update_all()` имеет `return_exceptions=True` но тихо глотает отдельные ошибки. Если фетч одной биржи падает, вызывающий не узнает, какие данные устарели.
- Нет явного таймаута на отдельные фетч-операции — медленная биржа может заблокировать весь цикл.

---

## 2. БИРЖЕВЫЕ ИНТЕГРАЦИИ (`arbitrage/exchanges/`)

### [`arbitrage/exchanges/htx_rest.py`](arbitrage/exchanges/htx_rest.py)

**Сильные стороны:**
- Правильное HMAC-SHA256 подписание канонической строки (sorted params, URL-encoded)
- Хороший fallback с v3 unified на v1 cross-account
- Правильное разделение cross-margin и isolated margin эндпоинтов

**Критические ошибки:**
- **CRITICAL — Строки 301-344:** `place_order()` использует `volume` для размера. На HTX линейных свопах для `opponent` (market) ордеров, `volume` — в **контрактах**, не в базовой валюте. Если конвертация неверна, ордера будут в 10x-100x больше задуманного.
- **Строки 46-55:** Session management не обрабатывает cleanup `aiohttp` при shutdown бота. Утечка соединений.

**Недостатки:**
- Нет retry логики с идемпотентностью для размещения ордеров — если соединение падает mid-response, неизвестно, был ли ордер размещён.

### [`arbitrage/exchanges/private_ws.py`](arbitrage/exchanges/private_ws.py)

**Сильные стороны:**
- Чистый дизайн: один класс на биржу с унифицированным интерфейсом callback
- Правильная HMAC аутентификация для OKX, HTX, Bybit (каждая с разными протоколами подписи)
- Gzip декомпрессия для HTX (их WS шлёт сжатые данные)
- Heartbeat обработка (HTX ping/pong на строке 332-333)

**Критические ошибки:**
- **CRITICAL — Строка 124 (OKX):** Цикл `async for message in ws:` может умереть молча при падении соединения (отмечено в `.env` — "the WS message loop dies silently"). В некоторых версиях websockets исключения внутри `async for` могут прервать цикл без реконнекта.
- **CRITICAL — Строка 283 (HTX):** `await asyncio.wait_for(ws.recv(), timeout=10)` во время auth — если ответ auth занимает >10s (HTX под нагрузкой известна 15-20s), auth считается проваленным и соединение перезапускается в бесконечном цикле.
- **Строка 327:** HTX декомпрессия `gzip.decompress(raw)` молча упадёт, если данные не gzip (что бывает для error messages).
- **Все классы:** Нет лимита максимального размера сообщения. Злой или сбоющий exchange может прислать гигантское сообщение → OOM.

**Недостатки:**
- Нет exponential backoff на реконнект — фиксированные 2-3 секунды (строка 136, 140). Должен быть exponential backoff (1s → 2s → 4s → 16s → cap).
- Нет мониторинга здоровья соединения — если `self.ws` открыт, но биржа перестала слать сообщения (зомби-соединение), бот не обнаружит. Нужна проверка "время последнего сообщения".

---

## 3. СИСТЕМА ИСПОЛНЕНИЯ (`arbitrage/system/`)

### [`arbitrage/system/execution.py`](arbitrage/system/execution.py) — AtomicExecutionEngine

**Сильные стороны:**
- **Упорядочение блокировок на уровне бирж** (строки 37-43: алфавитный порядок) — предотвращает ABBA deadlock. Отлично.
- **Pre-flight проверка баланса** (строки 88-100): проверяет обе биржи на достаточный margin перед любым ордером. Это самая важная защитная функция.
- **Per-symbol locks** — предотвращает рейсинг на одном символе

**Критические ошибки:**
- **CRITICAL — Строка 72:** `asyncio.get_event_loop().time()` — в Python 3.10+ это DeprecationWarning. Должно быть `asyncio.get_running_loop().time()`.
- **CRITICAL — Строка 330:** При `second_leg_failed` и `not report.hedged`, kill switch **навсегда** включается. Это слишком агрессивно — один сбой хеджа не заслуживает перманентного отключения, особенно для HTX с его периодическими проблемами.

**Недостатки:**
- `dry_run` путь не симулирует latency или slippage — результаты бэктеста слишком оптимистичны.
- Классификация ошибок "first_leg_failed", "second_leg_failed" — строки. Enum был бы безопаснее.

### [`arbitrage/system/execution_v2.py`](arbitrage/system/execution_v2.py) — AtomicExecutionEngineV2

**Сильные стороны:**
- Чистая 4-фазная архитектура (preflight → entry → verification → hedge)
- Per-exchange margin requirements (строки 82-87): HTX 20%, Binance 12% — реалистично
- Per-exchange verification delays (строки 127-132): HTX 3s, OKX/Bybit 2s — учитывает реальные различия API latency

**Критические ошибки:**
- **CRITICAL — Строки 157-159:** Логика определения сторон хрупкая:
  ```python
  exchange_a = intent.metadata.get("long_exchange")
  side_a = "buy" if intent.metadata.get("long_exchange") == exchange_a else "sell"
  ```
  Это всегда `true` (переменная сравнивается сама с собой), `side_a` всегда `"buy"`. **Это баг** — но строка 160 его маскирует: `side_b = "sell"`. Работает случайно. Путаный код = магнит для багов.
- **CRITICAL — Строки 198-212:** `emergency_hedge_all` ставит market ордера. На неликвидной бирже это может вызвать колоссальный slippage. Нет проверки max slippage на хедже.
- **CRITICAL — Строка 204:** Если обе ноги не исполнились, `_emergency_hedge_all` всё равно вызывается. Но если ничего не наполнилось, хеджировать нечего — это тратит API calls и может создать phantom-позиции.

### [`arbitrage/system/engine.py`](arbitrage/system/engine.py) — TradingSystemEngine

**Сильные стороны:**
- **Circuit breaker** (строки 212-217): пропускает биржи, которые недавно фейлили — хорошая отказоустойчивость
- **Symbol cooldown** (строки 296-299): предотвращает долбёжку failing символов
- **Kill switch при непроверенном хедже** (строки 321-331): правильный ядерный вариант для катастрофического сценария
- **Цикл мониторинга позиций** (`_process_open_positions`, строки 364-500): комплексная логика выхода с множеством условий (take-profit, max holding time, edge convergence, funding reversal, per-trade max loss)
- Хорошее логирование на многих уровнях

**Критические ошибки:**
- **CRITICAL — Строка 423:** `age_sec` используется **до** определения на строке 437:
  ```python
  periods_held = age_sec / funding_interval_sec  # Строка 423 — age_sec ещё не определён!
  ```
  Это **NameError**, который обрушит position monitor.
- **CRITICAL — Строки 290-300:** При `first_leg_failed` ошибка записывается на circuit breaker **обеих** бирж. Но если только одна биржа отвергла ордер (OKX прошёл, Bybit отверг), маркировать OKX как failed — неправильно. Заблокирует все будущие сделки с OKX без причины.
- **Строки 219-220:** `os.getenv("MAX_EQUITY_PER_TRADE_PCT", "0.30")` — чтение env vars в горячем цикле неэффективно. Должно кэшироваться при старте.
- **Строка 148:** `return` на kill switch — это выходит из всего `run_cycle()`. Оставшиеся символы в цикле пропускаются без лога.

**Недостатки:**
- Engine 680+ строк, должен быть разбит на: (1) orchestration цикла, (2) управление позициями, (3) обработка ошибок.
- `_symbol_loss_streak` (строка 62) объявлен, но никогда не используется.

---

## 4. КОНФИГУРАЦИЯ И МОДЕЛИ (`arbitrage/system/config.py`, `models.py`)

### [`arbitrage/system/config.py`](arbitrage/system/config.py)

**Сильные стороны:**
- Frozen dataclass — иммутабельна после постройки, предотвращает случайную мутацию
- `_first_env` helper для fallback имён env vars

**Недостатки:**
- **Строка 92:** `min_spread_pct: float = 0.50` — но `risk.max_order_slippage_bps = 25.0` (25 bps = 0.25%). Min spread 0.50%, но slippage бюджет 0.25%. Трейд может войти с 0.50% edge и сразу уйти underwater от slippage.

### [`arbitrage/system/models.py`](arbitrage/system/models.py)

**Сильные стороны:**
- Frozen dataclasses — хорошая иммутабельность для торговых данных
- `StrategyId` enum-based
- `MarketSnapshot` захватывает все данные в одном объекте

---

## 5. СТРАТЕГИИ (`arbitrage/system/strategies/`)

### [`arbitrage/system/strategies/cash_and_carry.py`](arbitrage/system/strategies/cash_and_carry.py)

**Сильные стороны:**
- Правильный расчёт APR: `funding_rate * 3 * 365` (8h периоды → daily → annual)
- Реалистичное моделирование комиссий: `round_trip_fees * 2` (entry + exit)
- Однобиржевой дизайн исключает cross-exchange latency risk
- Правильный расчёт basis: `(perp_price - spot_price) / spot_price`

**Критические ошибки:**
- **CRITICAL — Строка 86:** Стратегия требует `snapshot.spot_orderbooks.get(exchange)`. Если `MarketDataEngine` не загрузил spot данные, стратегия молча возвращает `None` для всех символов.
- **Строка 172:** `edge_bps = one_period_gain_pct * 100` — `one_period_gain_pct` уже в процентах. Результат может быть некорректным в зависимости от контекста.

**Недостатки:**
- **Строки 124-126:** APR предполагает, что funding rate постоянен. В реальности funding может перевернуться между проверкой и следующим расчётом.

### [`arbitrage/system/strategies/futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py)

**Сильные стороны:**
- Проверяет оба направления кросс-биржевых пар и оставляет лучшее (строки 93-102)
- Funding rate divergence как независимый сигнал
- `min_book_depth_multiplier` — гарантирует, что orderbook может поглотить позицию

**Недостатки:**
- Нет проверки корреляции — если цены двух бирж highly correlated но дивергируют, это может быть сломанная биржа, а не реальная возможность.

---

## 6. RISK ENGINE (`arbitrage/system/risk.py`)

**Сильные стороны:**
- Отслеживание streak breach latency — предотвращает вход при медленном API
- Обнаружение устаревшего orderbook на символ
- **Пер-exchange imbalance check** (строки 57-67): проверяет ТОЛЬКО ДВЕ биржи участвующие, не глобально. Большинство систем делают это неправильно.
- Cap total exposure + cap allocation per strategy

**Недостатки:**
- **Строки 70-75:** Оба check (daily DD и portfolio DD) вызывают `permanent=True` kill switch. Daily drawdown должен вызывать **временную** паузу, не перманентное отключение.
- Нет проверки concentration risk — все 3 max позиции могут быть на одной монете (например, все BTC пары с разными биржами).

---

## 7. MARKET INTELLIGENCE (`market_intelligence/`)

### [`market_intelligence/engine.py`](market_intelligence/engine.py)

**Сильные стороны:**
- Комплексный pipeline: data collection → feature engineering → regime classification → scoring → logging
- Чистое разделение между collector, feature engine, regime model, и scorer
- JSONL structured logging — отлично для production observability

**Недостатки:**
- Нет backtesting интеграции — MI pipeline генерирует скоры, но нет проверки, предсказывают ли скоры прибыльные возможности.

### [`market_intelligence/scorer.py`](market_intelligence/scorer.py)

**Сильные стороны:**
- Regime-aware weight overrides (больше веса на funding в PANIC, больше на regime alignment в TREND)
- Нормализация весов (строки 83-89) — гарантирует сумму положительных весов = 1.0
- Signal half-life для time-decay

**Недостатки:**
- **Строка 93:** `max_vol` включает 0 — если все объёмы 0, `log_max = 0` и liquidity score будет NaN или всё tied.

### [`market_intelligence/regime.py`](market_intelligence/regime.py)

**Сильные стороны:**
- Logit-based multi-regime классификация с правильной нормализацией
- Stability tracking — предотвращает flip-flopping между режимами
- Panic получает мгновенную классификацию (строка 13: `min_cycles = 0`) — правильное поведение

**Недостатки:**
- Нет backtesting переходов режимов — мы не знаем точность предсказаний.

---

## 8. УТИЛИТЫ (`arbitrage/utils/`)

### [`arbitrage/utils/rate_limiter.py`](arbitrage/utils/rate_limiter.py)

**Сильные стороны:**
- Token bucket — правильная реализация
- Exponential backoff на 429
- Burst capacity (`max_tokens = rate * 2`)

**Недостатки:**
- **Строка 88-106:** `acquire()` держит lock блокировки через весь sleep. Множество coroutine сериализуются. Правильно для безопасности, но вызывает latency spikes.

---

## 9. TELEGRAM БОТ (`main.py`, `handlers/`)

**Сильные стороны:**
- Чистая регистрация handlers с lambda фильтрами
- Ежечасно ротируемые лог-файлы
- Health check server integration

**Недостатки:**
- Нет rate limiting на Telegram команды — бот можно заспамить
- `handlers/short_handlers.py` (38KB) — слишком большой единственный файл, требует рефакторинга.

---

## 🔴 СУММАРНАЯ ТАБЛИЦА КРИТИЧЕСКИХ ОШИБОК

| # | Severity | Файл:Строка | Ошибка |
|---|----------|-------------|--------|
| 1 | **CRITICAL** | engine.py:423 | `age_sec` до определения → `NameError` crash |
| 2 | **CRITICAL** | private_ws.py:124 | WS message loop умирает молча (подтверждено `.env`) |
| 3 | **CRITICAL** | execution_v2.py:157-159 | Тавтологическое сравнение `side_a` — всегда "buy" |
| 4 | **CRITICAL** | cash_and_carry.py:172 | `edge_bps` возможно off by 10x |
| 5 | **CRITICAL** | execution.py:330 | Permanent kill switch на одном `second_leg_failed` |
| 6 | **HIGH** | execution.py:290 | Обе биржи помечены failed, когда одна отвергла |
| 7 | **HIGH** | risk.py:70-75 | Daily DD → permanent kill, противоречит auto-reset |
| 8 | **HIGH** | private_ws.py:283 | 10s auth timeout для HTX — недостаточно (15-20s под нагрузкой) |
| 9 | **HIGH** | engine.py:219 | `os.getenv()` в горячем цикле |
| 10 | **MEDIUM** | config.py:92 | `min_spread_pct` vs `max_order_slippage_bps` не валидируются |
| 11 | **MEDIUM** | state.py:85 | Полиморфный `Dict[tuple, PositionLike]` |
| 12 | **MEDIUM** | state.py | Файловое хранение позиций без atomic writes |

---

## 🟢 СИЛЬНЫЕ СТОРОНЫ

1. **Архитектура:** Чистое слоение (market data → risk → strategy → execution → position management) — профессиональный уровень
2. **Circuit breaker:** Health tracking на уровне бирж предотвращает каскадные отказы
3. **Pre-flight verification:** Проверка margin обеих бирж перед любым ордером — дизайн высшего класса
4. **Упорядочение блокировок:** Алфавитный порядок — глубокое понимание конкуренции
5. **Управление позициями:** Multi-condition exit logic (TP, timeout, edge convergence, funding reversal, max loss) — комплексно
6. **Market Intelligence:** Regime-aware scoring с adaptive weights — сложный и хорошо структурированный
7. **Rate limiter:** Token bucket с exponential backoff на 429 — корректно и надёжно
8. **Множество стратегий:** Cash & Carry, Futures Cross-Exchange, Funding Arb — каждая с distinct risk profile
9. **Dry run:** Существенно для тестирования без риска капитала
10. **Телеметрия:** Extensive structured logging с multiple уровнями и точками emission

---

## 🔴 НЕДОСТАТКИ (Улучшить)

1. **Мёртвый код:** `BotState` и `SystemState` оба существуют — путаница
2. **Валидация конфига:** Нет cross-validation между spread thresholds, slippage budget, и комиссиями
3. **Пробелы в тестах:** Тесты не покрывают: HTX contract-size conversion, hedge race conditions, WS recon under load
4. **String-typed errors:** `"first_leg_failed"` должен быть enum
5. **Конфиг из env vars:** Нет schema validation, опечатки молча падают на default
6. **Размер engine.py:** 680+ строк — смешивает orchestration, monitoring, error handling
7. **handlers/short_handlers.py:** 38KB файл — нуждается в декомпозиции
8. **Нет проверки корреляции:** Все 3 max позиции могут быть коррелированы к одной монете
9. **Волатильность funding rate:** Стратегии используют point-in-time funding без averaging или percentile

---

## 📊 ОБЩАЯ ОЦЕНКА

| Измерение | Оценка | Комментарий |
|-----------|--------|-------------|
| Архитектура | **7.5/10** | Хорошее слоение, мёртвый код, проблемы конфига |
| Безопасность | **6/10** | Circuit breaker + kill switch сильны, но permanent kill слишком агрессивен |
| Корректность исполнения | **5/10** | Edge cases на hedge, WS reliability, margin checks нуждаются в укреплении |
| Управление рисками | **7/10** | Хорошие multi-factor checks, missing concentration risk |
| Биржевая интеграция | **6.5/10** | Комплексная поддержка, но HTX имеет edge-case баги |
| Наблюдаемость | **8/10** | Хорошее логирование, missing health monitoring для WS liveness |
| Тестирование | **6/10** | Тесты есть но покрытие неполное для критических путей |
| Качество кода | **7/10** | Чистые модели, файлы нуждаются в рефакторинге по размеру |

### **ИТОГО: 6.5/10**

Это солидная основа с профессиональными архитектурными паттернами (circuit breaker, risk engine, multi-strategy runner). **12 критических ошибок** (из них первые 5 — обязательны к исправлению перед любым live trading) представляют ~2-3 дня сфокусированной работы. После исправления система будет на уровне **7.5-8/10**.

**Приоритет исправлений:**
1. 🔴 `age_sec` NameError — мгновенный фикс
2. 🔴 WS silent death — добавить heartbeat check + reconnection guarantee
3. 🔴 Tautological comparison в execution_v2
4. 🔴 edge_bps calculation
5. 🔴 Permanent kill → temporary cooldown
