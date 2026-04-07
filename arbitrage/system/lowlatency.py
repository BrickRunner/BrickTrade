from __future__ import annotations

import os
from dataclasses import dataclass
import asyncio
from typing import Dict, Any

import aiohttp

from arbitrage.system.interfaces import ExecutionVenue


@dataclass
class LowLatencyExecutionVenue(ExecutionVenue):
    base_url: str = os.getenv("LOWLATENCY_URL", "http://127.0.0.1:8089")
    _session: aiohttp.ClientSession | None = None

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def place_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity_usd: float,
        order_type: str,
        limit_price: float = 0.0,
        quantity_contracts: float | None = None,
        offset: str = "open",
    ) -> Dict:
        payload = {
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "quantity_usd": quantity_usd,
            "order_type": order_type,
            "limit_price": limit_price,
            "quantity_contracts": quantity_contracts,
            "offset": offset,
        }
        session = await self._session_get()
        async with session.post(f"{self.base_url}/execute", json=payload, timeout=5) as resp:
            return await resp.json()

    async def place_spot_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity_base: float,
        order_type: str,
        limit_price: float = 0.0,
    ) -> Dict:
        payload = {
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "quantity_base": quantity_base,
            "order_type": order_type,
            "limit_price": limit_price,
        }
        session = await self._session_get()
        async with session.post(f"{self.base_url}/spot_execute", json=payload, timeout=5) as resp:
            return await resp.json()

    async def place_oco_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity: float,
        *,
        tp_trigger: float,
        tp_price: float,
        sl_trigger: float,
        sl_price: float,
        spot: bool = False,
        reduce_only: bool = False,
    ) -> Dict:
        return {"success": False, "message": "oco_not_supported_lowlatency", "exchange": exchange}

    async def place_rfq(self, exchange: str, payload: Dict) -> Dict:
        return {"success": False, "message": "rfq_not_supported_lowlatency", "exchange": exchange}

    async def cancel_order(self, exchange: str, order_id: str) -> None:
        return None

    async def get_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        session = await self._session_get()
        async with session.post(f"{self.base_url}/order", json={"exchange": exchange, "symbol": symbol, "order_id": order_id}, timeout=5) as resp:
            return await resp.json()

    async def get_spot_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        session = await self._session_get()
        async with session.post(f"{self.base_url}/spot_order", json={"exchange": exchange, "symbol": symbol, "order_id": order_id}, timeout=5) as resp:
            return await resp.json()

    async def wait_for_fill(
        self,
        exchange: str,
        symbol: str,
        order_id: str,
        timeout_ms: int,
        *,
        spot: bool = False,
        expected_size: float | None = None,
    ) -> bool:
        if not order_id:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (timeout_ms / 1000)
        while loop.time() < deadline:
            try:
                if spot:
                    result = await self.get_spot_order(exchange, symbol, order_id)
                    if self._order_filled(exchange, result, expected_size):
                        return True
                else:
                    result = await self.get_order(exchange, symbol, order_id)
                    if self._order_filled(exchange, result, expected_size):
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.2)
        return False

    @staticmethod
    def _order_filled(exchange: str, response: Dict[str, Any], expected_size: float | None) -> bool:
        if exchange == "okx":
            data = response.get("data") or []
            if isinstance(data, list) and data:
                state = data[0].get("state")
                filled = float(data[0].get("accFillSz", 0) or 0)
                return state in {"filled", "2"} and (expected_size is None or filled >= expected_size * 0.98)
        if exchange == "bybit":
            data = response.get("result", {})
            filled = float(data.get("cumExecQty", 0) or 0)
            status = str(data.get("orderStatus") or "")
            return status in {"Filled", "filled"} and (expected_size is None or filled >= expected_size * 0.98)
        return False

    async def get_balances(self) -> Dict[str, float]:
        session = await self._session_get()
        async with session.get(f"{self.base_url}/balances", timeout=5) as resp:
            data = await resp.json()
            return {k: float(v) for k, v in data.items()} if isinstance(data, dict) else {}
