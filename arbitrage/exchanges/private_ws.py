"""
Private WebSocket clients for OKX, HTX, Bybit.

Provides real-time push updates for:
  - Account balances (no more REST polling)
  - Order fills (instant detection vs polling)
  - Position changes (no more REST polling)

Each exchange client authenticates via HMAC and subscribes to private channels.
A unified PrivateWsManager orchestrates all connections and exposes
thread-safe cached state via simple getters.
"""
from __future__ import annotations

import asyncio
import base64
import gzip
import hashlib
import hmac
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Set

import websockets
from websockets.client import WebSocketClientProtocol

from arbitrage.utils import ExchangeConfig, get_arbitrage_logger

logger = get_arbitrage_logger("private_ws")


# ─────────────────────────────────────────────────────────────────────────────
# OKX Private WebSocket
# ─────────────────────────────────────────────────────────────────────────────

class OKXPrivateWs:
    """
    OKX private WS: wss://ws.okx.com:8443/ws/v5/private
    Channels: account, orders, positions
    Auth: HMAC-SHA256 login message
    """

    LIVE_URL = "wss://ws.okx.com:8443/ws/v5/private"
    TESTNET_URL = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.passphrase = config.passphrase or ""
        self.testnet = config.testnet
        self.ws_url = self.TESTNET_URL if self.testnet else self.LIVE_URL
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self._on_balance: Optional[Callable] = None
        self._on_order: Optional[Callable] = None
        self._on_position: Optional[Callable] = None

    def _sign(self, timestamp: str) -> str:
        message = timestamp + "GET" + "/users/self/verify"
        mac = hmac.new(
            self.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        )
        return base64.b64encode(mac.digest()).decode()

    async def connect(
        self,
        on_balance: Optional[Callable] = None,
        on_order: Optional[Callable] = None,
        on_position: Optional[Callable] = None,
    ) -> None:
        self._on_balance = on_balance
        self._on_order = on_order
        self._on_position = on_position
        self.running = True

        while self.running:
            try:
                logger.info("OKX private WS: connecting %s", self.ws_url)
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=10,
                    max_size=10 * 1024 * 1024,  # 10 MB max message
                ) as ws:
                    self.ws = ws
                    self._last_msg_ts = time.monotonic()

                    # Authenticate
                    ts = str(int(time.time()))
                    sign = self._sign(ts)
                    login_msg = {
                        "op": "login",
                        "args": [{
                            "apiKey": self.api_key,
                            "passphrase": self.passphrase,
                            "timestamp": ts,
                            "sign": sign,
                        }],
                    }
                    await ws.send(json.dumps(login_msg))

                    # Wait for login response
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(raw)
                    if resp.get("event") == "login" and resp.get("code") == "0":
                        logger.info("OKX private WS: authenticated")
                        self._auth_failures = 0  # reset on success
                    else:
                        # FIX CRITICAL H: Stop re-authenticating after 3 failures
                        # to prevent log spam + API rate-limit exhaustion.
                        self._auth_failures = getattr(self, "_auth_failures", 0) + 1
                        if self._auth_failures >= 3:
                            logger.critical(
                                "OKX private WS: %d auth failures, stopping "
                                "— check API key/passphrase", self._auth_failures,
                            )
                            self.running = False
                            break
                        logger.error("OKX private WS: login failed (%d/3): %s", self._auth_failures, resp)
                        await asyncio.sleep(5)
                        continue

                    # Subscribe to private channels
                    sub = {
                        "op": "subscribe",
                        "args": [
                            {"channel": "account"},
                            {"channel": "orders", "instType": "SWAP"},
                            {"channel": "positions", "instType": "SWAP"},
                        ],
                    }
                    await ws.send(json.dumps(sub))
                    logger.info("OKX private WS: subscribed to account/orders/positions")

                    # FIX #2: Robust message loop with zombie-connection detection.
                    # The `async for ws` pattern can silently exit on some
                    # websockets library versions when the connection dies
                    # without raising ConnectionClosed.
                    # We use an explicit recv loop + heartbeat timestamp
                    # so we can detect a dead connection by age.
                    reconnect_reason = "loop_exited"
                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._last_msg_ts = time.monotonic()
                            data = json.loads(message)
                            await self._handle(data)
                        except asyncio.TimeoutError:
                            # No message in 30s — likely zombie connection
                            logger.warning(
                                "OKX private WS: no message in 30s, reconnecting"
                            )
                            reconnect_reason = "heartbeat_timeout"
                            break
                        except Exception as e:
                            logger.error("OKX private WS recv/handle error: %s", e)
                            reconnect_reason = "recv_error"
                            break

                    if self.running and reconnect_reason != "stop_requested":
                        logger.info(
                            "OKX private WS: reconnecting (reason=%s)",
                            reconnect_reason,
                        )

            except websockets.exceptions.ConnectionClosed:
                logger.warning("OKX private WS: connection closed")
                if self.running:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                logger.info("OKX private WS: cancelled")
                break
            except Exception as e:
                logger.error("OKX private WS error: %s: %s", type(e).__name__, e)
                if self.running:
                    await asyncio.sleep(3)
            finally:
                self.ws = None

    async def _handle(self, data: Dict) -> None:
        # Subscription confirmations
        if "event" in data:
            if data["event"] == "subscribe":
                logger.info("OKX private WS: sub confirmed: %s", data.get("arg", {}).get("channel"))
            elif data["event"] == "error":
                logger.error("OKX private WS: error: %s", data)
            return

        arg = data.get("arg", {})
        channel = arg.get("channel", "")
        items = data.get("data", [])

        if channel == "account" and self._on_balance:
            # items: [{"details": [{"ccy": "USDT", "availBal": "100.5", ...}], ...}]
            for item in items:
                for detail in item.get("details", []):
                    if detail.get("ccy") == "USDT":
                        bal = float(detail.get("availBal", 0))
                        await self._on_balance("okx", bal)
                        break

        elif channel == "orders" and self._on_order:
            for item in items:
                await self._on_order("okx", {
                    "order_id": item.get("ordId", ""),
                    "cl_order_id": item.get("clOrdId", ""),
                    "state": item.get("state", ""),
                    "fill_sz": float(item.get("fillSz", 0)),
                    "avg_px": float(item.get("avgPx", 0) or 0),
                    "symbol": item.get("instId", ""),
                    "side": item.get("side", ""),
                })

        elif channel == "positions" and self._on_position:
            for item in items:
                await self._on_position("okx", {
                    "symbol": item.get("instId", ""),
                    "pos": float(item.get("pos", 0)),
                    "avg_px": float(item.get("avgPx", 0) or 0),
                    "upl": float(item.get("upl", 0) or 0),
                })

    async def disconnect(self) -> None:
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.open


