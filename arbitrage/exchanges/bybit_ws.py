"""
Bybit V5 WebSocket client for orderbook streaming.

Docs: https://bybit-exchange.github.io/docs/v5/ws/connect
Public WS URL: wss://stream.bybit.com/v5/public/linear
"""
import asyncio
import json
from typing import Callable, Optional, Dict, Any

import websockets
from websockets.client import WebSocketClientProtocol

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("bybit_ws")

WS_URL = "wss://stream.bybit.com/v5/public/linear"
TESTNET_WS_URL = "wss://stream-testnet.bybit.com/v5/public/linear"


class BybitWebSocket:
    """WebSocket client for Bybit V5 linear perpetuals orderbook"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.testnet = testnet
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self.callback: Optional[Callable] = None

        if testnet:
            self.ws_url = TESTNET_WS_URL
        else:
            self.ws_url = WS_URL

    async def connect(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Connect to WebSocket and subscribe to orderbook.

        Args:
            callback: Function to handle orderbook data
        """
        self.callback = callback
        self.running = True

        while self.running:
            try:
                logger.info(f"Connecting to Bybit WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self.ws = ws
                    logger.info("Connected to Bybit WebSocket")

                    # Subscribe to orderbook (depth 5)
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [f"orderbook.5.{self.symbol}"],
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"Subscribed to Bybit orderbook: {self.symbol}")

                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            await self._handle_message(data)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode Bybit message: {e}")
                        except Exception as e:
                            logger.error(f"Error handling Bybit message: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Bybit WebSocket connection closed")
                if self.running:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Bybit WebSocket error: {e}")
                if self.running:
                    await asyncio.sleep(2)
            finally:
                self.ws = None

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming messages"""
        # Subscription confirmation
        if data.get("op") == "subscribe":
            if data.get("success"):
                logger.info(f"Bybit subscription confirmed: {data.get('conn_id', '')}")
            else:
                logger.error(f"Bybit subscription error: {data}")
            return

        # Heartbeat pong
        if data.get("op") == "pong":
            return

        # Orderbook snapshot or delta
        topic = data.get("topic", "")
        if "orderbook" in topic and "data" in data:
            ob_data = data["data"]
            bids = ob_data.get("b", [])
            asks = ob_data.get("a", [])

            if bids or asks:
                orderbook = {
                    "exchange": "bybit",
                    "symbol": self.symbol,
                    "bids": [[float(price), float(qty)] for price, qty in bids],
                    "asks": [[float(price), float(qty)] for price, qty in asks],
                    "timestamp": int(data.get("ts", 0)),
                }

                if self.callback:
                    await self.callback(orderbook)

    async def disconnect(self) -> None:
        """Disconnect from WebSocket"""
        logger.info("Disconnecting from Bybit WebSocket")
        self.running = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.error(f"Error closing Bybit WebSocket: {e}")

        self.ws = None
        logger.info("Disconnected from Bybit WebSocket")

    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.open
