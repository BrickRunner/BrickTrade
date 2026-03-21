from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional

import websockets

from stocks.exchange.bcs_auth import BcsTokenManager
from stocks.system.models import CandleBar, StockQuote

logger = logging.getLogger(__name__)

_MARKET_DATA_URL = (
    "wss://ws.broker.ru/trade-api-market-data-connector/api/v1/market-data/ws"
)
_RECONNECT_DELAY = 3.0
_PING_INTERVAL = 20.0

Callback = Callable[..., Coroutine[Any, Any, None]]


class BcsMarketDataWs:
    """WebSocket client for BCS real-time market data.

    Supports orderbook, quotes, trades, and candle subscriptions through a
    single WebSocket connection.
    """

    def __init__(self, token_manager: BcsTokenManager) -> None:
        self._tm = token_manager
        self._ws: Optional[Any] = None
        self._running = False
        self._subscriptions: List[Dict[str, Any]] = []
        self._quote_callbacks: Dict[str, Callback] = {}
        self._orderbook_callbacks: Dict[str, Callback] = {}
        self._trade_callbacks: Dict[str, Callback] = {}
        self._candle_callbacks: Dict[str, Callback] = {}

    # ------------------------------------------------------------------
    # Subscription registration (call before connect)
    # ------------------------------------------------------------------

    def subscribe_quotes(
        self, ticker: str, class_code: str, callback: Callback
    ) -> None:
        self._quote_callbacks[ticker] = callback
        self._subscriptions.append(
            {
                "subscribeType": 0,
                "dataType": 3,
                "instruments": [{"ticker": ticker, "classCode": class_code}],
            }
        )

    def subscribe_orderbook(
        self, ticker: str, class_code: str, depth: int, callback: Callback
    ) -> None:
        self._orderbook_callbacks[ticker] = callback
        self._subscriptions.append(
            {
                "subscribeType": 0,
                "dataType": 0,
                "instruments": [{"ticker": ticker, "classCode": class_code}],
                "depth": depth,
            }
        )

    def subscribe_trades(
        self, ticker: str, class_code: str, callback: Callback
    ) -> None:
        self._trade_callbacks[ticker] = callback
        self._subscriptions.append(
            {
                "subscribeType": 0,
                "dataType": 2,
                "instruments": [{"ticker": ticker, "classCode": class_code}],
            }
        )

    def subscribe_candles(
        self, ticker: str, class_code: str, timeframe: str, callback: Callback
    ) -> None:
        self._candle_callbacks[ticker] = callback
        self._subscriptions.append(
            {
                "subscribeType": 0,
                "dataType": 1,
                "instruments": [{"ticker": ticker, "classCode": class_code}],
                "timeFrame": timeframe,
            }
        )

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect and begin streaming. Reconnects on failure."""
        self._running = True
        while self._running:
            try:
                await self._connect_and_stream()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("bcs_ws: connection lost (%s), reconnecting...", exc)
                await asyncio.sleep(_RECONNECT_DELAY)

    async def _connect_and_stream(self) -> None:
        token = await self._tm.get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        async with websockets.connect(
            _MARKET_DATA_URL,
            extra_headers=headers,
            ping_interval=_PING_INTERVAL,
        ) as ws:
            self._ws = ws
            logger.info("bcs_ws: connected")

            # Send subscriptions
            for sub in self._subscriptions:
                await ws.send(json.dumps(sub))
                logger.debug("bcs_ws: subscribed %s", sub)

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._dispatch(msg)
                except Exception as exc:
                    logger.warning("bcs_ws: dispatch error: %s", exc)

    async def _dispatch(self, msg: Dict[str, Any]) -> None:
        data_type = msg.get("dataType")
        ticker = msg.get("ticker", "")

        if data_type == 3:  # quotes
            cb = self._quote_callbacks.get(ticker)
            if cb:
                quote = StockQuote(
                    ticker=ticker,
                    bid=float(msg.get("bid", 0)),
                    ask=float(msg.get("offer", 0)),
                    last=float(msg.get("last", 0)),
                    volume=float(msg.get("volume", 0) if "volume" in msg else 0),
                    timestamp=time.time(),
                )
                await cb(quote)

        elif data_type == 0:  # orderbook
            cb = self._orderbook_callbacks.get(ticker)
            if cb:
                await cb(msg)

        elif data_type == 2:  # trades
            cb = self._trade_callbacks.get(ticker)
            if cb:
                await cb(msg)

        elif data_type == 1:  # candle
            cb = self._candle_callbacks.get(ticker)
            if cb:
                candle = CandleBar(
                    timestamp=float(msg.get("dateTime", time.time())),
                    open=float(msg.get("open", 0)),
                    high=float(msg.get("high", 0)),
                    low=float(msg.get("low", 0)),
                    close=float(msg.get("close", 0)),
                    volume=float(msg.get("volume", 0)),
                )
                await cb(candle)

    async def close(self) -> None:
        self._running = False
        if self._ws:
            await self._ws.close()
