"""
OKX WebSocket клиент для получения стаканов
"""
import asyncio
import json
import time
from typing import Callable, Optional, Dict, Any
import websockets
from websockets.client import WebSocketClientProtocol

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("okx_ws")


class OKXWebSocket:
    """WebSocket клиент для OKX"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.testnet = testnet
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self.callback: Optional[Callable] = None
        # FIX C2/H3: heartbeat tracking and subscription state
        self._last_msg_ts: float = 0.0
        self._subscribed: bool = False

        # URLs
        if testnet:
            self.ws_url = "wss://wspap.okx.com:8443/ws/v5/public?brokerId=9999"
        else:
            self.ws_url = "wss://ws.okx.com:8443/ws/v5/public"

        # Форматирование символа для OKX (BTC-USDT-SWAP)
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            self.okx_symbol = f"{base}-USDT-SWAP"
        else:
            self.okx_symbol = f"{symbol}-SWAP"

    async def connect(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Подключение к WebSocket и подписка на orderbook

        Args:
            callback: Функция обработки данных стакана
        """
        self.callback = callback
        self.running = True

        while self.running:
            try:
                logger.info(f"Connecting to OKX WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:
                    self.ws = ws
                    self._last_msg_ts = time.monotonic()  # FIX C2: heartbeat tracking
                    logger.info("Connected to OKX WebSocket")

                    # Подписка на orderbook
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [
                            {
                                "channel": "books5",
                                "instId": self.okx_symbol
                            }
                        ]
                    }

                    await ws.send(json.dumps(subscribe_msg))
                    # FIX H3: Wait for subscription confirmation before entering recv loop
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                        resp = json.loads(raw)
                        if "event" in resp:
                            if resp["event"] == "error":
                                logger.error(f"OKX subscription error: {resp}")
                                await asyncio.sleep(2)
                                continue  # reconnect
                            elif resp["event"] == "subscribe":
                                logger.info(f"OKX subscription confirmed: {self.okx_symbol}")
                                self._subscribed = True
                            # Snapshot snapshot message may also be the first data
                            pass
                        else:
                            # Data message — process it as first snapshot
                            self._subscribed = True
                            try:
                                await self._handle_message(resp)
                            except Exception as e:
                                logger.error(f"OKX initial snapshot error: {e}")
                    except asyncio.TimeoutError:
                        logger.warning("OKX subscription confirmation timeout, reconnecting")
                        continue

                    # FIX C2: Explicit recv loop with heartbeat + timeout
                    while self.running:
                        try:
                            # Heartbeat check: if >60s since last mesg, reconnect
                            if time.monotonic() - self._last_msg_ts > 60:
                                logger.warning(
                                    "OKX WebSocket: heartbeat timeout (%.0fs), reconnecting",
                                    time.monotonic() - self._last_msg_ts,
                                )
                                break
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "OKX WebSocket: no message in 30s, reconnecting",
                            )
                            break
                        except websockets.exceptions.ConnectionClosed:
                            break
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode OKX message: {e}")
                            continue

                        # Callback isolation: exceptions in handler do NOT break recv loop
                        try:
                            await self._handle_message(data)
                        except Exception as e:
                            logger.error(f"OKX _handle_message error: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("OKX WebSocket connection closed")
                if self.running:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"OKX WebSocket error: {e}")
                if self.running:
                    await asyncio.sleep(2)
            finally:
                self.ws = None
                self._subscribed = False

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Обработка входящих сообщений"""
        self._last_msg_ts = time.monotonic()  # FIX C2: update heartbeat on every message
        # Пропускаем служебные сообщения
        if "event" in data:
            if data["event"] == "subscribe":
                logger.info(f"OKX subscription confirmed: {data}")
            elif data["event"] == "error":
                logger.error(f"OKX subscription error: {data}")
            return

        # Обработка данных orderbook
        if "data" in data and len(data["data"]) > 0:
            orderbook_data = data["data"][0]

            if "bids" in orderbook_data and "asks" in orderbook_data:
                # Форматируем в общий вид
                orderbook = {
                    "exchange": "okx",
                    "symbol": self.symbol,
                    "bids": [[float(price), float(qty)] for price, qty, _, _ in orderbook_data["bids"]],
                    "asks": [[float(price), float(qty)] for price, qty, _, _ in orderbook_data["asks"]],
                    "timestamp": int(orderbook_data.get("ts", 0))
                }

                if self.callback:
                    await self.callback(orderbook)

    async def disconnect(self) -> None:
        """Отключение от WebSocket"""
        logger.info("Disconnecting from OKX WebSocket")
        self.running = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.error(f"Error closing OKX WebSocket: {e}")

        self.ws = None
        logger.info("Disconnected from OKX WebSocket")

    def is_connected(self) -> bool:
        """Проверка состояния подключения"""
        return self.ws is not None and self.ws.open
