"""
HTX (Huobi) REST API клиент.

Используются два base URL:
- FUTURES_BASE_URL  — для линейных свопов (USDT-margined perpetuals)
- SPOT_BASE_URL     — для спотового рынка

Документация HTX API:
  https://www.htx.com/en-us/opend/newApiPages/
"""
import asyncio
import aiohttp
import hmac
import hashlib
import base64
import urllib.parse
from datetime import datetime, timezone
from typing import Dict, Any, Optional

from arbitrage.utils import get_arbitrage_logger, ExchangeConfig

logger = get_arbitrage_logger("htx_rest")

# Base URLs
FUTURES_BASE_URL = "https://api.hbdm.com"    # Linear swap (USDT-margined)
SPOT_BASE_URL    = "https://api.htx.com"      # Spot market


def _usdt_to_htx(symbol: str) -> str:
    """Конвертировать BTCUSDT → BTC-USDT"""
    if "-" in symbol:
        return symbol.upper()
    if symbol.upper().endswith("USDT"):
        base = symbol[:-4].upper()
        return f"{base}-USDT"
    return symbol.upper()


def _htx_to_usdt(contract_code: str) -> str:
    """Конвертировать BTC-USDT → BTCUSDT"""
    return contract_code.replace("-", "").upper()


class HTXRestClient:
    """REST API клиент для HTX (Huobi) — линейные свопы + спот"""

    def __init__(self, config: ExchangeConfig):
        self.api_key    = config.api_key    if config.api_key    else ""
        self.api_secret = config.api_secret if config.api_secret else ""
        self.testnet    = config.testnet

        # Публичный режим (без аутентификации)
        self.public_only = not self.api_key or not self.api_secret

        self.session: Optional[aiohttp.ClientSession] = None

    # ─── Session ─────────────────────────────────────────────────────────────

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

    # ─── Auth Helpers ────────────────────────────────────────────────────────

    def _sign_request(
        self,
        method: str,
        host: str,
        path: str,
        params: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Добавить подпись к параметрам запроса.
        HTX использует HMAC-SHA256 подпись query-строки.
        """
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        signed_params = dict(params)
        signed_params.update({
            "AccessKeyId": self.api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp,
        })

        # Сортируем параметры и строим строку
        sorted_params = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(signed_params.items())
        )

        canonical = f"{method.upper()}\n{host}\n{path}\n{sorted_params}"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode()

        signed_params["Signature"] = signature
        return signed_params

    # ─── Public Request ───────────────────────────────────────────────────────

    async def _public_request(
        self,
        method: str,
        base_url: str,
        endpoint: str,
        params: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Публичный HTTP запрос"""
        session = await self._get_session()
        url = f"{base_url}{endpoint}"
        try:
            async with session.request(
                method=method, url=url, params=params or {}, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"Public request error {endpoint}: {e}")
            return {}

    # ─── Private Request ─────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        base_url: str,
        endpoint: str,
        params: Optional[Dict] = None,
        data: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Приватный HTTP запрос с подписью"""
        if self.public_only:
            logger.warning(f"Cannot make private request without API keys: {endpoint}")
            return {"status": "error", "err-msg": "No API keys configured"}

        host = urllib.parse.urlparse(base_url).hostname or ""
        get_params = dict(params or {})

        signed = self._sign_request(method.upper(), host, endpoint, get_params)
        session = await self._get_session()
        url = f"{base_url}{endpoint}"

        try:
            if method.upper() == "GET":
                async with session.get(url, params=signed, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    return await resp.json(content_type=None)
            else:
                # POST: подпись в query string, тело — JSON
                qs = "&".join(f"{k}={urllib.parse.quote(str(v), safe='')}" for k, v in signed.items())
                async with session.post(
                    f"{url}?{qs}",
                    json=data or {},
                    headers={"Content-Type": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    return await resp.json(content_type=None)
        except Exception as e:
            logger.error(f"Private request error {endpoint}: {e}")
            return {"status": "error", "err-msg": str(e)}

    # ─── Market Data (Public) ────────────────────────────────────────────────

    async def get_instruments(self) -> Dict[str, Any]:
        """Получить список линейных свопов (USDT-margined)"""
        return await self._public_request(
            "GET", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_contract_info"
        )

    async def get_tickers(self, inst_type: str = "SWAP") -> Dict[str, Any]:
        """
        Получить котировки (bid/ask) для всех линейных свопов.
        inst_type игнорируется — HTX использует единственный эндпоинт.
        """
        return await self._public_request(
            "GET", FUTURES_BASE_URL, "/linear-swap-ex/market/detail/batch_merged"
        )

    async def get_spot_tickers(self) -> Dict[str, Any]:
        """Получить котировки спотового рынка"""
        return await self._public_request(
            "GET", SPOT_BASE_URL, "/market/tickers"
        )

    async def get_funding_rates(self) -> Dict[str, Any]:
        """Получить текущие ставки финансирования для всех контрактов"""
        return await self._public_request(
            "GET", FUTURES_BASE_URL, "/swap-api/v3/swap_batch_funding_rate"
        )

    async def get_orderbook(
        self, symbol: str, category: str = "linear", limit: int = 5
    ) -> Dict[str, Any]:
        """
        Получить стакан ордеров для линейного свопа.
        symbol может быть как BTCUSDT, так и BTC-USDT.
        type: step0 — полный стакан (step5/step4 — агрегированный).
        """
        contract_code = _usdt_to_htx(symbol)
        return await self._public_request(
            "GET", FUTURES_BASE_URL, "/linear-swap-ex/market/depth",
            params={"contract_code": contract_code, "type": "step0"},
        )

    async def get_spot_orderbook(self, symbol: str, depth: int = 5) -> Dict[str, Any]:
        """Стакан ордеров для спотовой пары"""
        htx_sym = symbol.lower().replace("-", "").replace("usdt", "usdt")
        return await self._public_request(
            "GET", SPOT_BASE_URL, "/market/depth",
            params={"symbol": htx_sym, "type": "step0"},
        )

    # ─── Account / Trading (Private) ────────────────────────────────────────

    async def get_balance(self) -> Dict[str, Any]:
        """Получить баланс аккаунта (линейные свопы)"""
        return await self._request(
            "GET", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_account_info"
        )

    async def get_positions(self) -> Dict[str, Any]:
        """Получить открытые позиции"""
        return await self._request(
            "GET", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_position_info"
        )

    async def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        """Установить плечо"""
        contract_code = _usdt_to_htx(symbol)
        return await self._request(
            "POST", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_switch_lever_rate",
            data={"contract_code": contract_code, "lever_rate": leverage},
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
        lever_rate: int = 3,
    ) -> Dict[str, Any]:
        """
        Разместить ордер на линейный своп.

        side: "buy" (открыть long / закрыть short) или "sell" (открыть short / закрыть long)
        offset: "open" / "close"
        order_type: "limit" / "opponent" (market) / "ioc" / "optimal_5"
        """
        contract_code = _usdt_to_htx(symbol)

        # Маппинг order_type
        htx_order_type = order_type.lower()
        if htx_order_type == "market":
            htx_order_type = "opponent"
        elif htx_order_type == "ioc":
            htx_order_type = "ioc"

        body: Dict[str, Any] = {
            "contract_code": contract_code,
            "direction": side.lower(),
            "offset": offset,
            "lever_rate": lever_rate,
            "order_price_type": htx_order_type,
            "volume": size,
        }
        if price > 0 and htx_order_type == "limit":
            body["price"] = price

        return await self._request(
            "POST", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_order", data=body
        )

    async def cancel_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Отменить ордер"""
        contract_code = _usdt_to_htx(symbol)
        return await self._request(
            "POST", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_cancel",
            data={"contract_code": contract_code, "order_id": order_id},
        )

    async def get_order(self, symbol: str, order_id: str) -> Dict[str, Any]:
        """Получить статус ордера"""
        contract_code = _usdt_to_htx(symbol)
        return await self._request(
            "POST", FUTURES_BASE_URL, "/linear-swap-api/v3/swap_order_info",
            data={"contract_code": contract_code, "order_id": order_id},
        )

    async def close_position(self, symbol: str, side: str, size: float) -> Dict[str, Any]:
        """
        Закрыть позицию рыночным ордером.
        side: "buy" или "sell" — направление ЗАКРЫВАЮЩЕГО ордера
        """
        return await self.place_order(
            symbol=symbol,
            side=side,
            size=size,
            order_type="opponent",
            offset="close",
        )

    async def close(self) -> None:
        """Закрыть HTTP сессию"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
