from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from aiogram import types
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from arbitrage.exchanges.bybit_rest import BybitRestClient
from arbitrage.utils.config import ExchangeConfig
from arbitrage.system.strategies.overheat_detector import (
    Candle,
    OverheatDetector,
    OverheatSignal,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Symbols & timeframes
# ---------------------------------------------------------------------------

SHORT_SYMBOLS: List[str] = [
    # Majors
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "XRPUSDT", "SOLUSDT",

    # Meme coins (высокая волатильность)
    "DOGEUSDT", "PEPEUSDT", "SHIBUSDT", "WIFUSDT", "BONKUSDT",
    "FLOKIUSDT", "MEMEUSDT", "BOMEUSDT", "TRUMPUSDT", "MEWUSDT",
    "MOGSUSDT", "BRETTUSDT", "POPCATUSDT", "NEIROUSDT", "BABYUSDT",
    "MYRIAUSDT", "TURBOLUSDT", "RATUSDT", "SAMOYNUSDT", "MNTUSDT",

    # AI / DeFi / Infrastructure
    "RENDERUSDT", "FETUSDT", "AGIXUSDT", "AAVEUSDT", "UNIUSDT",
    "MKRUSDT", "LDOUSDT", "THETAUSDT", "GRTUSDT", "INJUSDT",

    # Layer 1 / Layer 2
    "ADAUSDT", "AVAXUSDT", "DOTUSDT", "MATICUSDT", "ATOMUSDT",
    "NEARUSDT", "APTUSDT", "SUIUSDT", "SEIUSDT", "ARBUSDT",
    "OPUSDT", "MANTAUSDT", "STXUSDT", "IMXUSDT", "TIAUSDT",

    # DeFi / Gaming / Metaverse
    "LINKUSDT", "AXSUSDT", "SANDUSDT", "MANAUSDT", "GALAUSDT",
    "ILVUSDT", "ENSUSDT", "ICPUSDT", "JTOUSDT", "BLURUSDT",

    # Other high-volume alts
    "TONUSDT", "TRXUSDT", "LTCUSDT", "ETCUSDT", "FILUSDT",
    "RUNEUSDT", "FTMUSDT", "XLMUSDT", "ALGOUSDT", "VETUSDT",
    "HBARUSDT", "EGLDUSDT", "FLOWUSDT", "MINAUSDT", "ZILUSDT",

    # New trending / volatile
    "WLDUSDT", "JUPUSDT", "PYTHUSDT", "ONDOUSDT", "DYMUSDT",
    "ALTUSDT", "PIXELUSDT", "ACEUSDT", "XAIUSDT", "PORTOUSDT",
    "ORDIUSDT", "CHRUSDT", "QNTUSDT", "KASUSDT", "ARKMUSDT",
    "BEAMUSDT", "CKBUSDT", "1000SATSUSDT", "ENAUSDT", "USTCUSDT",
]

TIMEFRAMES: Dict[str, str] = {
    "1m": "1",
    "5m": "5",
    "15m": "15",
}

# ---------------------------------------------------------------------------
# Position tracking
# ---------------------------------------------------------------------------

POSITIONS_FILE = "data/short_positions.json"


@dataclass
class ShortPosition:
    symbol: str
    side: str  # always "Sell" for short
    qty: float
    entry_price: float
    stop_loss: float
    take_profit: float
    leverage: int
    order_id: str
    opened_at: float = field(default_factory=time.time)
    score: int = 0
    pnl: float = 0.0
    status: str = "open"  # open / closed / error

    def to_dict(self) -> Dict[str, Any]:
        return {
            "symbol": self.symbol, "side": self.side, "qty": self.qty,
            "entry_price": self.entry_price, "stop_loss": self.stop_loss,
            "take_profit": self.take_profit, "leverage": self.leverage,
            "order_id": self.order_id, "opened_at": self.opened_at,
            "score": self.score, "pnl": self.pnl, "status": self.status,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShortPosition":
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ---------------------------------------------------------------------------
# Execution config
# ---------------------------------------------------------------------------

EXEC_CONFIG: Dict[str, Any] = {
    "enabled": True,             # execution on by default
    "leverage": 5,
    "order_size_usdt": 5.5,      # per-trade size in USDT (Bybit min ~5 USDT)
    "max_positions": 3,          # max simultaneous shorts
    "sl_pct": 2.0,               # stop-loss %
    "tp_pct": 3.0,               # take-profit %
    "min_score": 5,              # min overheat score to execute
    "auto_execute": True,        # auto-execute on scan without confirm
}

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_detector = OverheatDetector()
_client: Optional[BybitRestClient] = None
_scan_task: Optional[asyncio.Task] = None
_auto_interval: int = 300
_selected_tf: str = "5m"
_last_results: List[OverheatSignal] = []
_open_positions: List[ShortPosition] = []
_user_id: Optional[int] = None
_instrument_cache: Dict[str, Dict[str, Any]] = {}


def _get_client() -> BybitRestClient:
    """Lazy-init Bybit client from .env keys."""
    global _client
    if _client is None:
        cfg = ExchangeConfig(
            api_key=os.getenv("BYBIT_API_KEY", ""),
            api_secret=os.getenv("BYBIT_SECRET", ""),
            testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true",
        )
        _client = BybitRestClient(cfg)
    return _client


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

def _save_positions() -> None:
    os.makedirs(os.path.dirname(POSITIONS_FILE), exist_ok=True)
    with open(POSITIONS_FILE, "w") as f:
        json.dump([p.to_dict() for p in _open_positions], f, indent=2)


def _load_positions() -> None:
    global _open_positions
    if os.path.exists(POSITIONS_FILE):
        try:
            with open(POSITIONS_FILE) as f:
                data = json.load(f)
            _open_positions = [ShortPosition.from_dict(d) for d in data]
        except Exception as e:
            logger.error("short: load positions error: %s", e)
            _open_positions = []


_load_positions()

# ---------------------------------------------------------------------------
# Data fetchers via BybitRestClient
# ---------------------------------------------------------------------------

async def _fetch_candles(symbol: str, interval: str = "5", limit: int = 300) -> List[Candle]:
    client = _get_client()
    data = await client.get_kline(symbol, interval=interval, limit=limit)
    if data.get("retCode") != 0:
        return []
    rows = data.get("result", {}).get("list", [])
    if not rows:
        return []
    candles: List[Candle] = []
    for row in reversed(rows):  # newest-first → oldest-first
        candles.append(Candle(
            timestamp=int(row[0]),
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        ))
    return candles


async def _fetch_funding(symbol: str) -> Optional[float]:
    client = _get_client()
    data = await client.get_ticker(symbol)
    if data.get("retCode") == 0:
        items = data.get("result", {}).get("list", [])
        if items:
            rate_str = items[0].get("fundingRate", "")
            if rate_str:
                return float(rate_str)
    return None


async def _fetch_oi_history(symbol: str) -> Optional[List[float]]:
    client = _get_client()
    data = await client.get_open_interest(symbol, interval_time="5min", limit=10)
    if data.get("retCode") == 0:
        items = data.get("result", {}).get("list", [])
        if items:
            return [float(item["openInterest"]) for item in reversed(items)]
    return None


async def _fetch_instrument(symbol: str) -> Dict[str, Any]:
    """Fetch and cache instrument info (lot sizes, tick size)."""
    if symbol in _instrument_cache:
        return _instrument_cache[symbol]
    client = _get_client()
    data = await client.get_instrument_info(symbol)
    if data.get("retCode") == 0:
        items = data.get("result", {}).get("list", [])
        if items:
            _instrument_cache[symbol] = items[0]
            return items[0]
    return {}


# ---------------------------------------------------------------------------
# Order sizing helpers
# ---------------------------------------------------------------------------

def _round_qty(qty: float, qty_step: float) -> float:
    """Round quantity down to nearest qty_step."""
    if qty_step <= 0:
        return qty
    precision = max(0, -int(math.floor(math.log10(qty_step)))) if qty_step < 1 else 0
    rounded = math.floor(qty / qty_step) * qty_step
    return round(rounded, precision)


def _round_price(price: float, tick_size: float) -> float:
    """Round price to nearest tick_size."""
    if tick_size <= 0:
        return price
    precision = max(0, -int(math.floor(math.log10(tick_size)))) if tick_size < 1 else 0
    rounded = round(round(price / tick_size) * tick_size, precision)
    return rounded


# ---------------------------------------------------------------------------
# Execution engine
# ---------------------------------------------------------------------------

async def _execute_short(
    symbol: str, signal: OverheatSignal, bot=None, user_id: int = 0,
) -> Optional[ShortPosition]:
    """
    Open a SHORT position on Bybit for an overheated symbol.

    Flow:
    1. Check max positions limit
    2. Fetch instrument info (qty step, min qty, tick size)
    3. Get current price from last candle / ticker
    4. Calculate position size from USDT amount
    5. Set leverage
    6. Place SELL market order (open short)
    7. Set SL/TP via trading-stop
    8. Track position
    """
    cfg = EXEC_CONFIG
    client = _get_client()

    # --- Guard: already have position in this symbol ---
    active = [p for p in _open_positions if p.symbol == symbol and p.status == "open"]
    if active:
        logger.info("short: already have open position for %s, skipping", symbol)
        return None

    # --- Guard: max positions ---
    open_count = sum(1 for p in _open_positions if p.status == "open")
    if open_count >= cfg["max_positions"]:
        logger.info("short: max positions (%d) reached, skipping %s", cfg["max_positions"], symbol)
        return None

    # --- Guard: API keys ---
    if client.public_only:
        logger.warning("short: no API keys configured, cannot execute")
        return None

    try:
        # 1. Instrument info
        inst = await _fetch_instrument(symbol)
        if not inst:
            logger.error("short: no instrument info for %s", symbol)
            return None

        lot_filter = inst.get("lotSizeFilter", {})
        price_filter = inst.get("priceFilter", {})
        qty_step = float(lot_filter.get("qtyStep", "0.001"))
        min_qty = float(lot_filter.get("minOrderQty", "0.001"))
        min_notional = float(lot_filter.get("minNotionalValue", "5"))
        tick_size = float(price_filter.get("tickSize", "0.01"))

        # 2. Current price
        ticker_data = await client.get_ticker(symbol)
        if ticker_data.get("retCode") != 0:
            logger.error("short: failed to get ticker for %s", symbol)
            return None
        ticker_list = ticker_data.get("result", {}).get("list", [])
        if not ticker_list:
            return None
        current_price = float(ticker_list[0].get("lastPrice", 0))
        if current_price <= 0:
            return None

        # 3. Calculate qty
        leverage = cfg["leverage"]
        notional = cfg["order_size_usdt"]
        qty = notional / current_price
        qty = _round_qty(qty, qty_step)

        if qty < min_qty:
            logger.warning("short: calculated qty %.6f < min %.6f for %s", qty, min_qty, symbol)
            return None
        if qty * current_price < min_notional:
            logger.warning("short: notional %.2f < min %.2f for %s",
                           qty * current_price, min_notional, symbol)
            return None

        # 4. Set leverage
        lev_resp = await client.set_leverage(symbol, leverage)
        if lev_resp.get("retCode") not in (0, 110043):
            # 110043 = leverage not modified (already set)
            logger.warning("short: set leverage response: %s", lev_resp)

        # 5. Place SELL market order (open short)
        order_resp = await client.place_order(
            symbol=symbol,
            side="Sell",
            size=qty,
            order_type="market",
            offset="open",
        )

        if order_resp.get("retCode") != 0:
            err_msg = order_resp.get("retMsg", "unknown error")
            logger.error("short: order failed for %s: %s", symbol, err_msg)
            if bot and user_id:
                await bot.send_message(
                    user_id,
                    f"❌ SHORT ордер {symbol} не исполнен: {err_msg}",
                    parse_mode="HTML",
                )
            return None

        order_id = order_resp.get("result", {}).get("orderId", "")
        logger.info("short: SELL market order placed for %s qty=%.6f orderId=%s",
                     symbol, qty, order_id)

        # 6. Calculate SL / TP prices
        sl_price = _round_price(current_price * (1 + cfg["sl_pct"] / 100), tick_size)
        tp_price = _round_price(current_price * (1 - cfg["tp_pct"] / 100), tick_size)

        # 7. Set trading stop (SL + TP)
        await asyncio.sleep(0.5)  # wait for fill
        stop_resp = await client.set_trading_stop(
            symbol=symbol,
            stop_loss=sl_price,
            take_profit=tp_price,
        )
        if stop_resp.get("retCode") != 0:
            logger.warning("short: set SL/TP failed for %s: %s", symbol, stop_resp.get("retMsg"))

        # 8. Track position
        pos = ShortPosition(
            symbol=symbol,
            side="Sell",
            qty=qty,
            entry_price=current_price,
            stop_loss=sl_price,
            take_profit=tp_price,
            leverage=leverage,
            order_id=order_id,
            score=signal.score,
        )
        _open_positions.append(pos)
        _save_positions()

        # 9. Notify
        if bot and user_id:
            text = (
                f"🔻 <b>SHORT ОТКРЫТ</b>\n\n"
                f"Пара: <b>{symbol}</b>\n"
                f"Вход: <b>{current_price}</b>\n"
                f"Размер: <b>{qty}</b> ({notional:.0f} USDT × {leverage}x)\n"
                f"SL: <b>{sl_price}</b> ({cfg['sl_pct']}%)\n"
                f"TP: <b>{tp_price}</b> ({cfg['tp_pct']}%)\n"
                f"Score: <b>{signal.score}/7</b>\n"
                f"Order ID: <code>{order_id}</code>"
            )
            await bot.send_message(user_id, text, parse_mode="HTML")

        return pos

    except Exception as e:
        logger.error("short: execution error for %s: %s", symbol, e, exc_info=True)
        if bot and user_id:
            await bot.send_message(
                user_id,
                f"❌ Ошибка открытия SHORT {symbol}: {e}",
                parse_mode="HTML",
            )
        return None


async def _close_short_position(
    pos: ShortPosition, bot=None, user_id: int = 0, reason: str = "manual",
) -> bool:
    """Close an open SHORT by placing a BUY market order (reduceOnly)."""
    client = _get_client()
    if client.public_only:
        return False

    try:
        resp = await client.place_order(
            symbol=pos.symbol,
            side="Buy",
            size=pos.qty,
            order_type="market",
            offset="close",
        )
        if resp.get("retCode") != 0:
            logger.error("short: close failed for %s: %s", pos.symbol, resp.get("retMsg"))
            return False

        # Get current price to calculate PnL
        ticker = await client.get_ticker(pos.symbol)
        exit_price = pos.entry_price
        if ticker.get("retCode") == 0:
            items = ticker.get("result", {}).get("list", [])
            if items:
                exit_price = float(items[0].get("lastPrice", pos.entry_price))

        pos.pnl = (pos.entry_price - exit_price) * pos.qty
        pos.status = "closed"
        _save_positions()

        logger.info("short: closed %s reason=%s pnl=%.4f", pos.symbol, reason, pos.pnl)

        if bot and user_id:
            pnl_icon = "🟢" if pos.pnl >= 0 else "🔴"
            text = (
                f"✅ <b>SHORT ЗАКРЫТ</b>  ({reason})\n\n"
                f"Пара: <b>{pos.symbol}</b>\n"
                f"Вход: {pos.entry_price} → Выход: {exit_price}\n"
                f"PnL: {pnl_icon} <b>{pos.pnl:+.4f} USDT</b>\n"
            )
            await bot.send_message(user_id, text, parse_mode="HTML")
        return True

    except Exception as e:
        logger.error("short: close error for %s: %s", pos.symbol, e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Core scan
# ---------------------------------------------------------------------------

async def _scan_all_symbols(tf: str) -> List[OverheatSignal]:
    global _last_results
    results: List[OverheatSignal] = []
    bybit_interval = TIMEFRAMES.get(tf, "5")
    sem = asyncio.Semaphore(5)

    async def _analyze_one(sym: str) -> Optional[OverheatSignal]:
        async with sem:
            candles = await _fetch_candles(sym, interval=bybit_interval, limit=300)
            if len(candles) < 20:
                return None
            funding, oi_history = await asyncio.gather(
                _fetch_funding(sym),
                _fetch_oi_history(sym),
            )
            return _detector.analyze(sym, candles, funding_rate=funding, oi_history=oi_history)

    tasks = [_analyze_one(s) for s in SHORT_SYMBOLS]
    for result in await asyncio.gather(*tasks, return_exceptions=True):
        if isinstance(result, OverheatSignal):
            results.append(result)

    results.sort(key=lambda s: s.score, reverse=True)
    _last_results = results
    return results


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _format_signal(sig: OverheatSignal) -> str:
    lines = [
        f"{'🔥' if sig.is_overheated else '⚪'} <b>{sig.symbol}</b>  "
        f"Score: <b>{sig.score}/7</b>  "
        f"{'<b>OVERHEATED</b>' if sig.is_overheated else 'норм'}",
    ]
    sig_names = {
        "rsi": "RSI > порог",
        "price_spike": "Price Spike",
        "volume_spike": "Volume Spike",
        "funding": "Funding > 0",
        "oi_growth": "OI Growth",
        "ema_deviation": "EMA Deviation",
        "divergence": "Divergence",
    }
    parts = []
    for key, label in sig_names.items():
        icon = "✅" if sig.signals.get(key) else "❌"
        parts.append(f"{icon} {label}")
    lines.append("  ".join(parts[:4]))
    lines.append("  ".join(parts[4:]))
    lines.append(
        f"RSI={sig.meta.get('rsi_value', 0):.1f}  "
        f"ΔPrice={sig.meta.get('price_change', 0):.2f}%  "
        f"Vol×{sig.meta.get('volume_ratio', 0):.1f}  "
        f"EMA Δ={sig.meta.get('ema_distance', 0):.2f}%"
    )
    return "\n".join(lines)


def _format_results(results: List[OverheatSignal], tf: str) -> str:
    hot = [s for s in results if s.is_overheated]
    header = f"🔻 <b>Шорт-бот сканирование</b>  (TF: {tf}, Bybit)\n"
    header += f"Проанализировано: {len(results)} пар\n"
    header += f"Перегретых: <b>{len(hot)}</b>\n"
    exec_status = "✅ ВКЛ" if EXEC_CONFIG["enabled"] else "❌ ВЫКЛ"
    header += f"Торговля: {exec_status}\n"
    header += "━" * 30 + "\n"
    if hot:
        header += "\n".join(_format_signal(s) for s in hot)
        if EXEC_CONFIG["enabled"] and not EXEC_CONFIG["auto_execute"]:
            header += "\n\n💡 <i>Нажмите на пару чтобы открыть SHORT</i>"
    else:
        top = results[:5]
        header += "\n<i>Перегретых пар не найдено. Топ-5 по score:</i>\n\n"
        header += "\n\n".join(_format_signal(s) for s in top)
    return header


def _format_positions() -> str:
    open_pos = [p for p in _open_positions if p.status == "open"]
    closed_pos = [p for p in _open_positions if p.status == "closed"]
    if not open_pos and not closed_pos:
        return "📋 <b>Позиции</b>\n\nНет открытых или закрытых позиций."

    lines = ["📋 <b>Позиции Шорт-бот</b>\n"]
    if open_pos:
        lines.append(f"<b>Открытые ({len(open_pos)}):</b>")
        for p in open_pos:
            lines.append(
                f"  🔻 <b>{p.symbol}</b>  qty={p.qty}  entry={p.entry_price}\n"
                f"     SL={p.stop_loss}  TP={p.take_profit}  "
                f"lev={p.leverage}x  score={p.score}"
            )
    if closed_pos:
        total_pnl = sum(p.pnl for p in closed_pos)
        lines.append(f"\n<b>Закрытые ({len(closed_pos)}):</b>")
        for p in closed_pos[-5:]:
            icon = "🟢" if p.pnl >= 0 else "🔴"
            lines.append(f"  {icon} {p.symbol}  PnL: {p.pnl:+.4f} USDT")
        lines.append(f"\nИтого PnL: <b>{total_pnl:+.4f} USDT</b>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Auto-scan loop
# ---------------------------------------------------------------------------

async def _auto_scan_loop(bot, user_id: int) -> None:
    while True:
        try:
            results = await _scan_all_symbols(_selected_tf)
            hot = [s for s in results if s.is_overheated]
            if hot:
                text = _format_results(results, _selected_tf)
                text += "\n\n🔔 <b>Авто-уведомление</b>"
                try:
                    await bot.send_message(user_id, text, parse_mode="HTML")
                except Exception as exc:
                    logger.warning("short: failed to send auto-scan: %s", exc)

                # Auto-execute if enabled
                if EXEC_CONFIG["enabled"] and EXEC_CONFIG["auto_execute"]:
                    for sig in hot:
                        if sig.score >= EXEC_CONFIG["min_score"]:
                            await _execute_short(sig.symbol, sig, bot=bot, user_id=user_id)
        except Exception as exc:
            logger.error("short: auto-scan error: %s", exc)
        await asyncio.sleep(_auto_interval)


# ---------------------------------------------------------------------------
# Keyboards
# ---------------------------------------------------------------------------

def _short_menu_kb(current_tf: str) -> InlineKeyboardMarkup:
    tf_buttons = []
    for tf in ["1m", "5m", "15m"]:
        label = f"{tf} ✓" if tf == current_tf else tf
        tf_buttons.append(InlineKeyboardButton(text=label, callback_data=f"short_tf:{tf}"))
    auto_running = _scan_task is not None and not _scan_task.done()
    auto_label = "🔄 Авто ✅" if auto_running else "🔄 Авто OFF"
    auto_cb = "short_auto_off" if auto_running else "short_auto_on"
    exec_label = "⚡ Торговля ✅" if EXEC_CONFIG["enabled"] else "⚡ Торговля OFF"
    exec_cb = "short_exec_off" if EXEC_CONFIG["enabled"] else "short_exec_on"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Сканировать", callback_data="short_scan_now"),
         InlineKeyboardButton(text=auto_label, callback_data=auto_cb)],
        tf_buttons,
        [InlineKeyboardButton(text=exec_label, callback_data=exec_cb),
         InlineKeyboardButton(text="📋 Позиции", callback_data="short_positions")],
        [InlineKeyboardButton(text="📊 Последние", callback_data="short_last"),
         InlineKeyboardButton(text="⚙ Настройки", callback_data="short_settings")],
    ])


def _results_kb(hot_signals: List[OverheatSignal]) -> InlineKeyboardMarkup:
    """Keyboard with buttons for each overheated symbol (to confirm short)."""
    rows: list[list[InlineKeyboardButton]] = []
    if EXEC_CONFIG["enabled"] and not EXEC_CONFIG["auto_execute"]:
        # Show confirm buttons for overheated symbols
        row: list[InlineKeyboardButton] = []
        for sig in hot_signals:
            if sig.score >= EXEC_CONFIG["min_score"]:
                row.append(InlineKeyboardButton(
                    text=f"🔻 SHORT {sig.symbol}",
                    callback_data=f"short_exec:{sig.symbol}",
                ))
                if len(row) == 2:
                    rows.append(row)
                    row = []
        if row:
            rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅ Меню", callback_data="short_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _positions_kb() -> InlineKeyboardMarkup:
    """Keyboard to manage open positions."""
    rows: list[list[InlineKeyboardButton]] = []
    open_pos = [p for p in _open_positions if p.status == "open"]
    for p in open_pos:
        rows.append([InlineKeyboardButton(
            text=f"❌ Закрыть {p.symbol}",
            callback_data=f"short_close:{p.symbol}",
        )])
    if open_pos:
        rows.append([InlineKeyboardButton(
            text="🚨 Закрыть ВСЕ",
            callback_data="short_close_all",
        )])
    rows.append([InlineKeyboardButton(text="🗑 Очистить историю", callback_data="short_clear_history")])
    rows.append([InlineKeyboardButton(text="⬅ Меню", callback_data="short_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅ Назад", callback_data="short_menu")],
    ])


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def _safe_edit(msg: types.Message, text: str, reply_markup=None) -> None:
    try:
        await msg.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
    except TelegramBadRequest:
        pass


