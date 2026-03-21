"""
Binance USDT-M Futures WebSocket client for orderbook streaming.

Docs: https://binance-docs.github.io/apidocs/futures/en/#websocket-market-streams
Stream URL: wss://fstream.binance.com/ws/<streamName>
"""
import asyncio
import json
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
                    logger.info(f"Connected to Binance WebSocket for {self.symbol}")

                    async for message in ws:
                        if not self.running:
                            break

                        try:
                            data = json.loads(message)
                            await self._handle_message(data)
                        except json.JSONDecodeError as e:
                            logger.error(f"Failed to decode Binance message: {e}")
                        except Exception as e:
                            logger.error(f"Error handling Binance message: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Binance WebSocket connection closed")
                if self.running:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"Binance WebSocket error: {e}")
                if self.running:
                    await asyncio.sleep(2)
            finally:
                self.ws = None

    async def _handle_message(self, data: Dict[str, Any]) -> None:
        """Handle incoming orderbook message"""
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
        return self.ws is not None and self.ws.open
