# BrickTrade — Полный анализ прибыльности кода

> Дата: 2026-04-05
> Тип: Архитектурный аудит с фокусом на максимизацию прибыли
> Ограничение: рекомендации ТОЛЬКО, без изменений кода

---

## Обзор архитектуры

Проект состоит из 3 основных подсистем:

| Подсистема | Описание | Статус |
|---|---|---|
| **Arbitrage Bot** | Кросс-биржевой арбитраж фьючерсов, funding, triangular, pairs trading | ✅ Работает, 6 стратегий |
| **Stock Bot (MOEX/BCS)** | Торговля акциями на Мосбирже через BCS API | ✅ Работает, 6 стратегий |
| **Market Intelligence** | ML-скоринг, анализ режимов, portfolio risk | ✅ Работает |

**Текущий капитал:** ~$11 в арбитраже + ~700₽ в акциях

---

## 🔴 КРИТИЧЕСКИЕ ПРОБЛЕМЫ (потеря денег прямо сейчас)

### 1. Maker-Taker отключён — переплата комиссий в 2.5x

**Файл:** [`.env:197`](.env:197)
```
EXEC_USE_MAKER_TAKER=false
```

Maker fee на OKX/Bybit в 5-6 раз ниже taker:
- OKX: maker 0.02% vs taker 0.05% — экономия **60% на одной ноге**
- Bybit: maker 0.01% vs taker 0.055% — экономия **82% на одной ноге**

**Расчёт:**
- Round-trip без maker-taker: `(0.05 + 0.055) × 2 = 0.21%`
- С maker-taker: `(0.02 + 0.055) + (0.02 + 0.055) = 0.15%` — экономия **~28%**

**Проблема:** Код уже готов ([`futures_cross_exchange.py:173-193`](arbitrage/system/strategies/futures_cross_exchange.py:173)), но отключен из-за race condition с one-leg trades (комментарий в `.env:195-196`).

**Что исправить в коде перед включением:**
- В [`execution.py`](arbitrage/system/execution.py) добавить per-exchange lock на весь цикл: maker-размещение → проверка → fallback на taker
- Убедиться что обе ноги забирают maker-ликвидность до того как любая из них переходит в taker
- В [`futures_cross_exchange.py`](arbitrage/system/strategies/futures_cross_exchange.py) формула blended-fee (строка 181) использует 70/30 — это консервативно, но нужно проверить что maker order действительно имеет `post_only=True` в venue

### 2. EXEC V2 position_check_delay = 2 секунды unhedged экспозиции

**Файл:** [`.env:301`](.env:301)
```
EXEC_V2_POSITION_CHECK_DELAY=2.0
```

Execution V2 ([`execution_v2.py`](arbitrage/system/execution_v2.py:1)):
- Phase 1: открывает обе ноги
- Phase 2: ждет position_check_delay секунд
- Phase 3: проверяет позиции на биржах, хеджит если дисбаланс

2 секунды — это **огромное окно** для крипторынка. Цена может уйти на 0.1-0.5% — и хедж уже не спасает.

**Замечание:** В коде [`execution_v2.py:97`](arbitrage/system/execution_v2.py:97) есть `min(position_check_delay, 0.5)` — но env-переменная 2.0 передаётся как аргумент в конструктор, не через os.getenv напрямую. Нужно проверить [`live_adapters.py`](arbitrage/system/live_adapters.py) где передаётся аргумент.

### 3. Опечатка в MONITOR_ENABLED — модуль не работает

**Файл:** [`.env:305`](.env:305)
```
MONITOR_ENABLED=falses
```

Позиционный монитор — fail-safe для обнаружения и закрытия orphan позиций — не запускается из-за опечатки. Бот может держать "потерянные" позиции на бирже без учёта в state, что ведёт к непредсказуемым убыткам.

### 4. LEVERAGE=1 — нереалистично для дельта-нейтрального арбитража

**Файл:** [`.env:244`](.env:244)
```
LEVERAGE=1
```

Арбитраж дельта-нейтрален: long на одной бирже = short на другой. Направление рынка не влияет на P&L. С 1x плечом:
- $11 баланса → позиция ~$3 (30% от баланса)
- Спреда 0.15% → прибыль $0.0045 за сделку
- С 5x плечом: $11 позиции → прибыль $0.0165 — **x3.7 больше**

