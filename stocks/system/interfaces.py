from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from stocks.system.models import StockExecutionReport, StockSnapshot, StockTradeIntent


class StockMarketDataProvider(Protocol):
    async def get_snapshot(self, ticker: str) -> StockSnapshot: ...
    async def health(self) -> Dict[str, float]: ...


class StockExecutionVenue(Protocol):
    async def place_order(
        self,
        ticker: str,
        side: str,
        quantity_lots: int,
        order_type: str,
        limit_price: float = 0.0,
    ) -> Dict[str, Any]: ...

    async def cancel_order(self, order_id: str) -> None: ...
    async def get_order(self, order_id: str) -> Dict[str, Any]: ...
    async def get_portfolio(self) -> Dict[str, Any]: ...
    async def get_positions(self) -> Dict[str, int]: ...


class StockStrategy(Protocol):
    @property
    def strategy_id(self) -> str: ...
    async def on_snapshot(self, snapshot: StockSnapshot) -> List[StockTradeIntent]: ...


class ConfirmationSink(Protocol):
    """Semi-auto mode: send trade proposal to user, await response."""
    async def request_confirmation(
        self, intent: StockTradeIntent, user_id: int
    ) -> Optional[StockTradeIntent]: ...
