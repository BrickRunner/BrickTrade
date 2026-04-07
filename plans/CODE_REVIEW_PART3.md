# 🔍 АУДИТ КОДА (Часть 3): Slippage, Fees, Capital, Models, Exchange, Summary

## 10. SLIPPAGE MODEL — `arbitrage/system/slippage.py`

### 💪 Сильные стороны
1. Non-linear size pressure: depth_ratio^1.5 (строка 29)
2. walk_book() для VWAP fill price (строка 40) — volume-weighted average
3. walk_book_slippage_bps() для прямого расчёта slippage в bps
4. Multi-factor model: depth + volatility + latency (строка 32)

### ⚠️ Недостатки
1. Volatility factor слишком слабый: volatility_bps_factor=3.0 при типичной vol=0.001 даёт 0.003 bps — ничтожно
2. Нет time-of-day adjustment (Asia vs US session volatility differs)
3. depth_bps_factor=4.0 фиксирован — должен калиброваться по рынку

### 🚨 Критические ошибки: НЕТ

---

## 11. FEES — `arbitrage/system/fees.py`

### 💪 Сильные стороны
1. 4-level lookup: env-specific → env-global → fee tier tracker → default
2. Conservative default 5.0 bps (строка 17) — лучше переоценить чем недооценить
3. total_round_trip_fee_bps() (строка 95) — удобная utility
4. Поддержка maker rebates (отрицательные fees)
5. Symbol-specific fees через fee_bps_from_snapshot()

### ⚠️ Недостатки
1. Fee tier tracker import через try/except (строка 38) — может silent-fail
2. maker_fee_bps default=2.0 (строка 77): на многих биржах maker fee = 0-1 bps, 2 bps консервативно но OK
3. Нет caching — каждый вызов fee_bps() может делать import + tracker lookup

### 🚨 Критические ошибки: НЕТ

---

## 12. CAPITAL ALLOCATOR — `arbitrage/system/capital_allocator.py`

### 💪 Сильные стороны
1. Dynamic weighting по vol/funding/trend (строки 24-58)
2. Floor at 10% (строка 59) — никогда не даёт 0 allocation
3. Per-strategy hard cap (строка 63)
4. Single-strategy bypass (строка 65) — оптимизация для одной стратегии

### ⚠️ Недостатки
1. Volatility buckets слишком грубые — 5 ступеней. Continuous function лучше.
2. Funding boost 1.3x слишком агрессивен при >15 bps — может увеличить size при extreme funding (часто перед reversal)
3. Нет Kelly criterion или Sharpe-based sizing
4. Нет учёта текущей exposure — аллоцирует как будто exposure=0

### 🚨 Критические ошибки: НЕТ

---

## 13. MODELS — `arbitrage/system/models.py`

### 💪 Сильные стороны
1. Frozen dataclasses для immutable snapshots
2. StrategyId enum — type-safe strategy identification
3. OpenPosition.metadata — гибкое расширение без изменения schema

### ⚠️ Недостатки
1. MarketSnapshot.indicators: Dict[str, float] — нет type safety, easy to typo key names
2. TradeIntent.notional_usd default=0 — кто устанавливает? Не очевидно из модели
3. OpenPosition не frozen — может мутировать после создания

### 🚨 Критические ошибки: НЕТ

---

## 14. EXCHANGE REST CLIENTS — `okx_rest.py`, `bybit_rest.py`, `htx_rest.py`, `binance_rest.py`

### 💪 Сильные стороны
1. Connection pooling (limit=100, limit_per_host=30) — правильные параметры
2. DNS caching (ttl_dns_cache=300) — снижает latency на 5-10ms
3. Rate limiter integration — все запросы через acquire()
4. 429 retry with backoff — правильная обработка rate limits
5. HMAC signing для каждой биржи — корректные auth implementations
6. Timeout=5-10s — разумные значения

### ⚠️ Недостатки
1. 4 файла по 13-18k chars каждый — огромное дублирование. Base class + per-exchange overrides сократил бы на 60%
2. Session lifecycle: нет explicit close в ряде клиентов — connection leak при restart
3. Нет request ID tracking — при concurrent requests сложно debug
4. OKX testnet URL = production URL (строка 31 okx_rest.py): `https://www.okx.com` и для testnet тоже — OKX testnet сломан?

### 🚨 Критические ошибки: НЕТ для execution, но OKX testnet URL подозрителен

---

## 15. PRIVATE WS — `arbitrage/exchanges/private_ws.py`

### 💪 Сильные стороны
1. Real-time balance push — eliminates 5s REST polling lag
2. Order fill detection via WS — sub-ms vs 250ms REST poll
3. Position change tracking
4. Auto-reconnect с backoff
5. Thread-safe cached state
6. Seed balances from REST at startup

### ⚠️ Недостатки
1. Binance private WS not supported (строка 84 factory.py)
2. No heartbeat/keepalive tracking — может не заметить zombie connection
3. Large file (32k chars) — нужен split по exchange

### 🚨 Критические ошибки: НЕТ

---

## 16. POSITION SIZER — `arbitrage/system/position_sizer.py`

### 💪 Сильные стороны
1. 5-factor sizing: volatility, liquidity, spread, balance, risk
2. Balance constraint considers locked margin
3. Risk concentration adjustment

### ⚠️ Недостатки
1. Не используется в engine! Engine использует capital_allocator, а position_sizer — dead code
2. Discrete buckets вместо continuous functions
3. Нет integration с реальным equity curve

### 🚨 КРИТИЧЕСКАЯ: DEAD CODE — модуль не подключён к engine

---

## ИТОГОВАЯ СВОДКА КРИТИЧЕСКИХ ОШИБОК

| # | Модуль | Ошибка | Влияние | Приоритет |
|---|--------|--------|---------|-----------|
| 1 | engine.py:333 | Global exchange block при per-symbol failure | Блокирует все пары на бирже на 30 мин | P1 |
| 2 | execution.py:923 | Race condition в hedge verification (0.3s settle) | Двойной hedge = потеря денег | P1 |
| 3 | live_adapters.py:486 | Fill price fallback to mid-price | Искажение PnL на 5-20 bps | P2 |
| 4 | config.py | Dataclass defaults != from_env defaults | Тесты на одних параметрах, прод на других | P2 |
| 5 | position_sizer.py | Весь модуль — dead code | Не подключён к engine, wasted effort | P3 |

NOTE: Fee calculation в engine.py:438-450 была верифицирована и КОРРЕКТНА.
Двухступенчатая конвертация bps -> pct -> fraction: val/100 (bps->pct) затем sum/100 (pct->fraction) — математически правильно.

---

## 🎯 РЕКОМЕНДАЦИИ (приоритет)

### P0 — Исправить немедленно
1. **Per-(exchange,symbol) margin cooldown** вместо per-exchange global block (engine.py:333)
2. **Увеличить hedge_settle_seconds** до 1.0-2.0s или добавить retry loop в verification (execution.py:923)

### P1 — Исправить до продакшена
3. **Синхронизировать config defaults** между dataclass и from_env
4. **Fill price fallback** — логировать и помечать позиции где использовался mid-price

### P2 — Улучшить
6. **Подключить position_sizer** или удалить
7. **Кешировать os.getenv** в init, не в hot loop
8. **Добавить fill price logging** когда используется mid-price fallback
9. **Exchange adapter polymorphism** — убрать if/elif chains

### P3 — Nice to have
10. **Exponential backoff** в circuit breaker
11. **Per-exchange latency tracking** в risk engine
12. **Continuous sizing functions** вместо discrete buckets
13. **Sequence number validation** в WS orderbooks