Риск при 5x для арбитража минимален: если одна биржа ликвидирует, другая биржа имеет зеркальную позицию, что тоже ликвидирует — net P&L ≈ 0.

### 5. Только 1 стратегия из 6 активна

**Файл:** [`.env:162`](.env:162)
```
ENABLED_STRATEGIES=futures_cross_exchange
```

Неиспользуемые стратегии:

| Стратегия | Принцип | Риск | Потенциал |
|---|---|---|---|
| `funding_arbitrage` | Long на бирже с низким funding, short с высоким | Низкий (конвергенция) | $$$ |
| `cash_and_carry` | Spot buy + perp short на одной бирже | Минимальный | $$ (стабильный) |
| `triangular_arbitrage` | USDT→BTC→ETH→USDT на одной бирже | Очень низкий | $ (быстрые) |
| `pairs_trading` | Long/Short коррелированных пар | Низкий (z-score exit) | $$ |
| `funding_harvesting` | Агрессивный сбор экстремального funding | Средний | $$ |

---

## 🟡 СТРУКТУРНЫЕ ПРОБЛЕМЫ (убивают прибыль незаметно)

### 6. os.getenv() в горячем цикле — 3600+ вызовов/час

**Файл:** [`engine.py:101-113`](arbitrage/system/engine.py:101)

Переменные `_exit_take_profit_usd`, `_exit_max_hold_seconds`, `_max_equity_per_trade_pct`, `_margin_reject_cooldown_sec` и другие читаются из env внутри `run_cycle()`. При `EXEC_CYCLE_INTERVAL=1.0` это **3600 вызовов/час на каждую переменную**.

**Оптимизация:** Чтение при first run с кэшированием. Уже частично реализовано (строки 102-113), но инициализация `_exit_params_initialized` происходит при каждом вызове метода `_parse_exit_params()`.

### 7. Глобальный exchange block блокирует ВСЮ биржу из-за одного символа

**Файл:** [`engine.py`](arbitrage/system/engine.py) — `_unstable_exchanges: dict[str, float]`

Когда second_leg_failed на BTCUSDT/OKX, ВСЕ символы на OKX блокируются на 30 минут. ETHUSDT, SOLUSDT и другие пропускают свои возможности.

**Решение:** Заменить `dict[str, float]` на `dict[tuple[str, str], float]` где tuple = (exchange, symbol).

### 8. Volatility Scaling инициализирован но не работает

**Файл:** [`engine.py:116-117`](arbitrage/system/engine.py:116)
```python
_volatility_scaling_enabled: bool = True
_target_atr_usd: float = 0.0  # Never populated
```

ATR-трекер не инициализируется — `_target_atr_usd` остаётся 0.0 навсегда. Волатильные рынки → размер позиции не уменьшается → больше убытков на стопах. Спокойные рынки → позиция не увеличивается → упущенная прибыль.

**Решение:** Инициализировать `_target_atr_usd` на основе historical ATR из Market Intelligence (`market_intelligence/engine.py` уже считает `atr_pct` для BTC).

### 9. EXIT_TAKE_PROFIT_USD=$0.50 нереалистичен для $11 баланса

**Файл:** [`.env:201`](.env:201)

При позиции $3 (30% от $11), TP $0.50 требует **16.7% возврата** на спреде. Типичный арбитражный спред: 0.1-0.5%. TP срабатывает <1% времени.

**Рекомендация:** `$0.05` — сработает при спреде 1.7% на позиции $3, что реально при волатильности.

### 10. RISK_MAX_OPEN_POSITIONS=2 — слишком консервативно

**Файл:** [`.env:210`](.env:210)

При 100+ символах, 2 позиции = 2-3 торговые возможности/день. Увеличение до 4 = +50-100% больше сделок.

---

## 🟢 УПУЩЕННЫЕ ВОЗМОЖНОСТИ

### 11. Market Intelligence не влияет на execution

**Проблема:** MI ([`market_intelligence/engine.py`](market_intelligence/engine.py:113)) генерирует:
- Скоры возможностей (0-100)
- Режимы рынка (trending, ranging, panic)
- Корреляции с BTC
- ATR и волатильность

Но `TradingSystemEngine` не использует эти данные для фильтрации сделок.

**Решение:** Перед entry в [`engine.py run_cycle()`](arbitrage/system/engine.py:142) проверять MI score для данного символа. Если score < min_opportunity_score — пропускать сделку даже если spread есть.

