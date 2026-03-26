# 🚀 Arbitrage Bot: Major Enhancements

## Обновление от 2026-03-25

Добавлено 4 основных улучшения для максимизации прибыльности и эффективности арбитражной торговли.

---

## 1. ⚡ Maker/Taker Hybrid Execution (FEE OPTIMIZER)

### Описание
Экономия **50-80% на комиссиях** путём размещения одной ноги как maker order (post-only).

### Преимущества
- **Maker fees**: 0-2 bps (часто негативные = rebate)
- **Taker fees**: 4-6 bps
- **Savings**: 50-80% на одной ноге = 25-40% на всей сделке

### Как работает
1. **Первая нога** (надёжная биржа): Maker order с post-only
2. **Вторая нога** (параллельно): Taker IOC как обычно
3. **Retry logic**: 2-3 попытки maker, затем fallback на taker
4. **Dynamic calibration**: Автоматически подстраивает timeout и price offset

### Конфигурация (.env)
```bash
# Enable maker/taker mode
EXEC_USE_MAKER_TAKER=true

# Maker order parameters
EXEC_MAKER_TIMEOUT_MS=2000          # Wait time for maker fill
EXEC_MAKER_MAX_RETRIES=2            # Retry attempts before taker fallback
EXEC_MAKER_PRICE_OFFSET_BPS=0.5    # Price offset inside spread (bps)
```

### API
```python
from arbitrage.system.fee_optimizer import get_fee_optimizer

optimizer = get_fee_optimizer()

# Check if should use maker
should_use = optimizer.should_use_maker(
    exchange="okx",
    volatility=0.015,  # 1.5%
    spread_bps=15.0,
)

# Record result
optimizer.record_maker_attempt(
    exchange="okx",
    filled=True,
    wait_ms=1200,
)

# Get stats
summary = optimizer.get_summary()
# {"okx": {"fill_rate_pct": 78.5, "total_saved_usd": 24.50, ...}}
```

### Пример savings
- **Trade size**: $10 USDT
- **Taker fee**: 5 bps = $0.005
- **Maker fee**: 0 bps = $0
- **Saved per leg**: $0.005
- **10 trades/day**: $0.05/day = **$18/year** (только на $10 positions!)

---

## 2. 💰 Funding Rate Arbitrage (ОТДЕЛЬНАЯ СТРАТЕГИЯ)

### Описание
Новый режим для захвата funding rate дифференциалов. Удержание позиций 8 часов для получения funding payments.

### Характеристики
- **Профиль**: Long hold (8h), low frequency, stable returns
- **Entry**: Funding differential ≥ 0.05% (5 bps)
- **Exit**: После получения funding payment
- **Risk**: Convergence risk (цены разъезжаются)
- **Return**: Funding diff - fees - slippage

### Как работает
1. **Scan funding rates** на всех биржах
2. **Find pair** с максимальным дифференциалом
3. **Open position**: Long на низком funding, Short на высоком
4. **Hold ~8 hours** до следующего funding time
5. **Collect funding** payment
6. **Close position** сразу после funding

### Конфигурация (.env)
```bash
# Enable funding arbitrage strategy
ENABLED_STRATEGIES=futures_cross_exchange,funding_arbitrage

# Funding arbitrage parameters
FUNDING_MIN_DIFF_PCT=0.05           # Min differential to enter (0.05%)
FUNDING_MAX_HOLD_HOURS=8.5          # Max hold time
FUNDING_MIN_HOLD_HOURS=7.0          # Min hold before early exit
FUNDING_ENTRY_WINDOW_HOURS=1.0     # Only enter if funding within 1h
FUNDING_MAX_SPREAD_COST_BPS=15.0   # Max spread cost on entry
FUNDING_TARGET_PROFIT_BPS=10.0     # Target profit after costs
FUNDING_MAX_CONVERGENCE_RISK_BPS=30.0  # Max adverse price move
```

### API
```python
from arbitrage.system.strategies.funding_arbitrage import FundingArbitrageStrategy, FundingConfig

config = FundingConfig(
    min_funding_diff_pct=0.05,
    max_hold_hours=8.5,
    target_profit_bps=10.0,
)

strategy = FundingArbitrageStrategy(config)

# Scan opportunities
opportunities = await strategy.scan_opportunities(
    symbols=["BTCUSDT", "ETHUSDT"],
    funding_data={"okx": {"BTCUSDT": 0.08}, "htx": {"BTCUSDT": 0.02}},
    spread_data={"BTCUSDT": 10.0},
    next_funding_times={"okx": datetime.now() + timedelta(hours=0.5)},
)

# Create trade intent
if opportunities:
    intent = strategy.create_intent(
        opp=opportunities[0],
        notional_usd=10.0,
        current_prices={"okx": 50000, "htx": 50010},
    )
```