# ─────────────────────────────────────────────────────────────────────────────
# HTX Private WebSocket
# ─────────────────────────────────────────────────────────────────────────────

class HTXPrivateWs:
    """
    HTX private WS: wss://api.hbdm.com/linear-swap-notification
    Channels: accounts_cross, orders_cross, positions_cross
    Auth: HMAC-SHA256 signature in auth request
    """

    WS_URL = "wss://api.hbdm.com/linear-swap-notification"

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self._on_balance: Optional[Callable] = None
        self._on_order: Optional[Callable] = None
        self._on_position: Optional[Callable] = None

    def _build_auth_params(self) -> Dict[str, str]:
        """Build HTX WS authentication parameters (same signing as REST)."""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        params = {
            "AccessKeyId": self.api_key,
            "SignatureMethod": "HmacSHA256",
            "SignatureVersion": "2",
            "Timestamp": timestamp,
        }
        host = "api.hbdm.com"
        path = "/linear-swap-notification"

        import urllib.parse
        sorted_params = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='')}"
            for k, v in sorted(params.items())
        )
        canonical = f"GET\n{host}\n{path}\n{sorted_params}"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode("utf-8"),
                canonical.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode()

        params["Signature"] = signature
        return params

    async def connect(
        self,
        on_balance: Optional[Callable] = None,
        on_order: Optional[Callable] = None,
        on_position: Optional[Callable] = None,
    ) -> None:
        self._on_balance = on_balance
        self._on_order = on_order
        self._on_position = on_position
        self.running = True

        while self.running:
            try:
                logger.info("HTX private WS: connecting %s", self.WS_URL)
                async with websockets.connect(
                    self.WS_URL,
                    ping_interval=None,  # HTX custom heartbeat
                    ping_timeout=None,
                    close_timeout=5,
                    max_size=10 * 1024 * 1024,  # 10 MB max message
                ) as ws:
                    self.ws = ws
                    self._last_msg_ts = time.monotonic()

                    # Authenticate
                    auth_params = self._build_auth_params()
                    auth_msg = {
                        "op": "auth",
                        "type": "api",
                        **auth_params,
                    }
                    await ws.send(json.dumps(auth_msg))

                    # FIX #8: Increase auth timeout from 10s → 30s
                    # HTX under load can take 15-20s for auth response.
                    raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    resp = self._decompress(raw)
                    if resp.get("op") == "auth" and resp.get("err-code") == 0:
                        logger.info("HTX private WS: authenticated")
                        self._auth_failures = 0  # FIX CRITICAL H: reset on success
                    else:
                        # FIX CRITICAL H: Stop re-authenticating after 3 failures
                        self._auth_failures = getattr(self, "_auth_failures", 0) + 1
                        if self._auth_failures >= 3:
                            logger.critical(
                                "HTX private WS: %d auth failures, stopping "
                                "— check API key/secret", self._auth_failures,
                            )
                            self.running = False
                            break
                        logger.error("HTX private WS: auth failed (%d/3): %s", self._auth_failures, resp)
                        await asyncio.sleep(5)
                        continue

                    # Subscribe to private channels.
                    # HTX unified margin: accounts_unify for balance,
                    # but orders/positions use standard cross topics with wildcard.
                    subs = [
                        {"op": "sub", "topic": "accounts_unify.USDT"},
                        {"op": "sub", "topic": "orders_cross.*"},
                        {"op": "sub", "topic": "positions_cross.*"},
                    ]
                    for sub in subs:
                        await ws.send(json.dumps(sub))
                    logger.info("HTX private WS: subscribed to accounts/orders/positions")

                    # FIX #2: Robust message loop with heartbeat detection
                    reconnect_reason = "loop_exited"
                    while self.running:
                        try:
                            raw_msg = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._last_msg_ts = time.monotonic()
                            data = self._decompress(raw_msg)
                            await self._handle(ws, data)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "HTX private WS: no message in 30s, reconnecting"
                            )
                            reconnect_reason = "heartbeat_timeout"
                            break
                        except Exception as e:
                            logger.error("HTX private WS recv/handle error: %s", e)
                            reconnect_reason = "recv_error"
                            break

                    if self.running and reconnect_reason != "stop_requested":
                        logger.info(
                            "HTX private WS: reconnecting (reason=%s)",
                            reconnect_reason,
                        )

            except websockets.exceptions.ConnectionClosed:
                logger.warning("HTX private WS: connection closed")
                if self.running:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                logger.info("HTX private WS: cancelled")
                break
            except Exception as e:
                logger.error("HTX private WS error: %s: %s", type(e).__name__, e)
                if self.running:
                    await asyncio.sleep(3)
            finally:
                self.ws = None

    @staticmethod
    def _decompress(raw) -> Dict:
        # FIX #10: HTX sends gzip-compressed messages for data,
        # but error/auth responses may be plain text (not gzip).
        if isinstance(raw, bytes):
            # Check for gzip magic header (0x1f 0x8b)
            if raw[:2] == b'\x1f\x8b':
                try:
                    raw = gzip.decompress(raw)
                except Exception:
                    # Fallback: try decoding as plain text if gzip fails
                    pass
            else:
                # Not gzip — likely an error response, skip decompression
                pass
        return json.loads(raw) if not isinstance(raw, dict) else raw

    async def _handle(self, ws, data: Dict) -> None:
        # Heartbeat
        if "op" in data and data["op"] == "ping":
            await ws.send(json.dumps({"op": "pong", "ts": data.get("ts", "")}))
            return

        # Sub confirmation
        if "op" in data and data["op"] == "sub":
            if data.get("err-code") == 0:
                logger.info("HTX private WS: sub confirmed: %s", data.get("topic"))
            else:
                logger.error("HTX private WS: sub error: %s", data)
            return

        # Notification confirmation
        if "op" in data and data["op"] == "notify":
            topic = data.get("topic", "")
            items = data.get("data", [])
            if not isinstance(items, list):
                items = [items]

            if ("accounts_cross" in topic or "accounts_unify" in topic) and self._on_balance:
                for item in items:
                    # Log raw data for diagnostics (debug level to avoid spam).
                    bal_fields = {k: v for k, v in item.items()
                                  if any(x in k for x in ("margin", "balance", "available", "withdraw", "asset"))}
                    logger.debug("HTX private WS: account push: %s", bal_fields)
                    # Extract USDT balance — try all known field names.
                    # accounts_unify pushes per-asset items with margin_asset field.
                    asset = item.get("margin_asset", "")
                    if asset and asset != "USDT":
                        continue
                    bal = None
                    for fld in ("margin_available", "withdraw_available",
                                "margin_balance", "margin_static"):
                        val = item.get(fld)
                        if val is not None:
                            try:
                                v = float(val)
                                if v > 0:
                                    bal = v
                                    break
                            except (ValueError, TypeError):
                                continue
                    if bal is not None:
                        await self._on_balance("htx", bal)
                    elif asset == "USDT":
                        # USDT item found but all fields zero — still report 0.
                        await self._on_balance("htx", 0.0)

            elif ("orders_cross" in topic or "orders_unify" in topic) and self._on_order:
                for item in items:
                    status = str(item.get("status", ""))
                    await self._on_order("htx", {
                        "order_id": str(item.get("order_id", "")),
                        "cl_order_id": str(item.get("client_order_id", "")),
                        "state": status,
                        "fill_sz": float(item.get("trade_volume", 0)),
                        "avg_px": float(item.get("trade_avg_price", 0) or 0),
                        "symbol": item.get("contract_code", ""),
                        "side": item.get("direction", ""),
                    })

            elif ("positions_cross" in topic or "positions_unify" in topic) and self._on_position:
                for item in items:
                    await self._on_position("htx", {
                        "symbol": item.get("contract_code", ""),
                        "pos": float(item.get("volume", 0)),
                        "direction": item.get("direction", ""),
                        "avg_px": float(item.get("cost_hold", 0) or 0),
                        "upl": float(item.get("profit_unreal", 0) or 0),
                    })
            return

        # Auth echo, error, etc.
        if "op" in data and data["op"] == "auth":
            return  # Already handled during connect

    async def disconnect(self) -> None:
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.open


