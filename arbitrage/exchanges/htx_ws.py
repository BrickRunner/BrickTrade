"""
HTX (Huobi) WebSocket клиент для получения стаканов ордеров.

Линейные своп-контракты (USDT-margined perpetuals):
  WS URL: wss://api.hbdm.com/linear-swap-ws
  Подписка: {"sub": "market.BTC-USDT.depth.step0", "id": "htx_ws_1"}
  Сжатие: gzip (каждое сообщение нужно декомпрессировать)

Документация: https://www.htx.com/en-us/opend/newApiPages/
"""
import asyncio
import gzip
import json
from typing import Callable, Optional, Dict, Any

import websockets
from websockets.client import WebSocketClientProtocol

from arbitrage.utils import get_arbitrage_logger, usdt_to_htx as _usdt_to_htx

logger = get_arbitrage_logger("htx_ws")

# HTX Linear Swap WebSocket URL
WS_URL = "wss://api.hbdm.com/linear-swap-ws"


class HTXWebSocket:
    """WebSocket клиент для HTX (линейные свопы)"""

    def __init__(self, symbol: str, testnet: bool = False):
        self.symbol = symbol
        self.htx_symbol = _usdt_to_htx(symbol)  # BTC-USDT
        self.testnet = testnet
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self.callback: Optional[Callable] = None
        self._ping_id = 0

        # HTX не имеет официального тестнета для свопов — используем основной URL
        self.ws_url = WS_URL

    async def connect(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        """
        Подключение к WebSocket и подписка на orderbook.

        Args:
            callback: Функция обработки данных стакана.
                      Принимает dict: {exchange, symbol, bids, asks, timestamp}
        """
        self.callback = callback
        self.running = True

        while self.running:
            try:
                logger.info(f"Connecting to HTX WebSocket: {self.ws_url}")
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,   # HTX использует собственный heartbeat
                    ping_timeout=None,
                    close_timeout=5,
                ) as ws:
                    self.ws = ws
                    logger.info("Connected to HTX WebSocket")

                    # Подписка на стакан (step0 = все уровни, step5 = 20 уровней)
                    sub_msg = {
                        "sub": f"market.{self.htx_symbol}.depth.step0",
                        "id": f"htx_depth_{self.htx_symbol}"
                    }
                    await ws.send(json.dumps(sub_msg))
                    logger.info(f"Subscribed to HTX orderbook: {self.htx_symbol}")

                    # Обработка сообщений
                    async for raw in ws:
                        if not self.running:
                            break
                        try:
                            data = self._decompress(raw)
                            await self._handle_message(ws, data)
                        except Exception as e:
                            logger.error(f"Error handling HTX message: {e}", exc_info=True)

            except websockets.exceptions.ConnectionClosed:
                logger.warning("HTX WebSocket connection closed")
                if self.running:
                    await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"HTX WebSocket error: {e}")
                if self.running:
                    await asyncio.sleep(2)
            finally:
                self.ws = None

    def _decompress(self, raw: bytes) -> Dict[str, Any]:
        """Декомпрессировать gzip-сообщение HTX и распарсить JSON"""
        if isinstance(raw, bytes):
            raw = gzip.decompress(raw)
        return json.loads(raw)

    async def _handle_message(self, ws, data: Dict[str, Any]) -> None:
        """Обработка входящего сообщения"""

        # Heartbeat — HTX присылает {"ping": <ts>}, нужно ответить {"pong": <ts>}
        if "ping" in data:
            pong = {"pong": data["ping"]}
            try:
                await ws.send(json.dumps(pong))
            except Exception:
                pass
            return

        # Ответ на подписку
        if "subbed" in data:
            if data.get("status") == "ok":
                logger.info(f"HTX subscription confirmed: {data.get('subbed')}")
            else:
                logger.error(f"HTX subscription error: {data}")
            return

        # Обновление стакана
        if "ch" in data and "depth" in data.get("ch", "") and "tick" in data:
            tick = data["tick"]
            bids_raw = tick.get("bids", [])
            asks_raw = tick.get("asks", [])

            # HTX присылает [[price, qty], ...] уже отсортированные
            # bids по убыванию цены, asks по возрастанию
            orderbook = {
                "exchange": "htx",
                "symbol": self.symbol,
                "bids": [[float(p), float(q)] for p, q in bids_raw[:20]],
                "asks": [[float(p), float(q)] for p, q in asks_raw[:20]],
                "timestamp": int(data.get("ts", 0)),
            }

            if self.callback:
                await self.callback(orderbook)

    async def disconnect(self) -> None:
        """Отключение от WebSocket"""
        logger.info("Disconnecting from HTX WebSocket")
        self.running = False

        if self.ws:
            try:
                await self.ws.close()
            except Exception as e:
                logger.error(f"Error closing HTX WebSocket: {e}")

        self.ws = None
        logger.info("Disconnected from HTX WebSocket")

    def is_connected(self) -> bool:
        """Проверка состояния подключения"""
        return self.ws is not None and not self.ws.closed