def _menu_text() -> str:
    auto_status = "✅ активен" if _scan_task and not _scan_task.done() else "❌ выключен"
    exec_status = "✅ ВКЛ" if EXEC_CONFIG["enabled"] else "❌ ВЫКЛ"
    open_count = sum(1 for p in _open_positions if p.status == "open")
    return (
        "🔻 <b>Шорт-бот (Overheat Detector)</b>\n\n"
        "Сканирует перегретые монеты для SHORT.\n"
        "Данные: <b>Bybit API</b> (ключи из .env).\n"
        "7 сигналов: RSI, Price Spike, Volume, "
        "Funding, OI, EMA, Divergence.\n\n"
        f"Таймфрейм: <b>{_selected_tf}</b>\n"
        f"Авто-скан: {auto_status}\n"
        f"Торговля: {exec_status}\n"
        f"Открыто позиций: <b>{open_count}/{EXEC_CONFIG['max_positions']}</b>\n"
        f"Пар: {len(SHORT_SYMBOLS)}"
    )


async def handle_short_menu(m: types.Message) -> None:
    global _user_id, _scan_task
    _user_id = m.from_user.id
    # Auto-start scan loop on first menu open
    if _scan_task is None or _scan_task.done():
        _scan_task = asyncio.create_task(_auto_scan_loop(m.bot, m.from_user.id))
    await m.answer(_menu_text(), parse_mode="HTML", reply_markup=_short_menu_kb(_selected_tf))


