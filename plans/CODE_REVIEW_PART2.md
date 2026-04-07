# 🔍 АУДИТ КОДА (Часть 2): Risk, Adapters, Config, Infra

## 4. RISK ENGINE — `arbitrage/system/risk.py`

### 💪 Сильные стороны
1. 7 уровней проверок: kill switch → latency → leverage → slippage → stale data → drawdown → exposure
2. Pairwise imbalance (строка 64) — проверяет только 2 биржи в trade
3. Timestamp normalization (строка 51) — автодетект ms/s
4. Non-permanent daily drawdown kill switch (строка 79)

### ⚠️ Недостатки
1. Latency breach — глобальный счётчик (строка 33), не per-exchange
2. Variable shadowing: `snapshot` параметр перезатирается (строка 85)
3. Нет rate-of-change check (быстрая потеря за короткое время)

### 🚨 Критические ошибки: НЕТ — модуль безопасен

---

## 5. LIVE ADAPTERS — `arbitrage/system/live_adapters.py`

### 💪 Сильные стороны
1. WS-first, REST-fallback (строки 97-111, 776-827) — правильная latency hierarchy
2. Private WS balance push (строка 176) — eliminates REST polling
3. Per-symbol depth throttling (строка 147)
4. WS fill detection + REST fallback (строка 776)
5. Safety buffer + reserve (строки 310-320) — двухуровневая защита margin
6. Balance cache invalidation после каждого ордера (строка 829)
7. Min notional override для маленьких аккаунтов (строка 396)
8. Price rounding по tick size с floating-point fix (строка 923)

### ⚠️ Недостатки
1. Дублирование exchange-specific if/elif в 5+ методах — нужен polymorphism
2. HTX integer volume casting теряет дробную часть (строка 365)
3. min(timeout_ms, timeout_ms) (строка 795) — бессмысленный min
4. OCO только для OKX (строка 584) без fallback

### 🚨 КРИТИЧЕСКАЯ: Fill price fallback to mid (строки 486-489)
Если биржа не вернула fill price — используется mid-price. Для IOC market это ±5-20 bps ошибка. Искажает PnL, маскирует убытки.

---

## 6. CIRCUIT BREAKER — `arbitrage/system/circuit_breaker.py`

### 💪 Сильные стороны
1. Severity classification: transient/fatal/normal (строки 33-50)
2. Pattern matching для автоклассификации (строки 33-35)
3. Простой и надёжный — мало кода, мало багов

### ⚠️ Недостатки
1. Нет exponential backoff — всегда 600s
2. Float error count (0.5 за transient) — accumulation issue
3. Per-exchange, не per-symbol — одна плохая пара блокирует все

### 🚨 Критические ошибки: НЕТ

---

## 7. WS ORDERBOOKS — `arbitrage/system/ws_orderbooks.py`

### 💪 Сильные стороны
1. Crossed book detection (строка 91) — отклоняет bid >= ask
2. Server timestamp preference с sanity check (строка 97-109)
3. Watchdog task (строка 53) — мониторит здоровье WS
4. Restart decay (строка 137) — снижает счётчик при стабильности
5. Max restart limit (строка 142) — не зацикливается

### ⚠️ Недостатки
1. _stale_after_sec = 10.0 — слишком широко для HFT arb (3-5s лучше)
2. Нет sequence number validation — пропущенные сообщения не детектятся
3. Depth хранится без лимита по symbols (max_depth_symbols=200 есть но не enforced)

### 🚨 Критические ошибки: НЕТ

---

## 8. CONFIG — `arbitrage/system/config.py`

### 💪 Сильные стороны
1. Frozen dataclasses (строка 36) — immutable конфигурация
2. Validation (строка 226) — проверяет ranges и relationships
3. Safe defaults: dry_run=True по умолчанию (строка 178)
4. Multi-env fallback: _first_env() (строка 28)
5. Complete from_env() — все параметры конфигурируемы

### ⚠️ Недостатки
1. Defaults в dataclass != defaults в from_env():
   - RiskConfig default: max_open_positions=3, from_env: 20
   - RiskConfig default: max_orderbook_age_sec=10.0, from_env: 30.0
   - RiskConfig default: api_latency_limit_ms=400, from_env: 8000
   Это ОПАСНО — тесты используют dataclass defaults, продакшен from_env.
2. Нет validation ranges для strategy parameters
3. Нет warning при missing credentials в dry_run mode

### 🚨 Критические ошибки: НЕТ, но dataclass/env рассинхрон опасен

---

## 9. STATE — `arbitrage/system/state.py`

### 💪 Сильные стороны
1. File persistence с JSON (строки 27-58) — positions survive restart
2. Async lock everywhere (строка 82) — thread-safe
3. History trimming (строка 119) — _MAX_HISTORY_LEN предотвращает OOM
4. UTC daily reset (строка 91) — timezone-safe
5. Kill switch cooldown (строка 75) — auto-recovery через 10 мин

### ⚠️ Недостатки
1. Synchronous file I/O в _persist_positions (aiofiles не используется для записи)
2. Нет backup/rotation — один файл, crash during write = data loss
3. metadata фильтрация (строка 40): nested dict/list теряются при serialize

### 🚨 Критические ошибки: НЕТ

---

## 10. SLIPPAGE MODEL — `arbitrage/system/slippage.py`

### 💪 Сильные стороны
1. Non-linear size pressure (строка 29): depth_ratio^1.5 — ре