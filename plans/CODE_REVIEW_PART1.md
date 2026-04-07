# АУДИТ КОДА (Часть 1): Engine, Execution, Strategy

## 1. ENGINE — arbitrage/system/engine.py

### Сильные стороны
1. Orphan position scanning (строка 802) — сканирует незатреканные позиции на биржах при старте
2. Balance sync с reset_baselines (строка 678) — обнуляет drawdown-базы после синхронизации
3. Underfunded exchange filtering (строка 711) — предотвращает hedge-back на биржу без денег
4. Balance instability detection (строка 726) — детектит скачки баланса >80%
5. Phantom position cleanup (строка 573) — проверяет реальность позиций на биржах
6. Emergency close all (строка 900) — permanent kill switch при unverified hedge
7. Funding PnL с holding period (строка 455) — корректный учёт funding за время удержания
8. Comprehensive monitoring — каждое действие эмитит событие
9. Fee calculation (строки 438-450) — корректная двухступенчатая конвертация bps->pct->fraction

### Недостатки
1. os.getenv() в горячем цикле (строки 234, 332, 754) — читает env при каждом цикле
2. _select_strategy_ids() (строка 674) — no-op, ничего не делает
3. run_cycle() — 280+ строк без декомпозиции, сложно тестировать
4. f-string logging — overhead при отключённом уровне логирования

### КРИТИЧЕСКАЯ #1: Глобальный exchange block (строка 333)
second_leg_failed на одном символе блокирует ВСЮ биржу на 30 мин.
Если BTC/USDT не прошёл на OKX, ETH/USDT тоже будет заблокирован.
Нужен per-(exchange,symbol) cooldown.

---

## 2. EXECUTION — arbitrage/system/execution.py

### Сильные стороны
1. Per-symbol lock (строка 41) — защита от duplicate entries
2. Preflight margin check (строка 84) — проверка баланса на обеих биржах
3. Orphan position preflight (строка 116) — untracked contracts check
4. Self-trade rejection (строка 67) — wash trade prevention
5. Hedge verification через open_contracts() (строка 891)
6. Maker-taker hybrid (строка 809) — post_only -> retry -> taker fallback
7. Size matching (строка 200) — вторая нога = first_effective
8. Exit leg recovery (строка 428)

### Недостатки
1. Hardcoded reliability rank (строка 683) дублируется
2. Multi-leg ExecutionReport — fill prices всегда 0
3. min(timeout_ms, timeout_ms) (строка 795) — бессмысленный min

### КРИТИЧЕСКАЯ: Race condition в hedge verification (строка 923)
sleep(0.3s) -> open_contracts(). HTX может не успеть обработать ордер —
false negative -> повторный hedge -> двойной hedge = потеря денег.

---

## 3. STRATEGY — arbitrage/system/strategies/futures_cross_exchange.py

### Сильные стороны
1. Walk-the-book pricing (строка 141) — реальная цена по глубине стакана
2. Full round-trip fee accounting (строка 186) — entry + exit fees
3. Maker-taker fee awareness (строка 173)
4. itertools.combinations (строка 91) — все пары бирж
5. Bidirectional dedup (строка 105)
6. Per-direction cooldown (строка 208)
7. Funding rate arb (строка 264) — отдельная стратегия
8. Confidence scaling (строка 221) — пропорционален превышению

### Недостатки
1. Default min_spread_pct=0.08% слишком низкий для ~20bps round-trip
2. Funding arb: 1-period diff vs full round-trip (нужен accumulation)
3. Нет depth data -> пропускает проверку (строка 372)
4. Hardcoded 000 depth min (строка 388)
5. est_notional = balance * 0.05 — грубая оценка (строка 144)

### КРИТИЧЕСКАЯ: Funding PnL sign
В engine.py строка 467-470: funding_pnl = income_short - cost_long.
Правильно только когда short на high-funding exchange.
Нет проверки направленности.