async def cb_short_menu(cb: types.CallbackQuery) -> None:
    await _safe_edit(cb.message, _menu_text(), reply_markup=_short_menu_kb(_selected_tf))
    await cb.answer()


async def cb_short_scan_now(cb: types.CallbackQuery) -> None:
    await cb.answer("🔍 Сканирование запущено...")
    await _safe_edit(cb.message, "⏳ <b>Сканирование...</b>\nАнализ перегрева по всем парам (Bybit)...", reply_markup=None)
    results = await _scan_all_symbols(_selected_tf)
    hot = [s for s in results if s.is_overheated]
    text = _format_results(results, _selected_tf)
    await _safe_edit(cb.message, text, reply_markup=_results_kb(hot))


async def cb_short_auto_on(cb: types.CallbackQuery) -> None:
    global _scan_task, _user_id
    _user_id = cb.from_user.id
    if _scan_task and not _scan_task.done():
        await cb.answer("Авто-скан уже запущен")
        return
    _scan_task = asyncio.create_task(_auto_scan_loop(cb.bot, cb.from_user.id))
    await cb.answer("✅ Авто-скан включен")
    await cb_short_menu(cb)


async def cb_short_auto_off(cb: types.CallbackQuery) -> None:
    global _scan_task
    if _scan_task and not _scan_task.done():
        _scan_task.cancel()
        try:
            await _scan_task
        except asyncio.CancelledError:
            pass
    _scan_task = None
    await cb.answer("⏹ Авто-скан выключен")
    await cb_short_menu(cb)


