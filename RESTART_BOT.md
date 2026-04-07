# Перезапуск бота с V2

## Что было исправлено

Проблема была в том, что бот запускается через Telegram handlers (`main.py` → `handlers/arbitrage_handlers_simple.py`), а не через `arbitrage/main.py`.

В handlers всегда создавался V1 execution engine, даже когда `EXEC_USE_V2=true` в .env.

**Исправления:**
- ✅ Добавлен импорт `AtomicExecutionEngineV2` и `PositionMonitor`
- ✅ Добавлена conditional логика выбора V1/V2 engine
- ✅ Добавлена инициализация Position Monitor
- ✅ Добавлена правильная остановка monitor при shutdown
- ✅ Добавлены поля `position_monitor` и `monitor_task` в `_EngineState`

## Как перезапустить

### Шаг 1: Остановить текущий бот

```bash
# Найти процесс Python
ps aux | grep "python.*main.py" | grep -v grep

# Остановить (используй PID из вывода выше)
kill <PID>

# ИЛИ если запущен через systemd/supervisor
sudo systemctl stop bricktrade
# или
sudo supervisorctl stop bricktrade
```

### Шаг 2: Проверить конфигурацию

```bash
# Убедись что V2 включен
grep "^EXEC_USE_V2" .env
# Должно быть: EXEC_USE_V2=true

# Убедись что monitor включен
grep "^MONITOR_ENABLED" .env
# Должно быть: MONITOR_ENABLED=true

# Проверь полную конфигурацию
python migrate_to_v2.py --check
```

### Шаг 3: Запустить заново

```bash
# Запуск (основной Telegram bot)
python main.py

# ИЛИ если используешь systemd
sudo systemctl start bricktrade

# ИЛИ supervisor
sudo supervisorctl start bricktrade
```

### Шаг 4: Проверить что V2 запустился

**В Telegram:**
1. Отправь боту `/start_arb`
2. Бот начнёт торговлю

**В логах:**

```bash
# Смотри логи в реальном времени
tail -f logs/*/arbitrage.log | grep -E "EXEC_V|POSITION_MONITOR"
```

**Должен увидеть:**
```
[EXEC_V2] Using Execution V2 (Atomic Two-Phase System)
[POSITION_MONITOR] Initializing monitor (interval=30s, auto_close=True)
[POSITION_MONITOR] Background monitor started
```

**НЕ должен видеть:**
```
[EXEC_V1] Using Execution V1 (Legacy System)  ← Если видишь это - V2 НЕ активирован!
```

### Шаг 5: Проверить первую сделку

После запуска, когда появится первая сделка, проверь логи:

```bash
tail -200 logs/*/arbitrage.log | grep -E "Placing.*order|EXEC_V2"
```

**Правильное поведение V2:**
```
[EXEC_V2_START] BTCUSDT: notional=5.00 long=okx short=htx
[PREFLIGHT] BTCUSDT: balance_okx=9.85 balance_htx=4.74
[EXEC_V2_PREFLIGHT_OK] BTCUSDT: safe_notional=3.00
[ENTRY] BTCUSDT: placing both legs simultaneously
Placing OKX order: BTCUSDT sell ← Первая биржа
Placing HTX order: BTCUSDT buy  ← Вторая биржа (НЕ OKX!)
[EXEC_V2_SUCCESS] BTCUSDT: properly hedged
```

**Неправильное (старая проблема):**
```
Placing OKX order: BTCUSDT buy
Placing OKX order: BTCUSDT sell  ← Обе на OKX - это hedge, не арбитраж!
```

## Troubleshooting

### Проблема: Всё ещё видишь ордера на одной бирже

**Решение:**
1. Проверь что бот действительно перезапущен с новым кодом:
   ```bash
   grep "EXEC_V2\|EXEC_V1" logs/*/arbitrage.log | tail -1
   ```
   Если видишь `[EXEC_V1]` или ничего - код не обновился.

2. Убедись что используешь правильный Python:
   ```bash
   which python
   python --version
   ```

3. Перезапусти бота принудительно:
   ```bash
   killall -9 python  # ОСТОРОЖНО: убьёт все Python процессы!
   python main.py
   ```

### Проблема: Логи не показывают [EXEC_V2]

**Решение:**
1. Проверь .env:
   ```bash
   cat .env | grep EXEC_USE_V2
   ```

2. Если `EXEC_USE_V2=false`, включи:
   ```bash
   python migrate_to_v2.py --enable
   ```

3. Перезапусти бота.

### Проблема: Import error при запуске

**Ошибка:**
```
ImportError: cannot import name 'AtomicExecutionEngineV2'
```

**Решение:**
```bash
# Проверь что файл существует
ls -la arbitrage/system/execution_v2.py

# Если нет - скачай последнюю версию кода
git pull
# или пересоздай файл
```

## Мониторинг после запуска

### В течение первых 30 минут следи за:

1. **Логами execution:**
   ```bash
   tail -f logs/*/arbitrage.log | grep EXEC_V2
   ```

2. **Position Monitor:**
   ```bash
   tail -f logs/*/arbitrage.log | grep POSITION_MONITOR
   ```

3. **Критическими ошибками:**
   ```bash
   tail -f logs/*/arbitrage.log | grep -E "CRITICAL|ORPHAN|HEDGE_INCOMPLETE"
   ```

### Нормальное поведение V2:

```
[EXEC_V2_START] - каждая новая сделка
[EXEC_V2_SUCCESS] - успешные сделки
[POSITION_MONITOR] checks_performed=X orphans=0 - каждые 30 сек
```

### Тревожные сигналы:

```
[ORPHAN_DETECTED] ← НЕМЕДЛЕННО проверь!
[EXEC_V2_HEDGE_INCOMPLETE] ← КРИТИЧНО!
[EMERGENCY_CLOSE] ← Monitor закрывает orphan
```

---

**После успешного запуска V2 ожидай:**
- ✅ Сделки открываются на РАЗНЫХ биржах (OKX ↔ HTX, OKX ↔ Bybit)
- ✅ Success rate ~95%
- ✅ Нет orphan позиций
- ✅ Hedge работает корректно

**Удачи!** 🚀
