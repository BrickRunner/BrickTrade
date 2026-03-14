from __future__ import annotations

from typing import Dict, List, Protocol

from arbitrage.system.models import MarketSnapshot


class MarketDataProvider(Protocol):
    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        ...

    async def health(self) -> Dict[str, float]:
        ...


class ExecutionVenue(Protocol):
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
        ...

    async def place_spot_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity_base: float,
        order_type: str,
        limit_price: float = 0.0,
    ) -> Dict:
        ...

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
        ...

    async def place_rfq(self, exchange: str, payload: Dict) -> Dict:
        ...

    async def cancel_order(self, exchange: str, order_id: str) -> None:
        ...

    async def get_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        ...

    async def get_spot_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        ...

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
        ...

    async def get_balances(self) -> Dict[str, float]:
        ...


class MonitoringSink(Protocol):
    async def emit(self, event: str, payload: Dict) -> None:
        ...


class Strategy(Protocol):
    @property
    def strategy_id(self) -> str:
        ...

    async def on_market_snapshot(self, snapshot: MarketSnapshot) -> List:
        ...
