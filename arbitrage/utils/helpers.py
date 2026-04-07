"""
Вспомогательные функции для арбитражного бота
"""
import time
import hmac
import hashlib
import base64
from typing import Dict, Any, Optional
from decimal import Decimal, ROUND_DOWN


def get_timestamp_ms() -> int:
    """Получить текущий timestamp в миллисекундах"""
    return int(time.time() * 1000)


def get_timestamp_sec() -> int:
    """Получить текущий timestamp в секундах"""
    return int(time.time())


def sign_okx(timestamp: str, method: str, request_path: str, body: str, secret: str) -> str:
    """
    Создать подпись для OKX API

    Args:
        timestamp: ISO 8601 timestamp
        method: HTTP метод (GET, POST, etc)
        request_path: Путь запроса с параметрами
        body: Тело запроса (пустая строка для GET)
        secret: API secret

    Returns:
        Base64 encoded signature
    """
    message = timestamp + method + request_path + body
    mac = hmac.new(
        bytes(secret, encoding='utf8'),
        bytes(message, encoding='utf-8'),
        digestmod=hashlib.sha256
    )
    return base64.b64encode(mac.digest()).decode()


def calculate_spread(bid_price: float, ask_price: float) -> float:
    """
    Рассчитать спред в процентах.

    For cross-exchange arbitrage: spread = (best_bid_elsewhere - best_ask_here) / best_ask_here * 100
    Positive = profitable (bid > ask = arbitrage opportunity exists).
    Negative = not profitable (bid < ask = normal market condition).

    FIX AUDIT #9: The naming was confusing — this is NOT the bid-ask spread
    within one exchange, but the cross-exchange spread. When bid comes from
    exchange B and ask from exchange A, positive means B.bid > A.ask.

    Args:
        bid_price: Best bid price on exchange B (where you can SELL)
        ask_price: Best ask price on exchange A (where you can BUY)

    Returns:
        Спред в процентах (positive = cross-exchange arbitrage opportunity)
    """
    if ask_price == 0:
        return 0.0
    return ((bid_price - ask_price) / ask_price) * 100


def calculate_bid_ask_spread_pct(bid: float, ask: float) -> float:
    """
    Calculate the bid-ask spread as % of mid-price for a single exchange.
    Always >= 0. Lower = more liquid.

    FIX AUDIT: New function — replaces misuse of calculate_spread for
    intra-exchange spread measurement.
    """
    mid = (bid + ask) / 2.0
    if mid == 0:
        return 0.0
    return (ask - bid) / mid * 100


def round_down(value: float, decimals: int) -> float:
    """
    Округлить вниз до указанного количества знаков

    Args:
        value: Значение для округления
        decimals: Количество десятичных знаков

    Returns:
        Округленное значение
    """
    decimal_value = Decimal(str(value))
    return float(decimal_value.quantize(Decimal(10) ** -decimals, rounding=ROUND_DOWN))


def format_size(size: float, precision: int = 3) -> float:
    """
    Форматировать размер позиции с учетом точности

    Args:
        size: Размер позиции
        precision: Точность (количество знаков)

    Returns:
        Отформатированный размер
    """
    return round_down(size, precision)


def validate_orderbook(orderbook: Dict[str, Any]) -> bool:
    """
    Проверить валидность стакана

    Args:
        orderbook: Данные стакана

    Returns:
        True если стакан валидный
    """
    if not orderbook:
        return False

    if 'bids' not in orderbook or 'asks' not in orderbook:
        return False

    if not orderbook['bids'] or not orderbook['asks']:
        return False

    # Проверяем формат данных
    try:
        bid = float(orderbook['bids'][0][0])
        ask = float(orderbook['asks'][0][0])

        if bid <= 0 or ask <= 0:
            return False

        if bid >= ask:  # Бид не может быть больше аска
            return False

        return True
    except (IndexError, ValueError, TypeError):
        return False


def get_best_bid_ask(orderbook: Dict[str, Any]) -> Optional[tuple[float, float]]:
    """
    Получить лучшие bid и ask из стакана

    Args:
        orderbook: Данные стакана

    Returns:
        Tuple (bid, ask) или None если данные невалидны
    """
    if not validate_orderbook(orderbook):
        return None

    try:
        bid = float(orderbook['bids'][0][0])
        ask = float(orderbook['asks'][0][0])
        return (bid, ask)
    except (IndexError, ValueError, TypeError):
        return None


def calculate_position_value(size: float, price: float) -> float:
    """
    Рассчитать стоимость позиции

    Args:
        size: Размер позиции
        price: Цена

    Returns:
        Стоимость позиции
    """
    return size * price


def calculate_pnl(
    entry_price: float,
    exit_price: float,
    size: float,
    side: str,
    fee_rate: float = 0.0,
) -> float:
    """
    Рассчитать PnL позиции net of fees.

    FIX AUDIT #8: Previously ignored fees — a "profitable" trade could lose
    money after fees. Now accepts fee_rate (decimal, e.g. 0.0005 = 0.05%)
    and deducts it from gross PnL.

    Args:
        entry_price: Цена входа
        exit_price: Цена выхода
        size: Размер позиции
        side: Сторона ("LONG" или "SHORT")
        fee_rate: Trading fee rate per leg (e.g. 0.0005 = 0.05% = 5 bps).
            Total fee = size × (entry_price + exit_price) × fee_rate

    Returns:
        PnL в USDT (net of fees)
    """
    if side == "LONG":
        gross_pnl = (exit_price - entry_price) * size
    else:  # SHORT
        gross_pnl = (entry_price - exit_price) * size

    # Deduct fees on both entry and exit legs
    fee_total = size * (entry_price + exit_price) * fee_rate
    return gross_pnl - fee_total


def calculate_pnl_with_fees(
    entry_price_long: float,
    exit_price_long: float,
    entry_price_short: float,
    exit_price_short: float,
    size_usd: float,
    fee_rate_long: float = 0.0005,
    fee_rate_short: float = 0.0005,
    funding_pnl: float = 0.0,
) -> float:
    """
    Calculate total PnL for a cross-exchange arbitrage position, net of fees
    and including funding income/cost.

    FIX AUDIT #8: New comprehensive PnL function.

    Args:
        entry_price_long: Entry price on long exchange
        exit_price_long: Exit price (bid) on long exchange
        entry_price_short: Entry price (ask) on short exchange
        exit_price_short: Exit price (bid) on short exchange
        size_usd: Position notional in USD
        fee_rate_long: Fee rate on long exchange (e.g. 0.0005 = 5 bps)
        fee_rate_short: Fee rate on short exchange
        funding_pnl: Net funding income (+) or cost (-)

    Returns:
        Total PnL in USD net of all costs
    """
    # Long leg PnL
    long_pnl = ((exit_price_long - entry_price_long) / max(entry_price_long, 1e-9)) * size_usd

    # Short leg PnL
    short_pnl = ((entry_price_short - exit_price_short) / max(entry_price_short, 1e-9)) * size_usd

    # Fees: entry (2 legs) + exit (2 legs) = 4 fee events
    entry_fees = size_usd * (fee_rate_long + fee_rate_short)
    exit_fees = size_usd * (fee_rate_long + fee_rate_short)

    return long_pnl + short_pnl - entry_fees - exit_fees + funding_pnl


def usdt_to_htx(symbol: str) -> str:
    """Convert BTCUSDT -> BTC-USDT (HTX format)."""
    if "-" in symbol:
        return symbol.upper()
    if symbol.upper().endswith("USDT"):
        base = symbol[:-4].upper()
        return f"{base}-USDT"
    return symbol.upper()