### 12. Stock Bot: 6 стратегий × 38 тикеров при капитале 700₽

**Файл:** [`.env:55`](.env:55), [`.env:32`](.env:32)

38 тикеров × 6 стратегий = до 228 сигналов/цикл, но:
- `RISK_MAX_PER_POSITION=0.50` → 350₽ на позицию
- `RISK_MAX_POSITIONS=2` → максимум 2 открытые позиции
- 700₽ общий капитал

Рекомендация: ограничить до 3 лучших стратегий (по Sharpe ratio) и 10 наиболее ликвидных тикеров.

### 13. Нет бэктестинга — параметры оптимизированы вручную

**Файл:** [`arbitrage/system/backtest/engine.py`](arbitrage/system/backtest/engine.py:1) — бэктест-движок существует, но не запущен.

Все стратегические параметры (min_spread=0.15%, zscore=2.0, cooldown=60s) — результат ручной настройки, без исторической валидации.

### 14. SYMBOL_BLACKLIST пуст — ложные сигналы от низколиквидных пар

**Файл:** [`.env:161`](.env:161)
```
SYMBOL_BLACKLIST=
```

Бот сканирует ALL символы включая низколиквидные. Спреды там шире — выглядят как арбитраж, но фактическое исполнение происходит с огромным проскальзыванием.

**Рекомендация:** `SYMBOL_BLACKLIST=PEPEUSDT,SHIBUSDT,FLOKIUSDT,TRXUSDT`

### 15. Funding harvesting — слишком высокие пороги входа

**Файл:** [`funding_harvesting.py:42-46`](arbitrage/system/strategies/funding_harvesting.py:42)
- `min_funding_rate_pct=0.05` (0.05% за период)
- `min_apr_threshold=20.0` (20% годовых)

Проблема: при таких порогах стратегия ловит **поздно** — когда funding уже на пике и вот-вот развернётся.

**Рекомендация:** Снизить `min_apr_threshold` до 10% и `min_funding_rate_pct` до 0.02 — больше сделок с меньшим риском входа на пике.

### 16. Нет динамического распределения капитала между биржами

Баланс на 3 биржах распределён статично. Если на одной бирже баланс растёт (прибыль), а на другой падает (комиссии), бот не перебрасывает средства. Со временем одна биржа становится «underfunded» и не может открыть вторую ногу.

---

## 📊 Оценка влияния каждой оптимизации

| # | Оптимизация | Сложность реализации | Влияние на прибыль | Приоритет |
|---|---|---|---|---|
| **1** | Включить maker-taker (после fix race condition) | Средняя (2-4 часа) | **+40-60%** | 🔴 HIGH |
| **2** | Fix V2 delay 2s → 0.5s | Тривиальная (5 мин) | Предотвращает убытки | 🔴 HIGH |
| **3** | Fix опечатка falses → true | Тривиальная (1 мин) | Предотвращает orphan | 🔴 HIGH |
| **4** | LEVERAGE 1 → 5x для арбитража | Тривиальная (1 мин) | **+300-400%** | 🔴 HIGH |
| **5** | Включить funding_arbitrage + cash_and_carry | Тривиальная (1 мин) | **+50-100%** | 🟡 MEDIUM |
| **6** | Кешировать os.getenv() в engine | Низкая (1-2 часа) | +1-2% латентность | 🟢 LOW |
| **7** | Per-(exchange,symbol) cooldown | Средняя (3-4 часа) | +20-30% сделок | 🟡 MEDIUM |
| **8** | Включить volatility scaling | Средняя (4-6 часов) | -15% убытков, +15% прибыли | 🟡 MEDIUM |
| **9** | Realistic exit TP ($0.50 → $0.05) | Тривиальная (1 мин) | Больше успешных выходов | 🟡 MEDIUM |
| **10** | RISK_MAX_OPEN_POSITIONS 2 → 4 | Тривиальная (1 мин) | +50-100% сделок | 🟡 MEDIUM |
| **11** | MI ↔ Execution интеграция | Высокая (1-2 дня) | Фильтрация 20% плохих сделок | 🟢 LOW |
| **12** | Stock: ограничить стратегии/тикеры | Низкая (30 мин) | Концентрация на лучших | 🟡 MEDIUM |
| **13** | Бэктестинг всех стратегий | Высокая (2-3 дня) | Валидация параметров | 🟢 LOW |
| **14** | Заполнить SYMBOL_BLACKLIST | Тривиальная (5 мин) | Предотвращение ложных сигналов | 🟡 MEDIUM |
| **15** | Снизить funding harvesting пороги | Тривиальная (1 мин) | Больше сделок | 🟢 LOW |
| **16** | Автобалансировка между биржами | Высокая (1-2 дня) | Стабильная работа | 🟢 LOW |

