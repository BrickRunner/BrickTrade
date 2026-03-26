"""
Bybit V5 REST API клиент.

Используется единый base URL для всех эндпоинтов.
Документация: https://bybit-exchange.github.io/docs/v5/intro
"""
import asyncio
import aiohttp
import hmac
import hashlib
import json
import time
from typing import Dict, Any, Optional

from arbitrage.utils import get_arbitrage_logger, ExchangeConfig, get_rate_limiter

logger = get_arbitrage_logger("bybit_rest")
_EXCHANGE = "bybit"

BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"


class BybitRestClient:
    """REST API клиент для Bybit V5 — линейные USDT perpetuals + спот"""

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key if config.api_key else ""
        self.api_secret = config.api_secret if config.api_secret else ""
        self.testnet = config.testnet

        self.public_only = not self.api_key or not self.api_secret
        self.session: Optional[aiohttp.ClientSession] = None

        if self.testnet:
            self.base_url = "https://api-testnet.bybit.com"
        else:
            self.base_url = BASE_URL

    # ─── Session ──────────────────────────────────────────────────────────────

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

    # ─── Auth ─────────────────────────────────────────────────────────────────

    def _sign(self, timestamp: str, payload: str) -> str:
        """
        Bybit V5 signature: HMAC-SHA256(timestamp + api_key + recv_window + payload)
        payload = query string for GET, JSON body for POST
        """
        param_str = f"{timestamp}{self.api_key}{RECV_WINDOW}{payload}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, payload: str = "") -> Dict[str, str]:
        """Создать заголовки аутентификации"""
        timestamp = str(int(time.time() * 1000))
        sign = self._sign(timestamp, payload)
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": sign,
            "X-BAPI-RECV-WINDOW": RECV_WINDOW,
            "Content-Type": "application/json",
        }

    # ─── Public Request ───────────────────────────────────────────────────────

    async def _public_request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Публичный HTTP запрос с retry и rate limiting"""
        limiter = get_rate_limiter()
        url = f"{self.base_url}{endpoint}"
        for attempt in range(3):
            try:
                await limiter.acquire(_EXCHANGE)
                session = await self._get_session()
                async with session.request(
                    method=method, url=url, params=params or {},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 429:
                        backoff = limiter.record_429(_EXCHANGE)
                        logger.warning("Bybit 429 on %s (attempt %d), backoff %.1fs", endpoint, attempt + 1, backoff)
                        await asyncio.sleep(backoff)
                        continue
                    limiter.record_success(_EXCHANGE)
                    return await resp.json(content_type=None)
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"Public request error {endpoint}: {e}")
                    return {"retCode": -1, "retMsg": str(e), "result": {}}

    # ─── Private Request ──────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Приватный HTTP запрос с подписью, retry и rate limiting"""
        if self.public_only:
            logger.warning(f"Cannot make private request without API keys: {endpoint}")
            return {"retCode": -1, "retMsg": "No API keys configured", "result": {}}

        limiter = get_rate_limiter()
        url = f"{self.base_url}{endpoint}"

        for attempt in range(3):
            try:
                await limiter.acquire(_EXCHANGE)
                session = await self._get_session()

                if method.upper() == "GET":
                    # GET: подпись на query string
                    qs = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
                    headers = self._auth_headers(qs)
                    async with session.get(
                        url, params=params or {}, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 429:
                            backoff = limiter.record_429(_EXCHANGE)
                            logger.warning("Bybit 429 on %s (attempt %d), backoff %.1fs", endpoint, attempt + 1, backoff)
                            await asyncio.sleep(backoff)
                            continue
                        limiter.record_success(_EXCHANGE)
                        return await resp.json(content_type=None)
                else:
                    # POST: подпись на JSON body
                    body = json.dumps(data or {})
                    headers = self._auth_headers(body)
                    async with session.post(
                        url, data=body, headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 429:
                            backoff = limiter.record_429(_EXCHANGE)
                            logger.warning("Bybit 429 on %s (attempt %d), backoff %.1fs", endpoint, attempt + 1, backoff)
                            await asyncio.sleep(backoff)
                            continue
                        limiter.record_success(_EXCHANGE)
                        return await resp.json(content_type=None)

            except Exception as e:
                if attempt < 2:
                    logger.warning(f"Request attempt {attempt+1} failed {endpoint}: {e}")
                    await asyncio.sleep(0.5 * (attempt + 1))
                else:
                    logger.error(f"Request error {endpoint} after 3 attempts: {e}")
                    return {"retCode": -1, "retMsg": str(e), "result": {}}

    # ─── Market Data (Public) ─────────────────────────────────────────────────

    async def get_instruments(self) -> Dict[str, Any]:
        """Получить список линейных USDT perpetuals"""
        return await self._public_request(
            "GET", "/v5/market/instruments-info",
            params={"category": "linear", "status": "Trading"},
        )

    async def get_spot_instruments(self) -> Dict[str, Any]:
        return await self._public_request(
            "GET", "/v5/market/instruments-info",
            params={"category": "spot", "status": "Trading"},
        )

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """Получить котировки для всех линейных контрактов"""
        return await self._public_request(
            "GET", "/v5/market/tickers",
            params={"category": "linear"},
        )

    async def get_spot_tickers(self) -> Dict[str, Any]:
        """Получить котировки спотового рынка"""
        return await self._public_request(
            "GET", "/v5/market/tickers",
            params={"category": "spot"},
        )

    async def get_funding_rates(self) -> Dict[str, Any]:
        """Получить текущие ставки финансирования (из tickers)"""
        # Bybit включает fundingRate в tickers для linear
        return await self.get_tickers()

    async def get_orderbook(
        self, symbol: str, category: str = "linear", limit: int = 5
    ) -> Dict[str, Any]:
        """Получить стакан ордеров"""
        return await self._public_request(
            "GET", "/v5/market/orderbook",
            params={"category": category, "symbol": symbol, "limit": limit},
        )

    async def get_kline(
        self, symbol: str, interval: str = "5", limit: int = 300,
        category: str = "linear",
    ) -> Dict[str, Any]:
        """Получить свечи (kline).  interval: 1,3,5,15,30,60,120,240,360,720,D,W,M"""
        return await self._public_request(
            "GET", "/v5/market/kline",
            params={
                "category": category,
                "symbol": symbol,
                "interval": interval,
                "limit": str(limit),
            },
        )

    async def get_ticker(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        """Получить тикер для конкретного символа (включает fundingRate)"""
        return await self._public_request(
            "GET", "/v5/market/tickers",
            params={"category": category, "symbol": symbol},
        )

    async def get_open_interest(
        self, symbol: str, interval_time: str = "5min", limit: int = 10,
    ) -> Dict[str, Any]:
        """Получить историю открытого интереса.  intervalTime: 5min,15min,30min,1h,4h,1d"""
        return await self._public_request(
            "GET", "/v5/market/open-interest",
            params={
                "category": "linear",
                "symbol": symbol,
                "intervalTime": interval_time,
                "limit": str(limit),
            },
        )

    # ─── Account / Trading (Private) ──────────────────────────────────────────

    async def get_balance(self) -> Dict[str, Any]:
        """Получить баланс — пробуем UNIFIED, затем CONTRACT"""
        result = await self._request(
            "GET", "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED"},
        )
        if not isinstance(result, dict):
            return {"retCode": -1, "retMsg": "empty_response", "result": {}}
        # Check if UNIFIED returned coins
        if result.get("retCode") == 0:
            coins = result.get("result", {}).get("list", [{}])[0].get("coin", [])
            if coins:
                return result
        # Fallback to CONTRACT
        result = await self._request(
            "GET", "/v5/account/wallet-balance",
            params={"accountType": "CONTRACT"},
        )
        if not isinstance(result, dict):
            return {"retCode": -1, "retMsg": "empty_response", "result": {}}
        return result

    async def get_fee_rates(self, category: str = "linear") -> Dict[str, Any]:
        return await self._request(
            "GET", "/v5/account/fee-rate",
            params={"category": category},
        )

    async def get_positions(self) -> Dict[str, Any]:
        """Получить открытые позиции"""
        return await self._request(
            "GET", "/v5/position/list",
            params={"category": "linear", "settleCoin": "USDT"},
        )

    async def get_cross_position(self, symbol: str) -> Dict[str, Any]:
        """Получить позицию по конкретному символу"""
        return await self._request(
            "GET", "/v5/position/list",
            params={"category": "linear", "symbol": symbol},
        )

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Установить плечо"""
        return await self._request(
            "POST", "/v5/position/set-leverage",
            data={
                "category": "linear",
                "symbol": symbol,
                "buyLeverage": str(leverage),
                "sellLeverage": str(leverage),
            },
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
        Разместить ордер.

        side: "Buy" или "Sell" (Bybit capitalize, но мы конвертируем)
        order_type: "limit" / "market" / "ioc"
        offset: "open" / "close" — close maps to reduceOnly=true
        """
        bybit_side = side.capitalize()  # "buy" → "Buy", "sell" → "Sell"

        # Маппинг order_type
        bybit_type = "Market"
        tif = "GTC"
        if order_type.lower() in ("market", "opponent", "optimal_5"):
            bybit_type = "Market"
            tif = ""
        elif order_type.lower() == "ioc":
            bybit_type = "Limit"
            tif = "IOC"
        elif order_type.lower() == "limit":
            bybit_type = "Limit"
            tif = "GTC"

        body: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "side": bybit_side,
            "orderType": bybit_type,
            "qty": str(size),
        }

        if bybit_type == "Limit" and price > 0:
            body["price"] = str(price)

        if tif:
            body["timeInForce"] = tif

        if offset == "close":
            body["reduceOnly"] = True

        return await self._request("POST", "/v5/order/create", data=body)

    async def place_spot_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: float = 0.0,
        time_in_force: str = "",
    ) -> Dict[str, Any]:
        bybit_side = side.capitalize()
        bybit_type = "Market"
        tif = "GTC"
        if order_type.lower() in ("market", "opponent", "optimal_5"):
            bybit_type = "Market"
            tif = ""
        elif order_type.lower() == "ioc":
            bybit_type = "Limit"
            tif = "IOC"
        elif order_type.lower() == "limit":
            bybit_type = "Limit"
            tif = "GTC"

        body: Dict[str, Any] = {
            "category": "spot",
            "symbol": symbol,
            "side": bybit_side,
            "orderType": bybit_type,
            "qty": str(size),
        }
        if bybit_type == "Limit" and price > 0:
            body["price"] = str(price)
        if time_in_force:
            body["timeInForce"] = time_in_force
        elif tif:
            body["timeInForce"] = tif
        return await self._request("POST", "/v5/order/create", data=body)

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Отменить ордер"""
        return await self._request(
            "POST", "/v5/order/cancel",
            data={"category": "linear", "symbol": symbol, "orderId": order_id},
        )

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Получить статус ордера"""
        return await self._request(
            "GET", "/v5/order/realtime",
            params={"category": "linear", "symbol": symbol, "orderId": order_id},
        )

    async def get_spot_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET", "/v5/order/realtime",
            params={"category": "spot", "symbol": symbol, "orderId": order_id},
        )

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        """Закрыть позицию рыночным ордером"""
        return await self.place_order(
            symbol=symbol,
            side=side,
            size=size,
            order_type="market",
            offset="close",
        )

    async def get_instrument_info(self, symbol: str, category: str = "linear") -> Dict[str, Any]:
        """Получить информацию об инструменте (lotSize, priceFilter, leverage)"""
        return await self._public_request(
            "GET", "/v5/market/instruments-info",
            params={"category": category, "symbol": symbol},
        )

    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        position_idx: int = 0,
    ) -> Dict[str, Any]:
        """Установить SL/TP на открытую позицию"""
        body: Dict[str, Any] = {
            "category": "linear",
            "symbol": symbol,
            "positionIdx": position_idx,
        }
        if stop_loss is not None:
            body["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = str(take_profit)
        return await self._request("POST", "/v5/position/trading-stop", data=body)

    # ── RFQ (Block Trading) ────────────────────────────────────────────────

    async def get_rfq_config(self) -> Dict[str, Any]:
        """Get RFQ config / counterparty info."""
        return await self._request("GET", "/v5/rfq/config-query")

    async def create_rfq(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Create RFQ. Payload must follow Bybit RFQ Create spec."""
        return await self._request("POST", "/v5/rfq/create-rfq", data=payload)

    async def cancel_rfq(self, rfq_id: str) -> Dict[str, Any]:
        return await self._request("POST", "/v5/rfq/cancel-rfq", data={"rfqId": rfq_id})

    async def execute_quote(self, quote_id: str) -> Dict[str, Any]:
        """Execute quote returned from RFQ."""
        return await self._request("POST", "/v5/rfq/execute-quote", data={"quoteId": quote_id})

    async def close(self) -> None:
        """Закрыть HTTP сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
