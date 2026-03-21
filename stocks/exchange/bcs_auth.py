from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

TOKEN_URL = (
    "https://be.broker.ru/trade-api-keycloak/realms/tradeapi/"
    "protocol/openid-connect/token"
)

# Access token lifetime margin — refresh 5 minutes before expiry.
_REFRESH_MARGIN_SEC = 300


class BcsTokenManager:
    """OAuth 2.0 token lifecycle for BCS Trade API.

    Usage::

        tm = BcsTokenManager(refresh_token="...", client_id="trade-api-write")
        token = await tm.get_access_token()   # auto-refreshes when needed
        await tm.close()
    """

    def __init__(
        self,
        refresh_token: str,
        client_id: str = "trade-api-write",
    ) -> None:
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0
        self._session: Optional[aiohttp.ClientSession] = None
        self._lock = asyncio.Lock()

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token and time.time() < self._expires_at - _REFRESH_MARGIN_SEC:
            return self._access_token
        async with self._lock:
            # Double-check after acquiring lock.
            if self._access_token and time.time() < self._expires_at - _REFRESH_MARGIN_SEC:
                return self._access_token
            await self._do_refresh()
        return self._access_token  # type: ignore[return-value]

    async def _do_refresh(self) -> None:
        session = await self._get_session()
        payload = {
            "client_id": self._client_id,
            "grant_type": "refresh_token",
            "refresh_token": self._refresh_token,
        }
        async with session.post(TOKEN_URL, data=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"BCS token refresh failed ({resp.status}): {body}")
            data = await resp.json()

        self._access_token = data["access_token"]
        expires_in = int(data.get("expires_in", 86400))
        self._expires_at = time.time() + expires_in

        # Update refresh token if rotated.
        new_refresh = data.get("refresh_token")
        if new_refresh and new_refresh != self._refresh_token:
            self._refresh_token = new_refresh
            logger.info("bcs_auth: refresh token rotated")

        logger.info(
            "bcs_auth: access token refreshed, expires_in=%ds",
            expires_in,
        )

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
