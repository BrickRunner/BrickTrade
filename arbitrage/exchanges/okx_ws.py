"""
OKX WebSocket клиент для получения стаканов
"""
import asyncio
import json
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
                    logger.info(f"Subscribed to OKX orderbook: {self.okx_symbol}")

                    # Обработка сообщений
                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            await self._handle_message(data)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode OKX message: {e}")
                        except Exception as e:
                            logger.error(f"Error handling OKX message: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("OKX WebSocket connection closed")
                if self.running:
                    logger.info("Reconnecting in 3 seconds...")
                    await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"OKX WebSocket error: {e}", exc_info=True)
                if self.running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)
            finally:
                self.ws = None

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Обработка входящих сообщений"""
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
