"""
Bybit V5 WebSocket client for orderbook streaming.

Docs: https://bybit-exchange.github.io/docs/v5/ws/connect
Public WS URL: wss://stream.bybit.com/v5/public/linear
"""
import asyncio
import json
import time
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
        # FIX C2: heartbeat tracking
        self._last_msg_ts: float = 0.0

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
                    self._last_msg_ts = time.monotonic()  # FIX C2: heartbeat
                    logger.info("Connected to Bybit WebSocket")

                    # Subscribe to orderbook (depth 5)
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": [f"orderbook.5.{self.symbol}"],
                    }
                    await ws.send(json.dumps(subscribe_msg))
                    logger.info(f"Subscribed to Bybit orderbook: {self.symbol}")

                    # FIX C2: Explicit recv loop with heartbeat + callback isolation
                    while self.running:
                        # Heartbeat check: >60s without message → reconnect
                        if time.monotonic() - self._last_msg_ts > 60:
                            logger.warning(
                                "Bybit WebSocket: heartbeat timeout (%.0fs), reconnecting",
                                time.monotonic() - self._last_msg_ts,
                            )
                            break
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Bybit WebSocket: no message in 30s, reconnecting",
                            )
                            break
                        except websockets.exceptions.ConnectionClosed:
                            break
                        except Exception as e:
                            logger.error(f"Error receiving Bybit message: {e}", exc_info=True)
                            break
                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode Bybit message: {e}")
                            continue

                        # Callback isolation: exceptions in handler do NOT break recv loop
                        try:
                            await self._handle_message(data)
                        except Exception as e:
                            logger.error(f"Bybit _handle_message error: {e}", exc_info=True)

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
        self._last_msg_ts = time.monotonic()  # FIX C2: update heartbeat
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
        """Check connection + heartbeat."""
        if self.ws is None or not self.ws.open:
            return False
        if time.monotonic() - self._last_msg_ts > 60:
            return False
        return True