async def cb_short_tf(cb: types.CallbackQuery) -> None:
    global _selected_tf
    tf = cb.data.split(":")[-1]
    if tf in TIMEFRAMES:
        _selected_tf = tf
    await cb.answer(f"Таймфрейм: {_selected_tf}")
    await cb_short_menu(cb)


async def cb_short_last(cb: types.CallbackQuery) -> None:
    if not _last_results:
        await cb.answer("Нет результатов. Запустите сканирование.")
        return
    hot = [s for s in _last_results if s.is_overheated]
    text = _format_results(_last_results, _selected_tf)
    await _safe_edit(cb.message, text, reply_markup=_results_kb(hot))
    await cb.answer()


async def cb_short_settings(cb: types.CallbackQuery) -> None:
    cfg = _detector.config
    ecfg = EXEC_CONFIG
    client = _get_client()
    key_status = "✅ настроен" if client.api_key else "❌ не задан"
    text = (
        "⚙ <b>Настройки Шорт-бот</b>\n\n"
        f"<b>API:</b>\n"
        f"  Источник: Bybit\n"
        f"  API ключ: {key_status}\n\n"
        f"<b>Детектор:</b>\n"
        f"  RSI period/threshold: {cfg['rsi_period']} / {cfg['rsi_threshold']}\n"
        f"  Price change: {cfg['price_change_threshold']}%\n"
        f"  Volume multiplier: {cfg['volume_multiplier']}×\n"
        f"  EMA fast/slow: {cfg['ema_fast']}/{cfg['ema_slow']}\n"
        f"  Score threshold: {cfg['score_threshold']}/7\n\n"
        f"<b>Торговля:</b>\n"
        f"  Режим: {'✅ ВКЛ' if ecfg['enabled'] else '❌ ВЫКЛ'}\n"
        f"  Авто-вход: {'✅' if ecfg['auto_execute'] else '❌ (по кнопке)'}\n"
        f"  Размер: {ecfg['order_size_usdt']} USDT\n"
        f"  Плечо: {ecfg['leverage']}×\n"
        f"  SL: {ecfg['sl_pct']}%  |  TP: {ecfg['tp_pct']}%\n"
        f"  Макс позиций: {ecfg['max_positions']}\n"
        f"  Мин score: {ecfg['min_score']}/7\n\n"
        f"Пар ({len(SHORT_SYMBOLS)}): "
        f"{', '.join(SHORT_SYMBOLS[:10])}..."
    )
    settings_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"💰 Размер: {ecfg['order_size_usdt']} USDT", callback_data="short_set_size")],
        [InlineKeyboardButton(text=f"📊 Плечо: {ecfg['leverage']}×", callback_data="short_set_lev"),
         InlineKeyboardButton(text=f"🎯 Мин score: {ecfg['min_score']}", callback_data="short_set_minscore")],
        [InlineKeyboardButton(text=f"🛑 SL: {ecfg['sl_pct']}%", callback_data="short_set_sl"),
         InlineKeyboardButton(text=f"💎 TP: {ecfg['tp_pct']}%", callback_data="short_set_tp")],
        [InlineKeyboardButton(text=f"📦 Макс позиций: {ecfg['max_positions']}", callback_data="short_set_maxpos")],
        [InlineKeyboardButton(text="⬅ Назад", callback_data="short_menu")],
    ])
    await _safe_edit(cb.message, text, reply_markup=settings_kb)
    await cb.answer()


