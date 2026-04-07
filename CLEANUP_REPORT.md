# BRICKTRADE — ОТЧЕТ О ЧИСТКЕ ПРОЕКТА

**Дата анализа:** 2026-03-26
**Статус:** Найдены лишние файлы, готовы рекомендации по удалению

---

## 📊 ОБЩАЯ СТАТИСТИКА

```
Всего файлов в корне: ~40
Markdown документов: 18
Python скриптов: 22
Тестовых/проверочных скриптов: 10
Размер логов: ~360 KB
```

---

## 🗑️ ФАЙЛЫ К УДАЛЕНИЮ

### 🔴 КАТЕГОРИЯ 1: Тестовые/проверочные скрипты (МОЖНО УДАЛИТЬ)

```bash
# Одноразовые тестовые скрипты — больше не нужны
rm test_audit_fixes.py              # 45 KB
rm test_enhancements.py             # 11 KB
rm test_improvements.py             # 42 KB
rm test_htx_api.py                  # 4.1 KB
rm test_htx_balance.py              # 1.3 KB
rm test_short_symbols.py            # 1.7 KB

# Утилиты для проверки — использовались для отладки
rm check_htx_positions.py          # 1.8 KB
rm check_live.py                    # 2.9 KB
rm check_short_config.py            # 2.0 KB
rm close_htx_positions.py           # 2.1 KB

# Shell скрипт для запуска
rm launch_bybit.sh                  # 148 B
```

**Экономия:** ~114 KB
**Риск:** НИЗКИЙ (можно восстановить из git при необходимости)

---

### 🟡 КАТЕГОРИЯ 2: Дубликаты документации (МОЖНО ОБЪЕДИНИТЬ/УДАЛИТЬ)

#### Дубликаты Quick Start:
```bash
# У нас теперь есть QUICKSTART.md — это главный
rm QUICK_START_ARBITRAGE.md        # 8.9 KB - дубликат
```

#### Устаревшие отчеты:
```bash
# Старые отчеты — информация устарела
rm CORRECTNESS_REPORT.md           # 7.8 KB
rm OPTIMIZATION_RESULTS.md         # 8.1 KB
rm REPORT.md                        # 8.3 KB (старый)

# Устаревшие планы — заменены IMPLEMENTATION_REPORT.md
rm PLAN.md                          # 9.4 KB
rm TODO.md                          # 502 B
```

#### Дубликаты спецификаций:
```bash
# Дубликат ARBITRAGE_ENHANCEMENTS.md
rm ENHANCEMENTS_SUMMARY.txt         # 11 KB

# Старые specs
rm READY_FOR_TEST_LAUNCH_ANALYSIS.md # 11 KB
rm SWITCH_TO_TRADING_MODE.md        # 8.1 KB
rm trading_system_spec.md           # 3.9 KB
```

**Экономия:** ~77 KB
**Риск:** СРЕДНИЙ (содержат историческую информацию, но устарели)

---

### 🟢 КАТЕГОРИЯ 3: Временные/вспомогательные файлы (МОЖНО УДАЛИТЬ)

```bash
# Вспомогательный скрипт для терминала
rm claude_terminal.py               # 2.0 KB

# Summary файл — информация дублируется
rm SUMMARY_SHORT_EXPANSION.txt      # небольшой
```

**Экономия:** ~2 KB
**Риск:** НИЗКИЙ

---

### 🔵 КАТЕГОРИЯ 4: Старые логи (МОЖНО ПОЧИСТИТЬ)

```bash
# Удалить старые логи (если не нужны для анализа)
# ВНИМАНИЕ: Удаляйте только если уверены!

# Старые market intelligence логи
rm logs/market_intelligence_prev_snapshot.json  # 52 KB
rm logs/market_intelligence.jsonl               # 52 KB
rm logs/mi_structured.jsonl                     # 4 KB

# Логи за старые даты (если есть)
# rm -rf logs/2026-03-25/  # Пример
```

**Экономия:** ~108 KB + старые логи
**Риск:** СРЕДНИЙ (нельзя восстановить)

---

### ⚪ КАТЕГОРИЯ 5: System файлы (НЕ ТРОГАТЬ)

```bash
# Эти файлы НЕ удаляем — они системные
.DS_Store                           # macOS
__pycache__/                        # Python cache
*.pyc, *.pyo                        # Compiled Python
venv/                               # Virtual environment
```