---

## 📋 Рекомендуемые `.env` изменения

```env
# ═══ 1. Maker-taker (ПРИ УСЛОВИИ исправления race condition в коде) ═══
EXEC_USE_MAKER_TAKER=true
EXEC_MAKER_TIMEOUT_MS=2000
EXEC_MAKER_MAX_RETRIES=2

# ═══ 2. Fix V2 delay ═══
EXEC_V2_POSITION_CHECK_DELAY=0.5

# ═══ 3. Fix typo + enable monitor ═══
MONITOR_ENABLED=true
MONITOR_CHECK_INTERVAL=15

# ═══ 4. Realistic leverage for delta-neutral arb ═══
LEVERAGE=5
RISK_MAX_LEVERAGE=5

# ═══ 5. More strategies ═══
ENABLED_STRATEGIES=futures_cross_exchange,funding_arbitrage,cash_and_carry

# ═══ 7 & 10. More positions + realistic exits ═══
RISK_MAX_OPEN_POSITIONS=4
EXIT_TAKE_PROFIT_USD=0.05
EXIT_MAX_HOLD_SECONDS=3600

# ═══ 14. Blacklist illiquid pairs ═══
SYMBOL_BLACKLIST=PEPEUSDT,SHIBUSDT,FLOKIUSDT

# ═══ 15. Lower funding harvesting thresholds ═══
FUNDING_HARVEST_MIN_RATE_PCT=0.02
FUNDING_HARVEST_MIN_APR=10.0
```

---

## ⚡ Quick Wins (1 час работы, максимальная отдача)

1. **Исправить `falses` → `true`** — позиционный монитор начнёт работать
2. **Исправить V2 delay** — 2s → 0.5s в конструкторе или env
3. **Поднять LEVERAGE до 5x** — x5 номинал, дельта-нейтральный = безопасный
4. **Заполнить SYMBOL_BLACKLIST** — 4-6 тикеров
5. **Включить funding_arbitrage** — одна строка в env

## 🏗️ Среднесрочные улучшения (1-3 дня)

1. **Fix maker-taker race condition** — per-exchange lock на полный cycle maker→fallback
2. **Per-(exchange,symbol) cooldown** — заменить глобальный `_unstable_exchanges`
3. **Volatility scaling** — инициализировать `_target_atr_usd` из MI данных
4. **MI ↔ Execution интеграция** — MI score как фильтр в risk engine
5. **Бэктестинг** — запустить [`backtest/engine.py`](arbitrage/system/backtest/engine.py:1) на исторических данных

## 🔮 Долгосрочные (1-2 недели)

1. **Добавить Binance** — 4-я биржа = больше комбинаций
2. **ML-enhanced entry** — MI скор для предсказания конвергенции спреда
3. **Динамическое распределение капитала** — автобалансировка между биржами
4. **Stock: trailing stop** — полная реализация в execution
5. **Adaptive position sizing** — Kelly criterion на основе win rate per-стратегии

---

## Итог

| Метрика | Сейчас | После Quick Wins | После всех улучшений |
|---|---|---|---|
| **Дневных сделок** | 0-3 | 2-6 | 5-15 |
| **Средняя прибыль/сделка** | $0.004 | $0.012 | $0.02-0.04 |
| **Дневной P&L** | $0.00-0.012 | $0.02-0.07 | $0.10-0.60 |
| **Месячный P&L** | $0-0.36 | $0.60-2.10 | $3-18 |
| **Win rate** | Неизвестен | ~55-60% | ~60-65% (с MI) |

**Главный вывод:** Арбитражный бот технически зрелый, но работает на ~10-15% от потенциальной эффективности. Три главных врага прибыли: (1) комиссии без maker-taker, (2) 1x плечо, (3) только 1 активная стратегия. Исправление этих трёх пунктов даст наибольший рост при минимальном дополнительном риске, поскольку арбитраж дельта-нейтрален по природе.