# ─────────────────────────────────────────────────────────────────────────────
# Bybit Private WebSocket
# ─────────────────────────────────────────────────────────────────────────────

class BybitPrivateWs:
    """
    Bybit private WS: wss://stream.bybit.com/v5/private
    Channels: wallet, execution, order, position
    Auth: HMAC-SHA256 auth message
    """

    LIVE_URL = "wss://stream.bybit.com/v5/private"
    TESTNET_URL = "wss://stream-testnet.bybit.com/v5/private"

    def __init__(self, config: ExchangeConfig):
        self.api_key = config.api_key
        self.api_secret = config.api_secret
        self.testnet = config.testnet
        self.ws_url = self.TESTNET_URL if self.testnet else self.LIVE_URL
        self.ws: Optional[WebSocketClientProtocol] = None
        self.running = False
        self._on_balance: Optional[Callable] = None
        self._on_order: Optional[Callable] = None
        self._on_position: Optional[Callable] = None

    def _sign(self, timestamp: str) -> str:
        param_str = f"GET/realtime{timestamp}"
        return hmac.new(
            self.api_secret.encode("utf-8"),
            param_str.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    async def connect(
        self,
        on_balance: Optional[Callable] = None,
        on_order: Optional[Callable] = None,
        on_position: Optional[Callable] = None,
    ) -> None:
        self._on_balance = on_balance
        self._on_order = on_order
        self._on_position = on_position
        self.running = True

        while self.running:
            try:
                logger.info("Bybit private WS: connecting %s", self.ws_url)
                async with websockets.connect(
                    self.ws_url, ping_interval=20, ping_timeout=10,
                    max_size=10 * 1024 * 1024,  # 10 MB max message
                ) as ws:
                    self.ws = ws
                    self._last_msg_ts = time.monotonic()

                    # Authenticate
                    expires = str(int((time.time() + 10) * 1000))
                    sign = self._sign(expires)
                    auth_msg = {
                        "op": "auth",
                        "args": [self.api_key, expires, sign],
                    }
                    await ws.send(json.dumps(auth_msg))

                    # Wait for auth response
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(raw)
                    if resp.get("op") == "auth" and resp.get("success"):
                        logger.info("Bybit private WS: authenticated")
                        self._auth_failures = 0  # FIX CRITICAL H: reset on success
                    else:
                        # FIX CRITICAL H: Stop re-authenticating after 3 failures
                        self._auth_failures = getattr(self, "_auth_failures", 0) + 1
                        if self._auth_failures >= 3:
                            logger.critical(
                                "Bybit private WS: %d auth failures, stopping "
                                "— check API key/secret", self._auth_failures,
                            )
                            self.running = False
                            break
                        logger.error("Bybit private WS: auth failed (%d/3): %s", self._auth_failures, resp)
                        await asyncio.sleep(5)
                        continue

                    # Subscribe to private channels
                    sub = {
                        "op": "subscribe",
                        "args": ["wallet", "execution", "order", "position"],
                    }
                    await ws.send(json.dumps(sub))
                    logger.info("Bybit private WS: subscribed to wallet/execution/order/position")

                    # FIX #2: Robust message loop with heartbeat detection
                    reconnect_reason = "loop_exited"
                    while self.running:
                        try:
                            message = await asyncio.wait_for(ws.recv(), timeout=30)
                            self._last_msg_ts = time.monotonic()
                            data = json.loads(message)
                            await self._handle(data)
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Bybit private WS: no message in 30s, reconnecting"
                            )
                            reconnect_reason = "heartbeat_timeout"
                            break
                        except Exception as e:
                            logger.error("Bybit private WS recv/handle error: %s", e)
                            reconnect_reason = "recv_error"
                            break

                    if self.running and reconnect_reason != "stop_requested":
                        logger.info(
                            "Bybit private WS: reconnecting (reason=%s)",
                            reconnect_reason,
                        )

            except websockets.exceptions.ConnectionClosed:
                logger.warning("Bybit private WS: connection closed")
                if self.running:
                    await asyncio.sleep(2)
            except asyncio.CancelledError:
                logger.info("Bybit private WS: cancelled")
                break
            except Exception as e:
                logger.error("Bybit private WS error: %s: %s", type(e).__name__, e)
                if self.running:
                    await asyncio.sleep(3)
            finally:
                self.ws = None

    async def _handle(self, data: Dict) -> None:
        # Subscription/auth confirmations
        if "op" in data:
            op = data["op"]
            if op == "subscribe":
                if data.get("success"):
                    logger.info("Bybit private WS: sub confirmed")
                else:
                    logger.error("Bybit private WS: sub error: %s", data)
            elif op == "pong":
                pass
            return

        topic = data.get("topic", "")
        items = data.get("data", [])
        if not isinstance(items, list):
            items = [items]

        if topic == "wallet" and self._on_balance:
            for item in items:
                for coin in item.get("coin", []):
                    if coin.get("coin") == "USDT":
                        bal = float(coin.get("availableToWithdraw", 0) or 0)
                        await self._on_balance("bybit", bal)
                        break

        elif topic == "execution" and self._on_order:
            # Execution = fill events (fastest notification)
            for item in items:
                await self._on_order("bybit", {
                    "order_id": item.get("orderId", ""),
                    "cl_order_id": item.get("orderLinkId", ""),
                    "state": "filled" if item.get("execType") == "Trade" else item.get("execType", ""),
                    "fill_sz": float(item.get("execQty", 0)),
                    "avg_px": float(item.get("execPrice", 0) or 0),
                    "symbol": item.get("symbol", ""),
                    "side": item.get("side", "").lower(),
                })

        elif topic == "order" and self._on_order:
            for item in items:
                await self._on_order("bybit", {
                    "order_id": item.get("orderId", ""),
                    "cl_order_id": item.get("orderLinkId", ""),
                    "state": item.get("orderStatus", "").lower(),
                    "fill_sz": float(item.get("cumExecQty", 0)),
                    "avg_px": float(item.get("avgPrice", 0) or 0),
                    "symbol": item.get("symbol", ""),
                    "side": item.get("side", "").lower(),
                })

        elif topic == "position" and self._on_position:
            for item in items:
                await self._on_position("bybit", {
                    "symbol": item.get("symbol", ""),
                    "pos": float(item.get("size", 0)),
                    "side": item.get("side", ""),
                    "avg_px": float(item.get("entryPrice", 0) or 0),
                    "upl": float(item.get("unrealisedPnl", 0) or 0),
                })

    async def disconnect(self) -> None:
        self.running = False
        if self.ws:
            try:
                await self.ws.close()
            except Exception:
                pass
        self.ws = None

    def is_connected(self) -> bool:
        return self.ws is not None and self.ws.open


