"""
OKX REST API клиент
"""
import asyncio
import aiohttp
import json
import time
import hmac
import hashlib
import base64
from typing import Dict, Any, Optional, List

from arbitrage.utils import get_arbitrage_logger, ExchangeConfig, get_rate_limiter

logger = get_arbitrage_logger("okx_rest")
_EXCHANGE = "okx"


class OKXRestClient:
    """REST API клиент для OKX"""

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.passphrase = config.passphrase
        self.testnet = config.testnet

        if self.testnet:
            self.base_url = "https://www.okx.com"  # OKX не имеет отдельного testnet REST
        else:
            self.base_url = "https://www.okx.com"

        self.session: Optional[aiohttp.ClientSession] = None
        # FIX CRITICAL #3: Lazy session lock — created inside _get_session()
        self._session_lock: Optional[asyncio.Lock] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Получить или создать HTTP сессию с оптимизированным connection pooling"""
        # FIX CRITICAL #3: Lazy lock creation for event-loop safety
        if self._session_lock is None:
            self._session_lock = asyncio.Lock()
        async with self._session_lock:
            if self.session is None or self.session.closed:
                # Создаем коннектор с оптимизациями для максимальной скорости
                connector = aiohttp.TCPConnector(
                    limit=100,  # Максимум 100 соединений
                    limit_per_host=30,  # Максимум 30 соединений к одному хосту
                    ttl_dns_cache=300,  # Кэш DNS на 5 минут
                    enable_cleanup_closed=True  # Автоочистка закрытых соединений
                )
                self.session = aiohttp.ClientSession(connector=connector)
            return self.session

    def _sign(self, timestamp: str, method: str, request_path: str, body: str = "") -> str:
        """Создать подпись для запроса"""
        message = timestamp + method + request_path + body
        mac = hmac.new(
            bytes(self.api_secret, encoding='utf8'),
            bytes(message, encoding='utf-8'),
            digestmod=hashlib.sha256
        )
        return base64.b64encode(mac.digest()).decode()

    def _get_headers(self, method: str, request_path: str, body: str = "") -> Dict[str, str]:
        """Получить заголовки для запроса"""
        # FIX CRITICAL #6: Use time.time() instead of deprecated datetime.utcnow()
        # to avoid timezone drift that can cause HMAC signature mismatches.
        timestamp_ms = int(time.time() * 1000)
        timestamp_str = f"{timestamp_ms}Z"
        sign = self._sign(timestamp_str, method, request_path, body)

        return {
            'OK-ACCESS-KEY': self.api_key,
            'OK-ACCESS-SIGN': sign,
            'OK-ACCESS-TIMESTAMP': timestamp_str,
            'OK-ACCESS-PASSPHRASE': self.passphrase,
            'Content-Type': 'application/json'
        }

    async def _public_request(self, method: str, endpoint: str,
                              params: Optional[Dict] = None) -> Dict[str, Any]:
        """Выполнить публичный HTTP запрос (без аутентификации)"""
        limiter = get_rate_limiter()
        await limiter.acquire(_EXCHANGE)

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        try:
            async with session.request(
                method=method,
                url=url,
                params=params,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 429:
                    backoff = limiter.record_429(_EXCHANGE)
                    logger.warning("OKX 429 on %s, backoff %.1fs", endpoint, backoff)
                    return {"code": "429", "msg": "rate_limited"}

                limiter.record_success(_EXCHANGE)

                try:
                    result = await response.json()
                except Exception:
                    text = await response.text()
                    logger.error(
                        "OKX public API error: non-JSON response "
                        f"status={response.status} endpoint={endpoint} params={params} body={text[:200]}"
                    )
                    return {"code": str(response.status), "msg": "non_json_response"}

                if result.get("code") != "0":
                    logger.error(
                        "OKX public API error: "
                        f"status={response.status} endpoint={endpoint} params={params} result={result}"
                    )

                return result

        except Exception as e:
            logger.error(f"OKX public request error: {e}")
            raise

    async def _request(self, method: str, endpoint: str, params: Optional[Dict] = None,
                       data: Optional[Dict] = None) -> Dict[str, Any]:
        """Выполнить HTTP запрос (с аутентификацией)"""
        limiter = get_rate_limiter()
        await limiter.acquire(_EXCHANGE)

        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        # Формируем request_path для подписи
        request_path = endpoint
        body = ""

        if method == "GET" and params:
            query_string = "&".join([f"{k}={v}" for k, v in params.items()])
            request_path += f"?{query_string}"
        elif method == "POST" and data:
            body = json.dumps(data)

        headers = self._get_headers(method, request_path, body)

        try:
            async with session.request(
                method=method,
                url=url,
                params=params if method == "GET" else None,
                data=body if method == "POST" else None,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 429:
                    backoff = limiter.record_429(_EXCHANGE)
                    logger.warning("OKX 429 on %s, backoff %.1fs", endpoint, backoff)
                    return {"code": "429", "msg": "rate_limited"}

                limiter.record_success(_EXCHANGE)
                result = await response.json()

                if result.get("code") != "0":
                    logger.error(f"OKX API error: {result}")

                return result

        except Exception as e:
            logger.error(f"OKX request error: {e}", exc_info=True)
            raise

    async def get_instruments(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """
        Получить список всех торговых инструментов (публичный API, без ключей)

        Args:
            inst_type: Тип инструмента (SWAP для perpetual futures)

        Returns:
            Dict с информацией о всех инструментах
        """
        return await self._public_request("GET", "/api/v5/public/instruments", {"instType": inst_type})

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """
        Получить текущие цены всех инструментов (публичный API, без ключей)

        Args:
            inst_type: Тип инструмента (SWAP для perpetual futures, SPOT для спота)

        Returns:
            Dict с ценами всех инструментов
        """
        return await self._public_request("GET", "/api/v5/market/tickers", {"instType": inst_type})

    async def get_spot_tickers(self) -> Dict[str, Any]:
        """Получить спотовые тикеры (публичный API)"""
        return await self._public_request("GET", "/api/v5/market/tickers", {"instType": "SPOT"})

    async def get_spot_instruments(self) -> Dict[str, Any]:
        return await self._public_request("GET", "/api/v5/public/instruments", {"instType": "SPOT"})

    async def get_funding_rate(self, inst_id: str) -> Dict[str, Any]:
        """
        Получить текущую ставку финансирования для конкретного инструмента

        Args:
            inst_id: Идентификатор инструмента в формате OKX (например BTC-USDT-SWAP)
        """
        return await self._public_request(
            "GET", "/api/v5/public/funding-rate", {"instId": inst_id}
        )

    async def get_funding_rates_all(self, symbols: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Получить ставки финансирования для SWAP инструментов.
        OKX requires per-instrument /api/v5/public/funding-rate calls.
        We fetch only the symbols we need (from config).
        """
        if not symbols:
            # Fallback: get all USDT-SWAP instruments
            instruments = await self._public_request(
                "GET", "/api/v5/public/instruments", {"instType": "SWAP"}
            )
            symbols = []
            if instruments.get("code") == "0":
                for inst in instruments.get("data", []):
                    inst_id = inst.get("instId", "")
                    if "-USDT-SWAP" in inst_id:
                        symbols.append(inst_id)

        all_rates = []
        # Fetch in parallel with semaphore to avoid rate limits
        sem = asyncio.Semaphore(5)

        async def _fetch_one(inst_id: str):
            async with sem:
                try:
                    resp = await self._public_request(
                        "GET", "/api/v5/public/funding-rate",
                        {"instId": inst_id}
                    )
                    if resp.get("code") == "0" and resp.get("data"):
                        return resp["data"]
                except Exception:
                    pass
                return []

        tasks = [_fetch_one(s) for s in symbols]
        results = await asyncio.gather(*tasks)
        for batch in results:
            all_rates.extend(batch)

        return {"code": "0", "data": all_rates}

    async def get_orderbook(self, inst_id: str, sz: int = 5) -> Dict[str, Any]:
        """
        Получить стакан ордеров

        Args:
            inst_id: Идентификатор инструмента (например BTC-USDT-SWAP)
            sz: Глубина стакана (1-400)
        """
        return await self._public_request(
            "GET", "/api/v5/market/books", {"instId": inst_id, "sz": sz}
        )

    async def get_balance(self) -> Dict[str, Any]:
        """Получить баланс аккаунта"""
        return await self._request("GET", "/api/v5/account/balance")

    async def get_trade_fee(self, inst_type: str = "SWAP", inst_id: str = "") -> Dict[str, Any]:
        params = {"instType": inst_type}
        if inst_id:
            params["instId"] = inst_id
        return await self._request("GET", "/api/v5/account/trade-fee", params)

    async def get_positions(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """Получить открытые позиции"""
        return await self._request("GET", "/api/v5/account/positions", {"instType": inst_type})

    async def get_cross_position(self, symbol: str) -> Dict[str, Any]:
        """Получить позицию по конкретному символу (cross margin)"""
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT-SWAP"
        else:
            inst_id = f"{symbol}-SWAP"
        return await self._request(
            "GET", "/api/v5/account/positions", {"instId": inst_id}
        )

    async def set_leverage(self, symbol: str, leverage: int, margin_mode: str = "cross") -> Dict[str, Any]:
        """Установить кредитное плечо"""
        # Форматирование символа для OKX
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT-SWAP"
        else:
            inst_id = f"{symbol}-SWAP"

        data = {
            "instId": inst_id,
            "lever": str(leverage),
            "mgnMode": margin_mode
        }
        return await self._request("POST", "/api/v5/account/set-leverage", data=data)

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: Optional[float] = None,
        time_in_force: str = "ioc",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Разместить ордер

        Args:
            symbol: Символ (BTCUSDT)
            side: Сторона (buy/sell)
            size: Размер
            order_type: Тип ордера (limit/market)
            price: Цена (для limit)
            time_in_force: Time in force (ioc/gtc)
            client_order_id: Optional idempotency key (OKX: clientOrderId).
                If a subsequent request uses the same clientOrderId and the
                original order is still open, OKX returns the original order
                instead of creating a duplicate.
        """
        # Форматирование символа для OKX
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT-SWAP"
        else:
            inst_id = f"{symbol}-SWAP"

        data = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": side,
            "ordType": order_type,
            "sz": str(size)
        }

        if client_order_id:
            data["clOrdId"] = client_order_id

        if order_type == "limit":
            if price is None:
                raise ValueError("Price is required for limit orders")
            data["px"] = str(price)

        # IOC order
        if time_in_force == "ioc":
            data["ordType"] = "ioc" if order_type == "limit" else "market"

        logger.info(f"Placing OKX order: {data}")
        return await self._request("POST", "/api/v5/trade/order", data=data)

    async def place_spot_order(
        self,
        symbol: str,
        side: str,
        size: float,
        order_type: str = "limit",
        price: Optional[float] = None,
        time_in_force: str = "ioc",
        client_order_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        inst_id = symbol.replace("USDT", "-USDT") if symbol.endswith("USDT") else symbol.replace("-", "")
        data = {
            "instId": inst_id,
            "tdMode": "cash",
            "side": side,
            "ordType": order_type,
            "sz": str(size),
        }
        if client_order_id:
            data["clOrdId"] = client_order_id
        if order_type == "limit":
            if price is None:
                raise ValueError("Price is required for limit orders")
            data["px"] = str(price)
        if time_in_force == "ioc":
            data["ordType"] = "ioc" if order_type == "limit" else "market"
        logger.info(f"Placing OKX spot order: {data}")
        return await self._request("POST", "/api/v5/trade/order", data=data)

    async def place_oco_order(
        self,
        symbol: str,
        side: str,
        size: float,
        *,
        tp_trigger: float,
        tp_price: float,
        sl_trigger: float,
        sl_price: float,
        spot: bool = False,
        reduce_only: bool = False,
    ) -> Dict[str, Any]:
        """
        Place an OCO (TP/SL) algo order.
        Uses /api/v5/trade/order-algo with ordType=oco.
        Docs: OKX REST v5 Place Algo Order (OCO) parameters.
        """
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT"
        else:
            inst_id = symbol
        if not spot:
            inst_id = inst_id if inst_id.endswith("-SWAP") else f"{inst_id}-SWAP"

        data = {
            "instId": inst_id,
            "tdMode": "cash" if spot else "cross",
            "side": side,
            "ordType": "oco",
            "sz": str(size),
            "tpTriggerPx": str(tp_trigger),
            "tpOrdPx": str(tp_price),
            "slTriggerPx": str(sl_trigger),
            "slOrdPx": str(sl_price),
        }
        if reduce_only and not spot:
            data["reduceOnly"] = "true"

        logger.info(f"Placing OKX OCO order: {data}")
        return await self._request("POST", "/api/v5/trade/order-algo", data=data)

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Отменить ордер"""
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT-SWAP"
        else:
            inst_id = f"{symbol}-SWAP"

        data = {
            "instId": inst_id,
            "ordId": order_id
        }
        return await self._request("POST", "/api/v5/trade/cancel-order", data=data)

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Получить информацию об ордере"""
        if symbol.endswith("USDT"):
            base = symbol[:-4]
            inst_id = f"{base}-USDT-SWAP"
        else:
            inst_id = f"{symbol}-SWAP"

        params = {
            "instId": inst_id,
            "ordId": order_id
        }
        return await self._request("GET", "/api/v5/trade/order", params=params)

    async def get_spot_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        inst_id = symbol.replace("USDT", "-USDT") if symbol.endswith("USDT") else symbol.replace("-", "")
        params = {"instId": inst_id, "ordId": order_id}
        return await self._request("GET", "/api/v5/trade/order", params=params)

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        """Закрыть позицию рыночным ордером"""
        # Для закрытия long позиции - sell, для short - buy
        close_side = "sell" if side == "long" else "buy"

        return await self.place_order(
            symbol=symbol,
            side=close_side,
            size=size,
            order_type="market"
        )

    async def close(self) -> None:
        """Закрыть HTTP сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
            logger.info("OKX REST session closed")