### Пример расчёта
- **BTCUSDT**: OKX funding = 0.08%, HTX funding = 0.02%
- **Differential**: 0.06% (6 bps)
- **Entry spread**: 10 bps
- **Exit spread**: 10 bps
- **Fees**: 10 bps
- **Profit**: 6 - 10 - 10 - 10 = **-24 bps** ❌ (не profitable)

Но если differential = 0.50% (50 bps):
- **Profit**: 50 - 10 - 10 - 10 = **+20 bps** ✅ (profitable!)

---

## 3. 📊 Dynamic Position Sizing

### Описание
Автоматическая подстройка размера позиции на основе рыночных условий.

### Факторы влияния
1. **Volatility**: Высокая волатильность → меньший size
2. **Liquidity**: Тонкие стаканы → меньший size
3. **Spread quality**: Широкие спреды → больший size (больше буфер)
4. **Available balance**: Ограничение по балансу
5. **Portfolio risk**: Больше позиций → меньше per position

### Конфигурация (.env)
```bash
# Dynamic position sizing
POSITION_BASE_NOTIONAL_USD=10.0     # Base size
POSITION_MAX_NOTIONAL_USD=100.0     # Maximum size
POSITION_MIN_NOTIONAL_USD=5.0       # Minimum size

# Risk-based sizing
POSITION_RISK_PER_TRADE_PCT=0.02    # 2% risk per trade
POSITION_USE_KELLY=true             # Use Kelly Criterion
POSITION_KELLY_FRACTION=0.25        # Use 25% of full Kelly (safer)
```

### API
```python
from arbitrage.system.position_sizer import DynamicPositionSizer

sizer = DynamicPositionSizer(
    base_notional_usd=10.0,
    max_notional_usd=100.0,
    min_notional_usd=5.0,
)

# Calculate optimal size
factors = sizer.calculate_size(
    symbol="BTCUSDT",
    long_exchange="okx",
    short_exchange="htx",
    volatility=0.015,  # 1.5%
    book_depth_usd=10000,  # $10k depth
    spread_bps=15.0,
    balances={"okx": 50.0, "htx": 50.0},
    open_positions=1,
    max_positions=3,
)

print(f"Final size: ${factors.final_notional}")
# Final size: $12.50 (adjusted from base $10)

# Kelly Criterion sizing
kelly_size = sizer.calculate_kelly_size(
    win_rate=0.65,       # 65% winrate
    avg_win_pct=0.02,    # 2% avg win
    avg_loss_pct=0.01,   # 1% avg loss
    current_equity=100.0,
)
print(f"Kelly size: ${kelly_size}")
```

### Adjustment examples
| Condition | Factor | Size |
|-----------|--------|------|
| Low volatility (0.5%) | 1.3x | $13 |
| Normal volatility (1.5%) | 1.0x | $10 |
| High volatility (4%) | 0.6x | $6 |
| Deep book (10x size) | 1.3x | $13 |
| Thin book (2x size) | 0.7x | $7 |
| Wide spread (30 bps) | 1.2x | $12 |
| Tight spread (5 bps) | 0.7x | $7 |

---

## 4. 🏆 Fee Tier Optimization

### Описание
Отслеживание VIP уровня на биржах и адаптация стратегии под текущие комиссии.

### Возможности
1. **Track fee tier**: Автоматически определяет текущий VIP level
2. **Calculate breakeven**: Вычисляет min spread для прибыльности
3. **Volume pursuit**: Рекомендует, стоит ли гнаться за volume для upgrade
4. **Strategy adaptation**: Подстраивает min_spread под текущие fees

### Конфигурация (.env)
```bash
# Fee tier tracking
FEE_TIER_TRACKING_ENABLED=true
FEE_TIER_UPDATE_INTERVAL_HOURS=24   # Check tier daily
```

### API
```python
from arbitrage.system.fee_tier_tracker import get_fee_tier_tracker

tracker = get_fee_tier_tracker()

# Update tier based on 30-day volume
tier = await tracker.update_tier(
    exchange="okx",
    volume_30d_usd=1_500_000,  # $1.5M volume
)

print(f"OKX Tier: {tier.tier_level} (maker={tier.maker_fee_bps} bps)")
# OKX Tier: 1 (maker=1.5 bps)

# Calculate breakeven spread
breakeven = tracker.calculate_breakeven_spread(
    long_exchange="okx",
    short_exchange="htx",
    use_maker_on_long=True,
)
print(f"Breakeven: {breakeven} bps")
# Breakeven: 8.5 bps

# Should we pursue higher volume?
should, reason = tracker.should_pursue_volume(
    exchange="okx",
    estimated_trades_per_day=50,
    avg_trade_size_usd=10.0,
)
print(f"Pursue volume: {should} ({reason})")
# Pursue volume: True (reach_in_28d_save_$450/yr)
```

