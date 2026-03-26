"""Test script to verify SHORT_SYMBOLS list."""
# -*- coding: utf-8 -*-

import sys
import io

# Fix Windows encoding issue
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from handlers.short_handlers import SHORT_SYMBOLS

# Count symbols
total_symbols = len(SHORT_SYMBOLS)

# Count by category (based on comments in source)
majors = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT"]
meme_coins = [s for s in SHORT_SYMBOLS if s in [
    "DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT",
    "FLOKIUSDT", "MEMEUSDT", "BOMEUSDT", "TRUMPUSDT", "MEWUSDT",
    "MOGSUSDT", "BRETTUSDT", "POPCATUSDT", "NEIROUSDT", "BABYUSDT",
    "MYRIAUSDT", "TURBOLUSDT", "RATUSDT", "SAMOYNUSDT", "MNTUSDT",
]]

# Check for duplicates
duplicates = [s for s in SHORT_SYMBOLS if SHORT_SYMBOLS.count(s) > 1]

print("=" * 60)
print("🔻 ШОРТ-БОТ: СТАТИСТИКА МОНЕТ")
print("=" * 60)
print(f"\n📊 Всего монет: {total_symbols}")
print(f"   • Majors: {len(majors)}")
print(f"   • Мем-коины: {len(meme_coins)}")
print(f"   • Другие: {total_symbols - len(majors) - len(meme_coins)}")

if duplicates:
    print(f"\n⚠️  Дубликаты найдены: {set(duplicates)}")
else:
    print("\n✅ Дубликатов не обнаружено")

print("\n📋 Полный список:")
print("-" * 60)
for i, symbol in enumerate(SHORT_SYMBOLS, 1):
    if i % 5 == 0:
        print(f"{i:2}. {symbol}")
    else:
        print(f"{i:2}. {symbol}", end="  ")
    if i % 5 == 0:
        print()

if len(SHORT_SYMBOLS) % 5 != 0:
    print()

print("\n" + "=" * 60)
print(f"✅ Готово к торговле! {total_symbols} пар для мониторинга")
print("=" * 60)
