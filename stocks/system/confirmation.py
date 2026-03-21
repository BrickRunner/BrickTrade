from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Coroutine, Dict, Optional

from stocks.system.models import StockTradeIntent

logger = logging.getLogger(__name__)


@dataclass
class PendingConfirmation:
    intent_id: str
    intent: StockTradeIntent
    user_id: int
    chat_id: int
    message_id: int
    created_at: float
    timeout_sec: int
    result: Optional[str] = None  # "confirmed" | "rejected" | "timeout"


class SemiAutoConfirmationManager:
    """Manages the semi-auto trade confirmation flow via Telegram.

    When a strategy produces a ``StockTradeIntent`` in semi-auto mode, the
    engine calls ``request_confirmation``.  This sends a Telegram message with
    Confirm / Reject / Modify inline buttons and waits asynchronously for the
    user response (or timeout).

    The actual message-sending is delegated to a ``send_fn`` callback so that
    this class stays independent of aiogram types.
    """

    def __init__(
        self,
        send_fn: Callable[..., Coroutine[Any, Any, Dict[str, Any]]],
        edit_fn: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
        timeout_sec: int = 60,
    ) -> None:
        self._send_fn = send_fn
        self._edit_fn = edit_fn
        self._timeout_sec = timeout_sec
        self._pending: Dict[str, PendingConfirmation] = {}
        self._events: Dict[str, asyncio.Event] = {}

    async def request_confirmation(
        self, intent: StockTradeIntent, user_id: int,
        current_price: float = 0.0, lot_size: int = 1,
    ) -> Optional[StockTradeIntent]:
        """Send a trade proposal and wait for user action.

        Returns the (possibly modified) intent if confirmed, ``None`` otherwise.
        """
        intent_id = str(uuid.uuid4())[:8]

        # Формируем текст сообщения.
        side_ru = "ПОКУПКА" if intent.side == "buy" else "ПРОДАЖА"
        side_arrow = "\u2b06\ufe0f" if intent.side == "buy" else "\u2b07\ufe0f"
        price_str = f"{current_price:,.2f}" if current_price else "?"

        shares = intent.quantity_lots * lot_size
        notional = current_price * shares if current_price else 0

        lot_info = f"x{intent.quantity_lots} лот(ов)"
        if lot_size > 1:
            lot_info += f" ({shares} шт. по {lot_size} в лоте)"

        text = (
            f"{side_arrow} <b>{side_ru} {intent.ticker}</b>\n"
            f"Стратегия: {intent.strategy_id.value}\n"
            f"Цена: {price_str} \u20bd  {lot_info}\n"
            f"Сумма: ~{notional:,.0f} \u20bd\n"
            f"Уверенность: {intent.confidence:.0%}\n"
            f"Ожид. доход: {intent.expected_edge_pct:.2f}%\n"
            f"SL: {intent.stop_loss_pct:.1f}%  |  TP: {intent.take_profit_pct:.1f}%\n"
            f"Таймаут: {self._timeout_sec} сек"
        )
        if intent.metadata:
            extras = "  ".join(f"{k}={v}" for k, v in intent.metadata.items())
            text += f"\n<i>{extras}</i>"

        buttons = [
            [
                {"text": "\u2705 Подтвердить", "callback_data": f"stock_confirm:{intent_id}"},
                {"text": "\u274c Отклонить", "callback_data": f"stock_reject:{intent_id}"},
            ],
        ]

        # Send via callback.
        msg_info = await self._send_fn(user_id, text, buttons)
        chat_id = msg_info.get("chat_id", user_id)
        message_id = msg_info.get("message_id", 0)

        pending = PendingConfirmation(
            intent_id=intent_id,
            intent=intent,
            user_id=user_id,
            chat_id=chat_id,
            message_id=message_id,
            created_at=time.time(),
            timeout_sec=self._timeout_sec,
        )
        event = asyncio.Event()
        self._pending[intent_id] = pending
        self._events[intent_id] = event

        # Wait for user action or timeout.
        try:
            await asyncio.wait_for(event.wait(), timeout=self._timeout_sec)
        except asyncio.TimeoutError:
            pending.result = "timeout"
            logger.info("confirmation: timeout for %s %s", intent_id, intent.ticker)
            # Notify user that the proposal expired.
            if self._edit_fn and pending.chat_id and pending.message_id:
                try:
                    await self._edit_fn(pending.chat_id, pending.message_id, "\u23f0 Таймаут — пропущено")
                except Exception:
                    pass

        self._events.pop(intent_id, None)
        result_pending = self._pending.pop(intent_id, pending)

        if result_pending.result == "confirmed":
            return result_pending.intent
        return None

    def on_confirm(self, intent_id: str) -> bool:
        """Called by Telegram callback handler when user confirms."""
        pending = self._pending.get(intent_id)
        if pending is None:
            return False
        pending.result = "confirmed"
        event = self._events.get(intent_id)
        if event:
            event.set()
        return True

    def on_reject(self, intent_id: str) -> bool:
        """Called by Telegram callback handler when user rejects."""
        pending = self._pending.get(intent_id)
        if pending is None:
            return False
        pending.result = "rejected"
        event = self._events.get(intent_id)
        if event:
            event.set()
        return True

    def on_modify(self, intent_id: str, new_lots: int) -> bool:
        """Called when user modifies the trade size."""
        pending = self._pending.get(intent_id)
        if pending is None:
            return False
        # Rebuild intent with modified quantity.
        old = pending.intent
        pending.intent = StockTradeIntent(
            strategy_id=old.strategy_id,
            ticker=old.ticker,
            side=old.side,
            quantity_lots=new_lots,
            order_type=old.order_type,
            limit_price=old.limit_price,
            confidence=old.confidence,
            expected_edge_pct=old.expected_edge_pct,
            stop_loss_pct=old.stop_loss_pct,
            take_profit_pct=old.take_profit_pct,
            mode=old.mode,
            metadata=old.metadata,
        )
        pending.result = "confirmed"
        event = self._events.get(intent_id)
        if event:
            event.set()
        return True

    @property
    def pending_count(self) -> int:
        return len(self._pending)