### Fee tier examples (OKX)
| Level | Volume (30d) | Maker | Taker | Breakeven (both sides) |
|-------|--------------|-------|-------|------------------------|
| 0 | $0 | 2.0 bps | 5.0 bps | 14 bps |
| 1 | $500k | 1.5 bps | 4.0 bps | 11 bps |
| 2 | $2M | 1.0 bps | 3.5 bps | 9 bps |
| 3 | $10M | 0.5 bps | 3.0 bps | 7 bps |
| 4 | $50M | 0.0 bps | 2.5 bps | 5 bps |
| 5 | $100M | -0.5 bps (rebate!) | 2.0 bps | 3 bps |

---

## 🎯 Совокупное влияние на прибыльность

### Base scenario (без улучшений)
- Trade size: $10
- Spread: 15 bps
- Fees: 10 bps (5 bps × 2 legs)
- Net profit: **5 bps = $0.005**

### With enhancements
1. **Maker/taker**: Save 3 bps → profit = 8 bps
2. **Dynamic sizing**: Size up to $12 in good conditions → profit = $0.0096
3. **Fee tier**: Reach tier 2 → fees = 7 bps → profit = $0.0108
4. **Funding arb**: Additional 10-30 bps per 8h hold

**Total improvement: +60-100% profit per trade!**

---

## 📁 Новые файлы

```
arbitrage/system/
├── fee_optimizer.py              # Maker/taker execution optimizer
├── fee_tier_tracker.py           # VIP tier tracking
├── position_sizer.py             # Dynamic position sizing
└── strategies/
    └── funding_arbitrage.py      # Funding rate arbitrage strategy
```

---

## 🚦 Включение функций

### 1. Включить maker/taker в .env
```bash
EXEC_USE_MAKER_TAKER=true
EXEC_MAKER_TIMEOUT_MS=2000
EXEC_MAKER_MAX_RETRIES=2
EXEC_MAKER_PRICE_OFFSET_BPS=0.5
```

### 2. Добавить funding arbitrage
```bash
ENABLED_STRATEGIES=futures_cross_exchange,funding_arbitrage
FUNDING_MIN_DIFF_PCT=0.05
FUNDING_TARGET_PROFIT_BPS=10.0
```

### 3. Включить dynamic sizing
```bash
POSITION_BASE_NOTIONAL_USD=10.0
POSITION_USE_KELLY=true
```

### 4. Включить fee tier tracking
```bash
FEE_TIER_TRACKING_ENABLED=true
```

---

## 📈 Мониторинг и метрики

### Fee optimizer stats
```python
summary = get_fee_optimizer().get_summary()
# {
#   "okx": {
#     "maker_attempts": 150,
#     "maker_fills": 120,
#     "fill_rate_pct": 80.0,
#     "total_saved_usd": 12.50,
#   }
# }
```

### Position sizing stats
```python
# Check current sizing factors
factors = sizer.calculate_size(...)
print(f"Vol adj: {factors.volatility_adj}x")
print(f"Liq adj: {factors.liquidity_adj}x")
print(f"Final: ${factors.final_notional}")
```

### Fee tier status
```python
summary = get_fee_tier_tracker().get_summary()
# {
#   "okx": {
#     "tier_level": 2,
#     "maker_fee_bps": 1.0,
#     "taker_fee_bps": 3.5,
#     "volume_30d_usd": 2_500_000,
#     "next_tier_volume": 10_000_000,
#   }
# }
```

---

## ⚠️ Важные замечания

### Maker/Taker
- ⚠️ **Не используйте в высокой волатильности** (> 3%)
- ⚠️ **Не используйте при тонких спредах** (< 10 bps)
- ✅ Лучше всего работает на ликвидных парах с умеренной волатильностью

### Funding Arbitrage
- ⚠️ **Risk**: Цены могут разъехаться за 8 часов
- ⚠️ **Capital lock**: Позиция заморожена на 8 часов
- ✅ Подходит для стабильных периодов и широких funding дифференциалов

### Dynamic Sizing
- ⚠️ Может уменьшить size до min при плохих условиях
- ⚠️ Kelly Criterion агрессивен — используйте fractional Kelly (0.25-0.50)
- ✅ Защищает от overtrading в неблагоприятных условиях

### Fee Tier
- ⚠️ Volume требования могут быть недостижимы для small accounts
- ✅ Даже tier 1-2 дают значительную экономию

---

## 🎉 Резюме

**4 основных улучшения** для максимизации прибыльности:

1. ⚡ **Maker/Taker**: -50-80% fees на одной ноге
2. 💰 **Funding Arb**: Новый источник дохода (8h holds)
3. 📊 **Dynamic Sizing**: Оптимальный size под условия
4. 🏆 **Fee Tier**: Адаптация под VIP levels

**Expected impact**: +60-100% увеличение profit per trade!

---

Дата: 2026-03-25
Версия: 3.0 (Enhanced)
