"""Check short bot configuration."""
# -*- coding: utf-8 -*-

import sys
import io

# Fix Windows encoding
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from handlers.short_handlers import SHORT_SYMBOLS, EXEC_CONFIG

print("=" * 60)
print("ШОРТ-БОТ: КОНФИГУРАЦИЯ")
print("=" * 60)
print(f"\n📊 МОНЕТЫ:")
print(f"   Всего пар: {len(SHORT_SYMBOLS)}")
print(f"   Было: 20")
print(f"   Стало: {len(SHORT_SYMBOLS)}")
print(f"   Прирост: +{len(SHORT_SYMBOLS) - 20} пар ({(len(SHORT_SYMBOLS) - 20) / 20 * 100:.0f}%)")

print(f"\n⚙️  НАСТРОЙКИ ТОРГОВЛИ:")
print(f"   Торговля: {'✅ ВКЛЮЧЕНА' if EXEC_CONFIG['enabled'] else '❌ ВЫКЛЮЧЕНА'}")
print(f"   Размер позиции: {EXEC_CONFIG['order_size_usdt']} USDT")
print(f"   Плечо: {EXEC_CONFIG['leverage']}x")
print(f"   Max позиций: {EXEC_CONFIG['max_positions']}")
print(f"   Stop-Loss: {EXEC_CONFIG['sl_pct']}%")
print(f"   Take-Profit: {EXEC_CONFIG['tp_pct']}%")
print(f"   Минимальный score: {EXEC_CONFIG['min_score']}/7")
print(f"   Авто-вход: {'✅ ВКЛ' if EXEC_CONFIG['auto_execute'] else '❌ Ручной'}")

print(f"\n📈 КАТЕГОРИИ:")
majors = 5
meme = 20
print(f"   Majors: {majors}")
print(f"   Мем-коины: {meme}")
print(f"   Другие: {len(SHORT_SYMBOLS) - majors - meme}")

print(f"\n🎯 РИСК/ДОХОДНОСТЬ:")
notional = EXEC_CONFIG['order_size_usdt'] * EXEC_CONFIG['leverage']
sl_loss = notional * EXEC_CONFIG['sl_pct'] / 100
tp_profit = notional * EXEC_CONFIG['tp_pct'] / 100
print(f"   Номинал позиции: {notional:.2f} USDT")
print(f"   Риск при SL: -{sl_loss:.2f} USDT")
print(f"   Прибыль при TP: +{tp_profit:.2f} USDT")
print(f"   Risk/Reward: 1:{tp_profit/sl_loss:.2f}")

print("\n" + "=" * 60)
print("✅ Конфигурация загружена успешно!")
print("=" * 60)