**Действие:** Оставить как есть или добавить в .gitignore

---

## 📋 ФАЙЛЫ КОТОРЫЕ НУЖНО ОСТАВИТЬ

### ✅ Основная кодовая база:
```
main.py                    # ✅ Основной entry point
config.py                  # ✅ Конфигурация
database.py                # ✅ База данных
api.py                     # ✅ API для курсов валют
scheduler.py               # ✅ Планировщик уведомлений
healthcheck.py             # ✅ Healthcheck endpoint
keyboards.py               # ✅ Telegram клавиатуры
states.py                  # ✅ FSM состояния
utils.py                   # ✅ Утилиты
```

### ✅ Новые компоненты (реализованные сегодня):
```
dashboard.py               # ✅ Streamlit dashboard
run_calibration.py         # ✅ Калибратор
```

### ✅ Актуальная документация:
```
IMPLEMENTATION_REPORT.md   # ✅ Главный отчет (19 KB)
QUICKSTART.md              # ✅ Quick start guide (12 KB)
CLAUDE.md                  # ✅ Инструкции для Claude (13 KB)
README.md                  # ✅ Основное README (8.2 KB)
important_roadmap.txt      # ✅ Roadmap (14 KB)

SHORT_BOT_ANALYSIS.md      # ✅ Анализ short бота (20 KB)
STOCK_IMPROVEMENTS.md      # ✅ Рекомендации по акциям (18 KB)
SHORT_BOT_EXPANSION.md     # ✅ Expansion план (7.3 KB)

ARBITRAGE_ENHANCEMENTS.md  # ✅ Enhancements (13 KB)
MARKET_INTELLIGENCE_FULL_SPEC.md  # ✅ MI spec (6.2 KB)
```

### ✅ Директории:
```
arbitrage/                 # ✅ Арбитраж система
handlers/                  # ✅ Telegram handlers
stocks/                    # ✅ Акции система
tests/                     # ✅ Unit тесты
data/                      # ✅ Данные
logs/                      # ✅ Логи
venv/                      # ✅ Virtual environment
```

### ✅ Конфигурация:
```
_env                       # ✅ Конфигурация (основная)
.env                       # ✅ Конфигурация (копия)
requirements.txt           # ✅ Python dependencies
pytest.ini                 # ✅ Pytest config
```

---

## 🔧 РЕКОМЕНДУЕМЫЕ КОМАНДЫ

### Вариант 1: Безопасная чистка (только тестовые скрипты)

```bash
cd /Users/macbookairdmitri/BrickTrade

# Удалить тестовые скрипты
rm test_audit_fixes.py test_enhancements.py test_improvements.py \
   test_htx_api.py test_htx_balance.py test_short_symbols.py

# Удалить check/close утилиты
rm check_htx_positions.py check_live.py check_short_config.py \
   close_htx_positions.py launch_bybit.sh

# Удалить вспомогательные
rm claude_terminal.py SUMMARY_SHORT_EXPANSION.txt

# Экономия: ~116 KB, Риск: НИЗКИЙ
```

### Вариант 2: Агрессивная чистка (+ устаревшая документация)

```bash
cd /Users/macbookairdmitri/BrickTrade

# Вариант 1 + устаревшие документы
rm QUICK_START_ARBITRAGE.md CORRECTNESS_REPORT.md \
   OPTIMIZATION_RESULTS.md REPORT.md PLAN.md TODO.md \
   ENHANCEMENTS_SUMMARY.txt READY_FOR_TEST_LAUNCH_ANALYSIS.md \
   SWITCH_TO_TRADING_MODE.md trading_system_spec.md

# Экономия: ~193 KB, Риск: СРЕДНИЙ
```

### Вариант 3: Полная чистка (+ старые логи)

```bash
cd /Users/macbookairdmitri/BrickTrade

# Вариант 2 + старые market intelligence логи
rm logs/market_intelligence_prev_snapshot.json \
   logs/market_intelligence.jsonl \
   logs/mi_structured.jsonl

# Экономия: ~301 KB, Риск: СРЕДНИЙ
```

### Вариант 4: Создать архив перед удалением (РЕКОМЕНДУЕТСЯ)

