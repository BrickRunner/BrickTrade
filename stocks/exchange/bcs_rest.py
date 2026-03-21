from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Dict, List, Optional

import aiohttp
from datetime import datetime, timezone

from stocks.exchange.bcs_auth import BcsTokenManager
from stocks.system.models import CandleBar

logger = logging.getLogger(__name__)

_BASE = "https://be.broker.ru"

# BCS rate limit: 10 transactions / second.
_MAX_RPS = 10
_TOKEN_INTERVAL = 1.0 / _MAX_RPS


class BcsRestClient:
    """REST client for BCS Trade API.

    Handles portfolio, orders, market data, and instrument reference.
    """

    def __init__(self, token_manager: BcsTokenManager) -> None:
        self._tm = token_manager
        self._session: Optional[aiohttp.ClientSession] = None
        self._last_request_ts: float = 0.0
        self._rate_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(
                limit=50,
                limit_per_host=20,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _throttle(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            wait = _TOKEN_INTERVAL - (now - self._last_request_ts)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request_ts = time.monotonic()

    async def _headers(self) -> Dict[str, str]:
        token = await self._tm.get_access_token()
        return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Any] = None,
        retries: int = 2,
    ) -> Any:
        last_exc: Optional[Exception] = None
        for attempt in range(1 + retries):
            try:
                await self._throttle()
                session = await self._get_session()
                headers = await self._headers()
                async with session.request(
                    method, url, headers=headers, params=params, json=json_body,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    body = await resp.json()
                    if resp.status >= 400:
                        logger.error("bcs_rest %s %s -> %s: %s", method, url, resp.status, body)
                        raise RuntimeError(f"BCS API error {resp.status}: {body}")
                    return body
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < retries:
                    logger.warning("bcs_rest %s %s attempt %d failed: %s, retrying...", method, url, attempt + 1, exc)
                    # Recreate session on connection errors.
                    if self._session and not self._session.closed:
                        await self._session.close()
                    self._session = None
                    await asyncio.sleep(0.5)
        raise last_exc  # type: ignore[misc]

    # ------------------------------------------------------------------
    # Portfolio & Limits
    # ------------------------------------------------------------------

    async def get_portfolio(self) -> Dict[str, Any]:
        return await self._request("GET", f"{_BASE}/trade-api-bff-portfolio/api/v1/portfolio")

    async def get_limits(self) -> Dict[str, Any]:
        return await self._request("GET", f"{_BASE}/trade-api-bff-limit/api/v1/limits")

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    # BCS API expects side and orderType as STRING numbers: "1"=buy, "2"=sell, etc.
    _SIDE_MAP = {1: "1", 2: "2", "buy": "1", "sell": "2", "1": "1", "2": "2"}
    _ORDER_TYPE_MAP = {1: "1", 2: "2", "market": "1", "limit": "2", "1": "1", "2": "2"}

    async def place_order(
        self,
        ticker: str,
        class_code: str,
        side: int | str,
        order_type: int | str,
        quantity: int,
        price: float = 0.0,
    ) -> Dict[str, Any]:
        """Place an order on BCS.

        Args:
            side: 1/"buy" = buy, 2/"sell" = sell.
            order_type: 1/"market" = market, 2/"limit" = limit.
        """
        side_str = self._SIDE_MAP.get(side, str(side))
        otype_str = self._ORDER_TYPE_MAP.get(order_type, str(order_type))

        order_id = str(uuid.uuid4())
        payload: Dict[str, Any] = {
            "clientOrderId": order_id,
            "side": side_str,
            "orderType": otype_str,
            "orderQuantity": quantity,
            "ticker": ticker,
            "classCode": class_code,
        }
        if otype_str == "2" and price > 0:
            payload["price"] = round(price, 8)

        logger.info(
            "bcs_rest: placing order %s %s %s qty=%d price=%.4f id=%s",
            side_str, ticker, otype_str, quantity, price, order_id,
        )
        result = await self._request(
            "POST",
            f"{_BASE}/trade-api-bff-operations/api/v1/orders",
            json_body=payload,
        )
        logger.info("bcs_rest: order response: %s", result)
        return {"order_id": order_id, **result}

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "POST",
            f"{_BASE}/trade-api-bff-operations/api/v1/orders/{order_id}/cancel",
        )

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"{_BASE}/trade-api-bff-operations/api/v1/orders/{order_id}",
        )

    # ------------------------------------------------------------------
    # Market Data
    # ------------------------------------------------------------------

    async def get_candles(
        self,
        ticker: str,
        class_code: str,
        timeframe: str,
        start_date: str,
        end_date: str,
    ) -> List[CandleBar]:
        """Fetch historical candles.

        Args:
            timeframe: M1, M5, M15, M30, H1, H4, D, W, MN.
            start_date / end_date: ISO-8601 strings.
        """
        params = {
            "classCode": class_code,
            "ticker": ticker,
            "startDate": start_date,
            "endDate": end_date,
            "timeFrame": timeframe,
        }
        data = await self._request(
            "GET",
            f"{_BASE}/trade-api-market-data-connector/api/v1/candles-chart",
            params=params,
        )
        logger.debug("bcs_rest: candles %s raw type=%s len=%s sample=%s",
                      ticker, type(data).__name__,
                      len(data) if isinstance(data, (list, dict)) else "?",
                      str(data)[:300])
        candles: List[CandleBar] = []
        raw_bars = data if isinstance(data, list) else data.get("bars", data.get("candles", []))
        for c in raw_bars:
            ts_raw = c.get("time", c.get("dateTime", 0))
            if isinstance(ts_raw, str):
                try:
                    ts_val = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts_val = 0.0
            else:
                ts_val = float(ts_raw)
            candles.append(
                CandleBar(
                    timestamp=ts_val,
                    open=float(c.get("open", 0)),
                    high=float(c.get("high", 0)),
                    low=float(c.get("low", 0)),
                    close=float(c.get("close", 0)),
                    volume=float(c.get("volume", 0)),
                )
            )
        return candles

    # ------------------------------------------------------------------
    # Instruments & Schedule
    # ------------------------------------------------------------------

    async def get_instruments(
        self, tickers: List[str], class_code: str = "TQBR"
    ) -> List[Dict[str, Any]]:
        return await self._request(
            "POST",
            f"{_BASE}/trade-api-information-service/api/v1/instruments/by-tickers",
            json_body={"tickers": tickers},
        )

    async def get_trading_status(self, class_code: str = "TQBR") -> Dict[str, Any]:
        return await self._request(
            "GET",
            f"{_BASE}/trade-api-information-service/api/v1/trading-schedule/status",
            params={"classCode": class_code},
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
        await self._tm.close()
