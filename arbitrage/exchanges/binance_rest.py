"""
Binance Futures REST API client.

USDT-margined perpetual futures (linear).
Docs: https://binance-docs.github.io/apidocs/futures/en/
"""
import asyncio
import aiohttp
import hmac
import hashlib
import time
from typing import Dict, Any, Optional
from urllib.parse import urlencode

from arbitrage.utils import get_arbitrage_logger, ExchangeConfig

logger = get_arbitrage_logger("binance_rest")

BASE_URL = "https://fapi.binance.com"
SPOT_BASE_URL = "https://api.binance.com"
RECV_WINDOW = 5000


class BinanceRestClient:
    """REST API client for Binance USDT-M Futures + Spot"""

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key if config.api_key else ""
        self.api_secret = config.api_secret if config.api_secret else ""
        self.testnet = config.testnet

        self.public_only = not self.api_key or not self.api_secret
        self.session: Optional[aiohttp.ClientSession] = None

        if self.testnet:
            self.base_url = "https://testnet.binancefuture.com"
        else:
            self.base_url = BASE_URL

    # --- Session ---

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=30,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self.session = aiohttp.ClientSession(connector=connector)
        return self.session

    # --- Auth ---

    def _sign(self, params: Dict[str, Any]) -> str:
        query = urlencode(params)
        return hmac.new(
            self.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self) -> Dict[str, str]:
        return {
            "X-MBX-APIKEY": self.api_key,
            "Content-Type": "application/json",
        }

    # --- Public Request ---

    async def _public_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        base_url: Optional[str] = None,
    ) -> Any:
        url = f"{base_url or self.base_url}{endpoint}"
        for attempt in range(3):
            try:
                session = await self._get_session()
                async with session.request(
                    method=method, url=url, params=params or {},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return await resp.json(content_type=None)
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"Public request error {endpoint}: {e}")
                    return {}

    # --- Private Request ---

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
        base_url: Optional[str] = None,
    ) -> Any:
        if self.public_only:
            logger.warning(f"Cannot make private request without API keys: {endpoint}")
            return {"code": -1, "msg": "No API keys configured"}

        url = f"{base_url or self.base_url}{endpoint}"

        for attempt in range(3):
            try:
                session = await self._get_session()
                req_params = dict(params or data or {})
                req_params["timestamp"] = int(time.time() * 1000)
                req_params["recvWindow"] = RECV_WINDOW
                req_params["signature"] = self._sign(req_params)
                headers = self._auth_headers()

                if method.upper() == "GET":
                    async with session.get(
                        url, params=req_params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        return await resp.json(content_type=None)
                else:
                    async with session.post(
                        url, params=req_params, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        return await resp.json(content_type=None)

            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Request attempt {attempt+1} failed {endpoint}: {e}")
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    logger.error(f"Request error {endpoint} after 3 attempts: {e}")
                    return {"code": -1, "msg": str(e)}

    # --- Market Data (Public) ---

    async def get_instruments(self) -> Dict[str, Any]:
        """Get exchange info for USDT-M futures"""
        return await self._public_request("GET", "/fapi/v1/exchangeInfo")

    async def get_spot_instruments(self) -> Dict[str, Any]:
        return await self._public_request(
            "GET", "/api/v3/exchangeInfo", base_url=SPOT_BASE_URL
        )

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """Get 24h ticker for all futures symbols (returns list)"""
        result = await self._public_request("GET", "/fapi/v1/ticker/bookTicker")
        # Wrap in dict for consistency with other clients
        return {"data": result if isinstance(result, list) else []}

    async def get_spot_tickers(self) -> Dict[str, Any]:
        result = await self._public_request(
            "GET", "/api/v3/ticker/bookTicker", base_url=SPOT_BASE_URL
        )
        return {"data": result if isinstance(result, list) else []}

    async def get_funding_rates(self) -> Dict[str, Any]:
        """Get premium index (includes funding rate) for all symbols"""
        result = await self._public_request("GET", "/fapi/v1/premiumIndex")
        return {"data": result if isinstance(result, list) else []}

    async def get_orderbook(
        self, symbol: str, category: str = "linear", limit: int = 10
    ) -> Dict[str, Any]:
        """Get order book depth"""
        return await self._public_request(
            "GET", "/fapi/v1/depth",
            params={"symbol": symbol, "limit": limit},
        )

    async def get_spot_orderbook(self, symbol: str, limit: int = 10) -> Dict[str, Any]:
        return await self._public_request(
            "GET", "/api/v3/depth",
            params={"symbol": symbol, "limit": limit},
            base_url=SPOT_BASE_URL,
        )

    # --- Account / Trading (Private) ---

    async def get_balance(self) -> Dict[str, Any]:
        """Get futures account balance"""
        return await self._request("GET", "/fapi/v2/balance")

    async def get_fee_rates(self, symbol: str = "") -> Dict[str, Any]:
        params = {}
        if symbol:
            params["symbol"] = symbol
        return await self._request("GET", "/fapi/v1/commissionRate", params=params)

    async def get_positions(self) -> Dict[str, Any]:
        """Get all open positions"""
        result = await self._request("GET", "/fapi/v2/positionRisk")
        return {"data": result if isinstance(result, list) else []}

    async def get_cross_position(self, symbol: str) -> Dict[str, Any]:
        """Get position for a specific symbol"""
        result = await self._request(
            "GET", "/fapi/v2/positionRisk",
            params={"symbol": symbol},
        )
        return {"data": result if isinstance(result, list) else []}

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Set leverage for a symbol"""
        return await self._request(
            "POST", "/fapi/v1/leverage",
            data={"symbol": symbol, "leverage": leverage},
        )

    async def set_margin_type(self, symbol: str, margin_type: str = "CROSSED") -> Dict[str, Any]:
        """Set margin type (ISOLATED or CROSSED)"""
        return await self._request(
            "POST", "/fapi/v1/marginType",
            data={"symbol": symbol, "marginType": margin_type},
        )

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: float = 0.0,
        time_in_force: str = "",
        offset: str = "open",
        lever_rate: int = 1,
    ) -> Dict[str, Any]:
        """
        Place a futures order.

        side: "buy" / "sell" (mapped to BUY/SELL)
        order_type: "limit" / "market" / "ioc"
        offset: "close" maps to reduceOnly=true
        """
        binance_side = side.upper()

        # Map order type
        binance_type = "MARKET"
        tif = ""
        if order_type.lower() in ("market", "opponent", "optimal_5"):
            binance_type = "MARKET"
        elif order_type.lower() == "ioc":
            binance_type = "LIMIT"
            tif = "IOC"
        elif order_type.lower() == "limit":
            binance_type = "LIMIT"
            tif = time_in_force.upper() if time_in_force else "GTC"

        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": binance_side,
            "type": binance_type,
            "quantity": str(size),
        }

        if binance_type == "LIMIT" and price > 0:
            body["price"] = str(price)
            body["timeInForce"] = tif or "GTC"

        if offset == "close":
            body["reduceOnly"] = "true"

        return await self._request("POST", "/fapi/v1/order", data=body)

    async def place_spot_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: float = 0.0,
        time_in_force: str = "",
    ) -> Dict[str, Any]:
        binance_side = side.upper()
        binance_type = "MARKET"
        tif = ""
        if order_type.lower() in ("market", "opponent"):
            binance_type = "MARKET"
        elif order_type.lower() == "ioc":
            binance_type = "LIMIT"
            tif = "IOC"
        elif order_type.lower() == "limit":
            binance_type = "LIMIT"
            tif = time_in_force.upper() if time_in_force else "GTC"

        body: Dict[str, Any] = {
            "symbol": symbol,
            "side": binance_side,
            "type": binance_type,
            "quantity": str(size),
        }
        if binance_type == "LIMIT" and price > 0:
            body["price"] = str(price)
            body["timeInForce"] = tif or "GTC"

        return await self._request(
            "POST", "/api/v3/order", data=body, base_url=SPOT_BASE_URL
        )

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "DELETE", "/fapi/v1/order",
            params={"symbol": symbol, "orderId": order_id},
        )

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", "/fapi/v1/order",
            params={"symbol": symbol, "orderId": order_id},
        )

    async def get_spot_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", "/api/v3/order",
            params={"symbol": symbol, "orderId": order_id},
            base_url=SPOT_BASE_URL,
        )

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        """Close position with market order"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            size=size,
            order_type="market",
            offset="close",
        )

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