# --- Settings value pickers ---

_SIZE_OPTIONS = [5.5, 10, 25, 50, 100, 250]
_LEV_OPTIONS = [1, 2, 3, 5, 10, 20]
_SL_OPTIONS = [1.0, 1.5, 2.0, 3.0, 5.0]
_TP_OPTIONS = [1.0, 2.0, 3.0, 5.0, 10.0]
_MAXPOS_OPTIONS = [1, 2, 3, 5, 10]
_MINSCORE_OPTIONS = [3, 4, 5, 6, 7]


def _picker_kb(options: list, prefix: str, current, fmt: str = "{}") -> InlineKeyboardMarkup:
    row = []
    for v in options:
        label = fmt.format(v)
        if v == current:
            label += " ✓"
        row.append(InlineKeyboardButton(text=label, callback_data=f"{prefix}:{v}"))
    # Split into rows of 3
    rows = [row[i:i + 3] for i in range(0, len(row), 3)]
    rows.append([InlineKeyboardButton(text="⬅ Назад", callback_data="short_settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cb_short_set_size(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_SIZE_OPTIONS, "short_size_val", EXEC_CONFIG["order_size_usdt"], "{} USDT")
    await _safe_edit(cb.message, "💰 <b>Размер позиции (USDT)</b>\nВыберите размер одной сделки:", reply_markup=kb)
    await cb.answer()


async def cb_short_size_val(cb: types.CallbackQuery) -> None:
    val = float(cb.data.split(":")[-1])
    EXEC_CONFIG["order_size_usdt"] = val
    await cb.answer(f"✅ Размер: {val} USDT")
    await cb_short_settings(cb)


async def cb_short_set_lev(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_LEV_OPTIONS, "short_lev_val", EXEC_CONFIG["leverage"], "{}×")
    await _safe_edit(cb.message, "📊 <b>Кредитное плечо</b>\nВыберите leverage:", reply_markup=kb)
    await cb.answer()


async def cb_short_lev_val(cb: types.CallbackQuery) -> None:
    val = int(cb.data.split(":")[-1])
    EXEC_CONFIG["leverage"] = val
    await cb.answer(f"✅ Плечо: {val}×")
    await cb_short_settings(cb)


async def cb_short_set_sl(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_SL_OPTIONS, "short_sl_val", EXEC_CONFIG["sl_pct"], "{}%")
    await _safe_edit(cb.message, "🛑 <b>Stop-Loss</b>\nПроцент от входа:", reply_markup=kb)
    await cb.answer()


async def cb_short_sl_val(cb: types.CallbackQuery) -> None:
    val = float(cb.data.split(":")[-1])
    EXEC_CONFIG["sl_pct"] = val
    await cb.answer(f"✅ SL: {val}%")
    await cb_short_settings(cb)


async def cb_short_set_tp(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_TP_OPTIONS, "short_tp_val", EXEC_CONFIG["tp_pct"], "{}%")
    await _safe_edit(cb.message, "💎 <b>Take-Profit</b>\nПроцент от входа:", reply_markup=kb)
    await cb.answer()


async def cb_short_tp_val(cb: types.CallbackQuery) -> None:
    val = float(cb.data.split(":")[-1])
    EXEC_CONFIG["tp_pct"] = val
    await cb.answer(f"✅ TP: {val}%")
    await cb_short_settings(cb)


async def cb_short_set_maxpos(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_MAXPOS_OPTIONS, "short_maxpos_val", EXEC_CONFIG["max_positions"], "{}")
    await _safe_edit(cb.message, "📦 <b>Макс позиций</b>\nОдновременно открытых:", reply_markup=kb)
    await cb.answer()


async def cb_short_maxpos_val(cb: types.CallbackQuery) -> None:
    val = int(cb.data.split(":")[-1])
    EXEC_CONFIG["max_positions"] = val
    await cb.answer(f"✅ Макс позиций: {val}")
    await cb_short_settings(cb)


async def cb_short_set_minscore(cb: types.CallbackQuery) -> None:
    kb = _picker_kb(_MINSCORE_OPTIONS, "short_minscore_val", EXEC_CONFIG["min_score"], "{}/7")
    await _safe_edit(cb.message, "🎯 <b>Минимальный score</b>\nДля открытия позиции:", reply_markup=kb)
    await cb.answer()


async def cb_short_minscore_val(cb: types.CallbackQuery) -> None:
    val = int(cb.data.split(":")[-1])
    EXEC_CONFIG["min_score"] = val
    await cb.answer(f"✅ Мин score: {val}/7")
    await cb_short_settings(cb)


# --- Execution toggles ---

async def cb_short_exec_on(cb: types.CallbackQuery) -> None:
    client = _get_client()
    if client.public_only:
        await cb.answer("❌ Нет API ключей! Задайте BYBIT_API_KEY/BYBIT_SECRET в .env")
        return
    EXEC_CONFIG["enabled"] = True
    await cb.answer("⚡ Торговля ВКЛЮЧЕНА")
    await cb_short_menu(cb)


async def cb_short_exec_off(cb: types.CallbackQuery) -> None:
    EXEC_CONFIG["enabled"] = False
    await cb.answer("⏹ Торговля ВЫКЛЮЧЕНА")
    await cb_short_menu(cb)


# --- Execute short by button ---

async def cb_short_exec_symbol(cb: types.CallbackQuery) -> None:
    """User confirms opening a SHORT for specific symbol."""
    symbol = cb.data.split(":")[-1]
    if not EXEC_CONFIG["enabled"]:
        await cb.answer("Торговля выключена")
        return

    # Find signal for this symbol
    sig = next((s for s in _last_results if s.symbol == symbol and s.is_overheated), None)
    if not sig:
        await cb.answer(f"Сигнал для {symbol} не найден или устарел")
        return

    if sig.score < EXEC_CONFIG["min_score"]:
        await cb.answer(f"Score {sig.score} < min {EXEC_CONFIG['min_score']}")
        return

    await cb.answer(f"⏳ Открываю SHORT {symbol}...")
    await _safe_edit(
        cb.message,
        f"⏳ <b>Открываю SHORT {symbol}...</b>\n"
        f"Score: {sig.score}/7 | Leverage: {EXEC_CONFIG['leverage']}x",
        reply_markup=None,
    )

    pos = await _execute_short(symbol, sig, bot=cb.bot, user_id=cb.from_user.id)
    if pos:
        await _safe_edit(
            cb.message,
            f"✅ <b>SHORT {symbol} открыт</b>\n"
            f"Entry: {pos.entry_price}  Qty: {pos.qty}\n"
            f"SL: {pos.stop_loss}  TP: {pos.take_profit}",
            reply_markup=_back_kb(),
        )
    else:
        await _safe_edit(
            cb.message,
            f"❌ Не удалось открыть SHORT {symbol}",
            reply_markup=_back_kb(),
        )


# --- Positions ---

async def cb_short_positions(cb: types.CallbackQuery) -> None:
    text = _format_positions()
    await _safe_edit(cb.message, text, reply_markup=_positions_kb())
    await cb.answer()


async def cb_short_close(cb: types.CallbackQuery) -> None:
    """Close a specific position by symbol."""
    symbol = cb.data.split(":")[-1]
    pos = next((p for p in _open_positions if p.symbol == symbol and p.status == "open"), None)
    if not pos:
        await cb.answer(f"Нет открытой позиции {symbol}")
        return

    await cb.answer(f"⏳ Закрываю {symbol}...")
    success = await _close_short_position(pos, bot=cb.bot, user_id=cb.from_user.id, reason="manual")
    if success:
        text = _format_positions()
        await _safe_edit(cb.message, text, reply_markup=_positions_kb())
    else:
        await _safe_edit(cb.message, f"❌ Ошибка закрытия {symbol}", reply_markup=_positions_kb())


async def cb_short_close_all(cb: types.CallbackQuery) -> None:
    """Emergency close all open positions."""
    open_pos = [p for p in _open_positions if p.status == "open"]
    if not open_pos:
        await cb.answer("Нет открытых позиций")
        return

    await cb.answer(f"⏳ Закрываю {len(open_pos)} позиций...")
    for pos in open_pos:
        await _close_short_position(pos, bot=cb.bot, user_id=cb.from_user.id, reason="close_all")

    text = _format_positions()
    await _safe_edit(cb.message, text, reply_markup=_positions_kb())


async def cb_short_clear_history(cb: types.CallbackQuery) -> None:
    """Clear closed positions from history."""
    global _open_positions
    _open_positions = [p for p in _open_positions if p.status == "open"]
    _save_positions()
    await cb.answer("🗑 История очищена")
    text = _format_positions()
    await _safe_edit(cb.message, text, reply_markup=_positions_kb())


# ---------------------------------------------------------------------------
# Shutdown
# ---------------------------------------------------------------------------

async def shutdown_short() -> None:
    global _scan_task, _client
    if _scan_task and not _scan_task.done():
        _scan_task.cancel()
        try:
            await _scan_task
        except (asyncio.CancelledError, Exception):
            pass
    _scan_task = None
    if _client:
        await _client.close()
        _client = None
    logger.info("short-bot: shutdown complete")
