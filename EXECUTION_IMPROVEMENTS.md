# Улучшения логики исполнения второй ноги

## Проблема (старая логика)
В MERLUSDT сделке (09:58):
- Попытка открыть $6.54 на HTX при балансе $4.74
- 3 попытки с ОДИНАКОВЫМ размером
- Все 3: "Insufficient margin available"
- → Hedge первой ноги → убыток в комиссиях

## Решение (новая логика)

### 1. Preflight Balance Check (строки 225-248)
**ДО** размещения второй ноги:
- Проверяем баланс второй биржи
- Если баланс < notional * 0.15 (15% margin requirement):
  - Уменьшаем notional до 80% доступного баланса
  - Если < $1 → сразу переходим к hedge (не тратим время на заведомо провальные попытки)

### 2. Adaptive Notional Reduction (строки 332-347)
**При получении margin error:**
- 1-я попытка FAILED → уменьшаем notional на 30%
- 2-я попытка с МЕНЬШИМ размером
- Если уменьшенный notional < $1 → выходим из retry loop → hedge

### 3. Earlier Market Order Fallback (строки 258-271)
**Старо:** IOC → IOC → Market (последняя попытка)
**Новое:** IOC → Market → Market
- После 1-й неудачной IOC сразу переходим на market order
- Больше шансов исполниться

### 4. Better Error Handling (строки 293-299)
- Обработка ошибок cancel
- Более детальное логирование каждой попытки
- Логируем размер и тип ордера для каждой попытки

## Результат

### Старая логика:
```
Attempt 1: IOC $6.54 → "Insufficient margin" → sleep 0.5s
Attempt 2: IOC $6.54 → "Insufficient margin" → sleep 1s  
Attempt 3: Market $6.54 → "Insufficient margin"
→ HEDGE → -$0.10 в комиссиях
```

### Новая логика:
```
Preflight: balance=$4.74, need=$0.98 (15% of $6.54) → OK но мало
Reduce: $6.54 → $3.98 (80% of $4.74 / 0.15)

Attempt 1: IOC $3.98 → placed, checking fill...
  - If filled → SUCCESS, no hedge needed!
  - If not filled → try market

Attempt 2: Market $3.98 → placed, checking fill...
  - If still margin error → reduce to $2.79 (70% of $3.98)
  
Attempt 3: Market $2.79 → placed
  - Higher chance of success with smaller size
  - Only hedge if ALL 3 attempts failed
```

## Ключевые улучшения:
1. ✅ Preflight проверка → не тратим время на заведомо провальные сделки
2. ✅ Адаптивное уменьшение размера → больше шансов исполниться
3. ✅ Ранний переход на market → лучший fill rate
4. ✅ Умный hedge → только если действительно все провалилось
