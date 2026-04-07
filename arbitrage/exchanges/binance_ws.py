"""
Binance USDT-M Futures WebSocket client for orderbook streaming.

Docs: https://binance-docs.github.io/apidocs/futures/en/#websocket-market-streams
Stream URL: wss://fstream.binance.com/ws/<streamName>

FIX CRITICAL C: Replaced 'async for message in ws' pattern with explicit
asyncio.wait_for(ws.recv(), timeout=30) because the 'async for' pattern
can silently exit on some websockets library versions when the connection
dies without raising ConnectionClosed — the classic "silent death" bug.
"""
import asyncio
import json
import time
from typing import Callable, Optional, Dict, Any

import websockets
from websockets.client import WebSocketClientProtocol

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("binance_ws")

WS_URL = "wss://fstream.binance.com/ws"
TESTNET_WS_URL = "wss://stream.binancefuture.com/ws"


class BinanceWebSocket:
    """WebSocket client for Binance USDT-M Futures orderbook"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.testnet = testnet
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self.callback: Optional[Callable] = None
        # FIX C2: heartbeat tracking
        self._last_msg_ts: float = 0.0

        # Binance uses lowercase symbol in stream names
        self.stream_symbol = symbol.lower()
        if testnet:
            self.ws_url = f"{TESTNET_WS_URL}/{self.stream_symbol}@depth5@100ms"
        else:
            self.ws_url = f"{WS_URL}/{self.stream_symbol}@depth5@100ms"

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
                logger.info(f"Connecting to Binance WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                ) as ws:
                    self.ws = ws
                    self._last_msg_ts = time.monotonic()  # FIX C2: heartbeat
                    logger.info(f"Connected to Binance WebSocket for {self.symbol}")

                    # FIX C2: Explicit recv loop with heartbeat + callback isolation
                    while self.running:
                        # Heartbeat check: if >60s since last msg, reconnect
                        if time.monotonic() - self._last_msg_ts > 60:
                            logger.warning(
                                "Binance WebSocket: heartbeat timeout (%.0fs), "
                                "reconnecting (symbol=%s)",
                                time.monotonic() - self._last_msg_ts,
                                self.symbol,
                            )
                            break
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Binance WebSocket: no message in 30s, "
                                "reconnecting (symbol=%s)", self.symbol,
                            )
                            break
                        except websockets.exceptions.ConnectionClosed:
                            logger.warning(
                                "Binance WebSocket: connection closed mid-loop"
                            )
                            break
                        except Exception as e:
                            logger.error(f"Error receiving Binance message: {e}", exc_info=True)
                            break

                        try:
                            data = json.loads(message)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode Binance message: {e}")
                            continue

                        # Callback isolation: exceptions in handler do NOT break recv loop
                        try:
                            await self._handle_message(data)
                        except Exception as e:
                            logger.error(f"Binance _handle_message error: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Binance WebSocket connection closed")
                if self.running:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                logger.info("Binance WebSocket: cancelled")
                break
            except Exception as e:
                logger.error(f"Binance WebSocket error: {e}")
                if self.running:
                    await asyncio.sleep(2)
            finally:
                self.ws = None

    def is_connected(self) -> bool:
        """Check connection + recent heartbeat."""
        if self.ws is None or not self.ws.open:
            return False
        # FIX C2: Also verify recent activity — a zombie connection reports open
        # but hasn't received data in a while.
        if time.monotonic() - self._last_msg_ts > 60:
            return False
        return True

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming orderbook message"""
        self._last_msg_ts = time.monotonic()  # FIX C2: update heartbeat
        # Binance partial depth stream format:
        # {"lastUpdateId": 123, "E": timestamp, "T": timestamp, "bids": [...], "asks": [...]}
        if "bids" in data and "asks" in data:
            orderbook = {
                "exchange": "binance",
                "symbol": self.symbol,
                "bids": [[float(price), float(qty)] for price, qty in data["bids"]],
                "asks": [[float(price), float(qty)] for price, qty in data["asks"]],
                "timestamp": int(data.get("E", data.get("T", 0))),
            }

            if self.callback:
                await self.callback(orderbook)

    async def disconnect(self) -> None:
        """Disconnect from WebSocket"""
        logger.info("Disconnecting from Binance WebSocket")
        self.running = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.error(f"Error closing Binance WebSocket: {e}")

        self.ws = None
        logger.info("Disconnected from Binance WebSocket")

    def is_connected(self) -> bool:
        """Check connection + recent heartbeat."""
        if self.ws is None or not self.ws.open:
            return False
        # FIX C2: Also verify recent activity — a zombie connection reports open
        # but hasn't received data in a while.
        if time.monotonic() - self._last_msg_ts > 60:
            return False
        return True