```bash
cd /Users/macbookairdmitri/BrickTrade

# Создать backup перед чисткой
mkdir -p backup_2026-03-26
mv test_*.py check_*.py close_*.py launch_*.sh backup_2026-03-26/
mv QUICK_START_ARBITRAGE.md CORRECTNESS_REPORT.md \
   OPTIMIZATION_RESULTS.md REPORT.md PLAN.md TODO.md \
   ENHANCEMENTS_SUMMARY.txt READY_FOR_TEST_LAUNCH_ANALYSIS.md \
   SWITCH_TO_TRADING_MODE.md trading_system_spec.md \
   backup_2026-03-26/

# Заархивировать
tar -czf backup_2026-03-26.tar.gz backup_2026-03-26/

# Удалить папку
rm -rf backup_2026-03-26/

# Можно удалить архив через месяц если все ок
```

---

## 📦 .gitignore РЕКОМЕНДАЦИИ

Добавить в `.gitignore` (если его нет):

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.so
.Python
build/
develop-eggs/
dist/
downloads/
eggs/
.eggs/
lib/
lib64/
parts/
sdist/
var/
wheels/
*.egg-info/
.installed.cfg
*.egg

# Virtual Environment
venv/
ENV/
env/

# IDE
.vscode/
.idea/
*.swp
*.swo
*~

# OS
.DS_Store
Thumbs.db

# Database
*.db
*.sqlite
*.sqlite3

# Logs (опционально - можно логи держать в git)
# logs/
# *.log

# Sensitive
.env
_env
*.key
*.pem

# Temporary
backup_*/
*.tar.gz
temp_*.py
test_manual_*.py
```

---

## 📊 ИТОГОВАЯ СВОДКА

### Размеры категорий:

| Категория | Файлов | Размер | Риск | Рекомендация |
|-----------|--------|--------|------|--------------|
| Тестовые скрипты | 11 | ~114 KB | НИЗКИЙ | ✅ **УДАЛИТЬ** |
| Дубликаты docs | 10 | ~77 KB | СРЕДНИЙ | ⚠️ Архивировать |
| Вспомогательные | 2 | ~2 KB | НИЗКИЙ | ✅ **УДАЛИТЬ** |
| Старые логи | 3 | ~108 KB | СРЕДНИЙ | ⚠️ По желанию |
| **ИТОГО** | **26** | **~301 KB** | — | — |

### Рекомендация:

**Минимум:** Вариант 1 (только тестовые скрипты) — безопасно, экономит 116 KB

**Оптимально:** Вариант 4 (создать архив) — безопасно, можно восстановить при необходимости

**Максимум:** Вариант 3 (полная чистка) — если уверен что не понадобится

---

## ✅ ПОСЛЕ ЧИСТКИ

После удаления файлов:

1. **Запустить тесты:**
```bash
pytest tests/ -v
# Убедиться что все работает
```

2. **Проверить бота:**
```bash
ps aux | grep "python main.py"
curl http://localhost:8080/health
```

3. **Сделать commit:**
```bash
git add -A
git commit -m "chore: cleanup unused test scripts and outdated docs"
```

---

## 🎯 ФИНАЛЬНЫЕ РЕКОМЕНДАЦИИ

### ✅ Что ТОЧНО можно удалить:
- Все `test_*.py` в корне (не путать с `tests/` директорией!)
- Все `check_*.py` и `close_*.py` утилиты
- `launch_bybit.sh`
- `claude_terminal.py`
- `SUMMARY_SHORT_EXPANSION.txt`

### ⚠️ Что МОЖНО удалить (с осторожностью):
- Устаревшие markdown отчеты
- Дубликаты документации
- Старые market intelligence логи

### ❌ Что НЕ УДАЛЯТЬ:
- Все в `arbitrage/`, `handlers/`, `stocks/`, `tests/`
- `main.py`, `config.py`, `database.py`, и др. core файлы
- `dashboard.py`, `run_calibration.py` (новые компоненты)
- Актуальную документацию (IMPLEMENTATION_REPORT.md, QUICKSTART.md, etc.)
- `venv/`, `data/`, `logs/` (текущие)
- `.env`, `_env`, `requirements.txt`

---

**Подготовлено:** Claude (Sonnet 4.5)
**Дата:** 2026-03-26
**Проект:** BrickTrade v2.0