# ─────────────────────────────────────────────────────────────────────────────
# Unified Private WS Manager
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PrivateWsManager:
    """
    Manages private WebSocket connections for all exchanges.

    Maintains thread-safe cached state for:
      - Balances per exchange
      - Order fill events (via asyncio.Event keyed by order_id)
      - Positions per exchange per symbol

    Usage:
        manager = PrivateWsManager(configs={"okx": okx_cfg, "htx": htx_cfg, "bybit": bybit_cfg})
        await manager.start()
        ...
        balance = manager.get_balance("okx")
        filled = await manager.wait_for_fill("okx", order_id, timeout_ms=2000)
        pos = manager.get_position("htx", "BTC-USDT")
    """

    configs: Dict[str, ExchangeConfig]
    _clients: Dict[str, Any] = field(default_factory=dict)
    _tasks: Dict[str, asyncio.Task] = field(default_factory=dict)
    _balances: Dict[str, float] = field(default_factory=dict)
    _positions: Dict[str, Dict[str, Dict]] = field(default_factory=dict)  # {exchange: {symbol: {...}}}
    _order_events: Dict[str, asyncio.Event] = field(default_factory=dict)
    _order_data: Dict[str, Dict] = field(default_factory=dict)
    _fill_events: Dict[str, asyncio.Event] = field(default_factory=dict)
    _running: bool = False

    async def start(self) -> None:
        self._running = True
        for exchange, config in self.configs.items():
            if not config.api_key or not config.api_secret:
                logger.info("private_ws: skipping %s (no API keys)", exchange)
                continue
            client = self._create_client(exchange, config)
            if not client:
                continue
            self._clients[exchange] = client
            task = asyncio.create_task(
                client.connect(
                    on_balance=self._on_balance,
                    on_order=self._on_order,
                    on_position=self._on_position,
                ),
                name=f"private_ws_{exchange}",
            )
            self._tasks[exchange] = task
        logger.info("private_ws: started %d connections: %s",
                     len(self._tasks), list(self._tasks.keys()))

    async def stop(self) -> None:
        self._running = False
        for exchange, client in self._clients.items():
            await client.disconnect()
        for exchange, task in self._tasks.items():
            if not task.done():
                task.cancel()
        self._clients.clear()
        self._tasks.clear()

    @staticmethod
    def _create_client(exchange: str, config: ExchangeConfig):
        if exchange == "okx":
            return OKXPrivateWs(config)
        elif exchange == "htx":
            return HTXPrivateWs(config)
        elif exchange == "bybit":
            return BybitPrivateWs(config)
        return None

    # ── Callbacks ─────────────────────────────────────────────────────────

    async def _on_balance(self, exchange: str, balance: float) -> None:
        self._balances[exchange] = balance
        logger.debug("private_ws: balance update %s = %.4f", exchange, balance)

    async def _on_order(self, exchange: str, order: Dict) -> None:
        oid = order.get("order_id", "")
        state = order.get("state", "")
        
        # FIX #10: Cancelled orders (HTX state="7") are NOT fills.
        # Previously `"7"` (cancelled) was in is_filled set, causing
        # false "order filled" signals for cancelled orders.
        is_filled = state in {
            "filled", "partially_filled",          # OKX
            "6", "partial-filled",                  # HTX (6=filled)
            "filled", "partiallyfilled",            # Bybit
        }
        
        fill_sz = order.get("fill_sz", 0)
        # FIX #10: Only signal fill event for actual fills (not just fill_sz > 0
        # which could be stale data from a previous order).
        if is_filled and fill_sz > 0:
            evt = self._fill_events.get(oid)
            if evt:
                evt.set()
            logger.debug("private_ws: order fill %s on %s state=%s sz=%.6f",
                         oid, exchange, state, fill_sz)
        elif fill_sz > 0 and not is_filled:
            # FIX #11: Log stale events as warnings for diagnostics
            logger.debug(
                "private_ws: non-fill with fill_sz>0 %s on %s state=%s sz=%.6f "
                "(likely stale event, not signalling)",
                oid, exchange, state, fill_sz,
            )

        # FIX #11: Store order data for cross-check (with bounded cleanup).
        self._order_data[oid] = order
        
        # Bounded cleanup: remove old order entries to prevent memory leak.
        # Keep only the most recent 1000 entries.
        if len(self._order_data) > 1000:
            # Remove oldest entries (dicts maintain insertion order in Python 3.7+)
            keys_to_remove = list(self._order_data.keys())[:500]
            for k in keys_to_remove:
                self._order_data.pop(k, None)
                self._order_events.pop(k, None)
                self._fill_events.pop(k, None)

        # Also signal general order event
        evt = self._order_events.get(oid)
        if evt:
            evt.set()

    async def _on_position(self, exchange: str, pos: Dict) -> None:
        symbol = pos.get("symbol", "")
        if symbol:
            self._positions.setdefault(exchange, {})[symbol] = pos
            logger.debug("private_ws: position update %s %s = %s", exchange, symbol, pos.get("pos"))

    # ── Public API ────────────────────────────────────────────────────────

    async def seed_balances(self, market_data) -> None:
        """Fetch initial balances via REST so cache isn't empty before WS pushes."""
        try:
            rest_balances = await market_data.fetch_balances()
            for ex, bal in rest_balances.items():
                if bal >= 0 and ex not in self._balances:
                    self._balances[ex] = bal
                    logger.info("private_ws: seeded %s balance = %.4f from REST", ex, bal)
        except Exception as e:
            logger.warning("private_ws: seed_balances failed: %s", e)

    def get_balance(self, exchange: str) -> Optional[float]:
        """Get cached balance for exchange, or None if no WS data yet."""
        return self._balances.get(exchange)

    def get_all_balances(self) -> Dict[str, float]:
        """Get all cached balances."""
        return dict(self._balances)

    def get_position(self, exchange: str, symbol: str) -> Optional[Dict]:
        """Get cached position for exchange+symbol."""
        return self._positions.get(exchange, {}).get(symbol)

    def get_positions(self, exchange: str) -> Dict[str, Dict]:
        """Get all cached positions for an exchange."""
        return dict(self._positions.get(exchange, {}))

    def get_open_contracts(self, exchange: str, symbol: str) -> float:
        """Get absolute position size from cache."""
        pos = self._positions.get(exchange, {}).get(symbol)
        if pos:
            return abs(float(pos.get("pos", 0)))
        return 0.0

    async def wait_for_fill(
        self,
        exchange: str,
        order_id: str,
        timeout_ms: int,
    ) -> bool:
        """
        Wait for an order fill event via WS push.
        Returns True if fill detected within timeout, False otherwise.
        """
        if not order_id:
            return False

        # Check if already filled
        existing = self._order_data.get(order_id)
        if existing:
            state = existing.get("state", "")
            # FIX #10: HTX state "7" = cancelled, NOT filled
            if state in {"filled", "partially_filled", "6", "partial-filled", "partiallyfilled"}:
                return True
            if existing.get("fill_sz", 0) > 0:
                return True

        # Create event and wait
        evt = asyncio.Event()
        self._fill_events[order_id] = evt
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout_ms / 1000)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._fill_events.pop(order_id, None)
            # Clean up old order data after some time
            # (keep recent ones for cross-check)

    def is_connected(self, exchange: str) -> bool:
        client = self._clients.get(exchange)
        return client.is_connected() if client else False

    def health_status(self) -> Dict[str, Dict]:
        result = {}
        for exchange, task in self._tasks.items():
            client = self._clients.get(exchange)
            result[exchange] = {
                "alive": not task.done(),
                "connected": client.is_connected() if client else False,
                "has_balance": exchange in self._balances,
                "positions_tracked": len(self._positions.get(exchange, {})),
            }
        return result
