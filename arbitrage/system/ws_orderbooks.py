from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, Optional

from arbitrage.exchanges.htx_ws import HTXWebSocket
from arbitrage.exchanges.okx_ws import OKXWebSocket
from arbitrage.system.models import OrderBookSnapshot


@dataclass
class WsOrderbookCache:
    symbols: Iterable[str]
    exchanges: Iterable[str]
    _orderbooks: Dict[str, Dict[str, OrderBookSnapshot]] = field(default_factory=dict)
    _tasks: list[asyncio.Task] = field(default_factory=list)
    _stale_after_sec: float = 3.0

    async def start(self) -> None:
        for exchange in self.exchanges:
            if exchange not in {"okx", "htx"}:
                continue
            for symbol in self.symbols:
                if exchange == "okx":
                    ws = OKXWebSocket(symbol=symbol)
                else:
                    ws = HTXWebSocket(symbol=symbol)
                task = asyncio.create_task(self._run_ws(ws, exchange, symbol))
                self._tasks.append(task)

    async def _run_ws(self, ws, exchange: str, symbol: str) -> None:
        async def _on_book(book: Dict) -> None:
            bids = book.get("bids") or []
            asks = book.get("asks") or []
            if not bids or not asks:
                return
            best_bid = float(bids[0][0])
            best_ask = float(asks[0][0])
            if best_bid <= 0 or best_ask <= 0:
                return
            snapshot = OrderBookSnapshot(
                exchange=exchange,
                symbol=symbol,
                bid=best_bid,
                ask=best_ask,
                timestamp=time.time(),
            )
            self._orderbooks.setdefault(exchange, {})[symbol] = snapshot

        await ws.connect(_on_book)

    def get(self, exchange: str, symbol: str) -> Optional[OrderBookSnapshot]:
        snapshot = self._orderbooks.get(exchange, {}).get(symbol)
        if not snapshot:
            return None
        if time.time() - snapshot.timestamp > self._stale_after_sec:
            return None
        return snapshot
