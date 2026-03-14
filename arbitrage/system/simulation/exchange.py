from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class SimulatedExecutionVenue:
    slippage_bps: float = 3.0
    fee_bps: float = 2.0
    fills: List[Dict] = field(default_factory=list)

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
        if quantity_usd <= 0:
            return {"success": False, "message": "invalid_quantity", "exchange": exchange}
        multiplier = 1 + (self.slippage_bps + self.fee_bps) / 10_000
        fill_price = (limit_price if limit_price > 0 else 1.0) * multiplier
        fill = {
            "success": True,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "notional_usd": quantity_usd,
            "fill_price": fill_price,
            "order_type": order_type,
        }
        self.fills.append(fill)
        return fill

    async def place_spot_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity_base: float,
        order_type: str,
        limit_price: float = 0.0,
    ) -> Dict:
        if quantity_base <= 0:
            return {"success": False, "message": "invalid_quantity", "exchange": exchange}
        multiplier = 1 + (self.slippage_bps + self.fee_bps) / 10_000
        fill_price = (limit_price if limit_price > 0 else 1.0) * multiplier
        fill = {
            "success": True,
            "exchange": exchange,
            "symbol": symbol,
            "side": side,
            "quantity_base": quantity_base,
            "fill_price": fill_price,
            "order_type": order_type,
        }
        self.fills.append(fill)
        return fill

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
        return {"success": False, "message": "oco_not_supported_sim", "exchange": exchange}

    async def place_rfq(self, exchange: str, payload: Dict) -> Dict:
        return {"success": False, "message": "rfq_not_supported_sim", "exchange": exchange}

    async def cancel_order(self, exchange: str, order_id: str) -> None:
        return None

    async def get_balances(self) -> Dict[str, float]:
        # Simulation venue does not maintain real balances; return empty map
        # so caller can safely fallback to MTM PnL.
        return {}
