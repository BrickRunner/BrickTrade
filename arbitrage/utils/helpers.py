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
    Рассчитать спред в процентах

    Args:
        bid_price: Цена покупки
        ask_price: Цена продажи

    Returns:
        Спред в процентах
    """
    if ask_price == 0:
        return 0.0
    return ((bid_price - ask_price) / ask_price) * 100


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


def calculate_pnl(entry_price: float, exit_price: float, size: float, side: str) -> float:
    """
    Рассчитать PnL позиции

    Args:
        entry_price: Цена входа
        exit_price: Цена выхода
        size: Размер позиции
        side: Сторона ("LONG" или "SHORT")

    Returns:
        PnL в USDT
    """
    if side == "LONG":
        return (exit_price - entry_price) * size
    else:  # SHORT
        return (entry_price - exit_price) * size
