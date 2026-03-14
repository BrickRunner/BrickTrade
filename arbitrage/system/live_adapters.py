from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable

from arbitrage.core.market_data import MarketDataEngine
from arbitrage.system.interfaces import ExecutionVenue, MarketDataProvider
from arbitrage.system.models import MarketSnapshot, OrderBookSnapshot
from arbitrage.system.ws_orderbooks import WsOrderbookCache
from arbitrage.system.fees import fee_bps as _env_fee_bps
from market_intelligence.indicators import (
    bollinger_bands as _bollinger_bands,
    ema as _shared_ema,
    macd as _shared_macd,
    rsi as _shared_rsi,
)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@dataclass
class LiveMarketDataProvider(MarketDataProvider):
    market_data: MarketDataEngine
    exchanges: Iterable[str]
    _initialized: bool = False
    _mid_history: Dict[str, deque] = field(default_factory=dict)
    _last_refresh_ts: float = 0.0
    _min_refresh_seconds: float = 1.0
    _last_futures_ts: float = 0.0
    _last_spot_ts: float = 0.0
    _last_funding_ts: float = 0.0
    _last_depth_ts: float = 0.0
    _last_balance_ts: float = 0.0
    _last_fee_ts: float = 0.0
    _futures_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("FUTURES_REFRESH_SECONDS", "1.0")))
    _spot_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("SPOT_REFRESH_SECONDS", "5.0")))
    _funding_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("FUNDING_REFRESH_SECONDS", "60.0")))
    _depth_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("DEPTH_REFRESH_SECONDS", "2.0")))
    _balance_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("BALANCE_REFRESH_SECONDS", "5.0")))
    _fee_refresh_seconds: float = field(default_factory=lambda: float(os.getenv("FEE_REFRESH_SECONDS", "900.0")))
    _ws_enabled: bool = field(default_factory=lambda: os.getenv("USE_WS_ORDERBOOKS", "false").strip().lower() in {"1", "true", "yes", "on"})
    _ws_cache: WsOrderbookCache | None = None
    _cached_balances: Dict[str, float] = field(default_factory=dict)

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self.market_data.initialize()
        await self.market_data.update_all()
        if self._ws_enabled:
            self._ws_cache = WsOrderbookCache(symbols=list(self.market_data.common_pairs), exchanges=self.exchanges)
            await self._ws_cache.start()
        self._initialized = True

    async def get_snapshot(self, symbol: str) -> MarketSnapshot:
        await self.initialize()
        now = time.time()
        if now - self._last_futures_ts >= self._futures_refresh_seconds:
            await self.market_data.update_futures_prices()
            self._last_futures_ts = now
        if now - self._last_spot_ts >= self._spot_refresh_seconds:
            await self.market_data.update_spot_prices()
            self._last_spot_ts = now
        if now - self._last_funding_ts >= self._funding_refresh_seconds:
            await self.market_data.update_funding_rates()
            self._last_funding_ts = now
        if now - self._last_fee_ts >= self._fee_refresh_seconds:
            await self.market_data.update_fee_rates()
            self._last_fee_ts = now
        if now - self._last_refresh_ts >= self._min_refresh_seconds:
            self._last_refresh_ts = now

        orderbooks: Dict[str, OrderBookSnapshot] = {}
        spot_orderbooks: Dict[str, OrderBookSnapshot] = {}
        orderbook_depth: Dict[str, Dict[str, list]] = {}
        spot_orderbook_depth: Dict[str, Dict[str, list]] = {}
        balances: Dict[str, float] = {}
        fee_bps: Dict[str, Dict[str, float]] = {}
        funding_rates: Dict[str, float] = {}
        mids: list[float] = []
        for exchange in self.exchanges:
            ws_ob = self._ws_cache.get(exchange, symbol) if self._ws_cache else None
            if ws_ob:
                orderbooks[exchange] = ws_ob
                mids.append(ws_ob.mid)
            else:
                ticker = self.market_data.get_futures_price(exchange, symbol)
                if ticker and ticker.bid > 0 and ticker.ask > 0:
                    orderbooks[exchange] = OrderBookSnapshot(
                        exchange=exchange,
                        symbol=symbol,
                        bid=ticker.bid,
                        ask=ticker.ask,
                        timestamp=ticker.timestamp,
                    )
                    mids.append((ticker.bid + ticker.ask) / 2)
            funding = self.market_data.get_funding(exchange, symbol)
            if funding:
                funding_rates[exchange] = funding.rate

        if os.getenv("ENABLE_SPOT_EXECUTION", "false").strip().lower() in {"1", "true", "yes", "on"}:
            for exchange in self.exchanges:
                spot_book = await self.market_data.fetch_spot_orderbook_depth(exchange, symbol, levels=1)
                if not spot_book:
                    continue
                bids = spot_book.get("bids") or []
                asks = spot_book.get("asks") or []
                if not bids or not asks:
                    continue
                bid = float(bids[0][0])
                ask = float(asks[0][0])
                if bid <= 0 or ask <= 0:
                    continue
                spot_orderbooks[exchange] = OrderBookSnapshot(
                    exchange=exchange,
                    symbol=symbol,
                    bid=bid,
                    ask=ask,
                    timestamp=time.time(),
                )

        if now - self._last_depth_ts >= self._depth_refresh_seconds:
            for exchange in self.exchanges:
                depth = await self.market_data.fetch_orderbook_depth(exchange, symbol, levels=10)
                if depth:
                    orderbook_depth[exchange] = depth
                if os.getenv("ENABLE_SPOT_EXECUTION", "false").strip().lower() in {"1", "true", "yes", "on"}:
                    spot_depth = await self.market_data.fetch_spot_orderbook_depth(exchange, symbol, levels=10)
                    if spot_depth:
                        spot_orderbook_depth[exchange] = spot_depth
            self._last_depth_ts = now

        if now - self._last_balance_ts >= self._balance_refresh_seconds:
            try:
                self._cached_balances = await self.market_data.fetch_balances()
            except Exception:
                pass
            self._last_balance_ts = now
        balances = dict(self._cached_balances)

        fee_bps = self.market_data.get_fee_bps()
        if not fee_bps:
            fee_bps = {
                ex: {"spot": _env_fee_bps(ex, "spot"), "perp": _env_fee_bps(ex, "perp")}
                for ex in self.exchanges
            }

        if not orderbooks:
            raise RuntimeError(f"No live orderbooks available for {symbol}")

        ref_mid = sum(mids) / len(mids)
        history = self._mid_history.setdefault(symbol, deque(maxlen=120))
        history.append(ref_mid)
        trend_strength = 0.0
        if len(history) > 10:
            old = history[0]
            trend_strength = (ref_mid - old) / max(abs(old), 1e-9)

        returns = []
        prev = None
        for mid in history:
            if prev is not None and prev > 0:
                returns.append(abs(mid - prev) / prev)
            prev = mid
        volatility = sum(returns) / len(returns) if returns else 0.0
        atr = self._compute_atr_like(list(history), ref_mid)
        atr_rolling = (sum(abs(x - ref_mid) for x in history) / len(history)) if history else atr

        spot_prices = [self.market_data.get_spot_price(ex, symbol) for ex in self.exchanges]
        spot_prices = [p for p in spot_prices if p and p > 0]
        spot_ref = sum(spot_prices) / len(spot_prices) if spot_prices else ref_mid
        history_list = list(history)
        rsi = _shared_rsi(history_list, period=14)
        ema_fast = _shared_ema(history_list, period=12)
        ema_slow = _shared_ema(history_list, period=26)
        macd_line, macd_signal, macd_hist = _shared_macd(history_list, fast=12, slow=26, signal=9)
        bb_lower, _bb_mid, bb_upper, _bb_width = _bollinger_bands(history_list, period=20, k=2.0)
        spread_bps = self._cross_exchange_spread_bps(orderbooks)
        funding_spread_bps = 0.0
        if funding_rates:
            funding_spread_bps = (max(funding_rates.values()) - min(funding_rates.values())) * 10_000

        indicators = {
            "spot_price": spot_ref,
            "perp_price": ref_mid,
            "rsi": rsi,
            "ema_fast": ema_fast,
            "ema_slow": ema_slow,
            "vwap": sum(history_list) / len(history_list) if history_list else ref_mid,
            "bb_upper": bb_upper,
            "bb_lower": bb_lower,
            "macd": macd_line,
            "macd_signal": macd_signal,
            "macd_hist": macd_hist,
            "spread_bps": spread_bps,
            "basis_bps": ((ref_mid - spot_ref) / max(spot_ref, 1e-9)) * 10_000,
            "funding_spread_bps": funding_spread_bps,
        }
        return MarketSnapshot(
            symbol=symbol,
            orderbooks=orderbooks,
            spot_orderbooks=spot_orderbooks,
            orderbook_depth=orderbook_depth,
            spot_orderbook_depth=spot_orderbook_depth,
            balances=balances,
            fee_bps=fee_bps,
            funding_rates=funding_rates,
            volatility=volatility,
            trend_strength=trend_strength,
            atr=atr,
            atr_rolling=max(atr_rolling, 1e-8),
            indicators=indicators,
            timestamp=time.time(),
        )

    async def health(self) -> Dict[str, float]:
        await self.initialize()
        return {exchange: self.market_data.get_latency(exchange) * 1000 for exchange in self.exchanges}

    @staticmethod
    def _compute_atr_like(values: list[float], fallback_mid: float) -> float:
        if len(values) < 2:
            return fallback_mid * 0.0005
        changes = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
        return sum(changes[-14:]) / max(1, min(14, len(changes)))

    @staticmethod
    def _cross_exchange_spread_bps(orderbooks: Dict[str, OrderBookSnapshot]) -> float:
        if len(orderbooks) < 2:
            return 0.0
        best_bid = max(ob.bid for ob in orderbooks.values())
        best_ask = min(ob.ask for ob in orderbooks.values())
        mid = (best_bid + best_ask) / 2 if best_bid > 0 and best_ask > 0 else 0.0
        if mid <= 0:
            return 0.0
        return ((best_bid - best_ask) / mid) * 10_000


@dataclass
class LiveExecutionVenue(ExecutionVenue):
    exchanges: Dict[str, Any]
    market_data: MarketDataEngine
    _balance_cache: Dict[str, float] = field(default_factory=dict)
    _last_balance_ts: float = 0.0
    _balance_refresh_seconds: float = 5.0
    # Reserve a small buffer for fees/funding/mark drift.
    safety_buffer_pct: float = field(
        default_factory=lambda: max(
            0.0, min(0.5, float(os.getenv("EXEC_MARGIN_SAFETY_BUFFER_PCT", "0.05")))
        )
    )
    # Absolute reserve in quote currency to avoid full balance depletion.
    safety_reserve_usd: float = field(
        default_factory=lambda: max(
            0.0, float(os.getenv("EXEC_MARGIN_SAFETY_RESERVE_USD", "0.50"))
        )
    )
    allow_min_notional_override: bool = field(
        default_factory=lambda: os.getenv("EXEC_ALLOW_MIN_NOTIONAL_OVERRIDE", "true").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    spot_min_qty: float = field(default_factory=lambda: float(os.getenv("SPOT_MIN_QTY", "0.0001")))
    spot_qty_step: float = field(default_factory=lambda: float(os.getenv("SPOT_QTY_STEP", "0.0001")))
    spot_min_notional_usd: float = field(default_factory=lambda: float(os.getenv("SPOT_MIN_NOTIONAL_USD", "5.0")))

    def _size_from_notional(self, exchange: str, symbol: str, notional_usd: float) -> float:
        ticker = self.market_data.get_futures_price(exchange, symbol)
        if not ticker:
            return 0.0
        px = (ticker.bid + ticker.ask) / 2
        ct = self.market_data.get_contract_size(exchange, symbol)
        min_size = self.market_data.get_min_order_size(exchange, symbol)
        if px <= 0 or ct <= 0:
            return 0.0
        if exchange == "bybit":
            qty = notional_usd / px
            step = max(ct, 1e-8)
            rounded = max(step, int(qty / step) * step)
            if min_size > 0:
                return max(rounded, min_size)
            return rounded
        # OKX/HTX operate in contract units; HTX is strict about integer volume.
        contracts = int(notional_usd / (px * ct))
        contracts = max(1, contracts)
        if exchange == "htx":
            size = float(int(contracts))
        else:
            size = float(contracts)
        if min_size > 0:
            return max(size, min_size)
        return size

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
        client = self.exchanges[exchange]
        is_close = (offset or "").lower() == "close"
        effective_notional = quantity_usd
        if not is_close:
            balances = await self._get_balances()
            available = max(0.0, balances.get(exchange, 0.0))
            max_safe_notional = max(
                0.0, (available * (1.0 - self.safety_buffer_pct)) - self.safety_reserve_usd
            )
            effective_notional = min(quantity_usd, max_safe_notional)
            min_notional = self._min_notional_usd(exchange, symbol)
            # If strategy sizing is below exchange minimum, lift to exchange minimum
            # when margin allows it instead of rejecting profitable signals.
            if effective_notional < min_notional and max_safe_notional >= min_notional:
                effective_notional = min_notional
            # Small-account override: allow one-contract minimum if balance can cover it,
            # even when safety buffer would otherwise block all entries.
            if (
                effective_notional < min_notional
                and self.allow_min_notional_override
                and available >= min_notional
            ):
                effective_notional = min_notional

            if available < min_notional or effective_notional < min_notional:
                return {
                    "success": False,
                    "message": (
                        "insufficient_margin_guard: "
                        f"available={available:.2f} "
                        f"required~{quantity_usd:.2f} "
                        f"min_notional={min_notional:.2f} "
                        f"max_safe_notional={max_safe_notional:.2f} "
                        f"buffer_pct={self.safety_buffer_pct:.2f} "
                        f"reserve_usd={self.safety_reserve_usd:.2f}"
                    ),
                    "exchange": exchange,
                }

        if quantity_contracts is not None and quantity_contracts > 0:
            size = float(quantity_contracts)
        else:
            size = self._size_from_notional(exchange, symbol, effective_notional)
        if size <= 0:
            return {"success": False, "message": "invalid_size", "exchange": exchange}

        mapped_order_type, tif, px = self._map_order_params(exchange, order_type, limit_price)
        if px > 0:
            px = self._round_price(exchange, symbol, px)
        if exchange == "okx":
            response = await client.place_order(
                symbol=symbol,
                side=side,
                size=int(size),
                order_type=mapped_order_type,
                price=px if px > 0 else None,
                time_in_force=tif,
            )
        elif exchange == "htx":
            response = await client.place_order(
                symbol=symbol,
                side=side,
                size=int(size),
                order_type=mapped_order_type,
                price=px,
                time_in_force=tif,
                offset=offset,
                lever_rate=1,
            )
        else:
            response = await client.place_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=mapped_order_type,
                price=px,
                time_in_force=tif,
                offset=offset,
                lever_rate=1,
            )

        if exchange == "okx":
            ok = response.get("code") == "0"
        elif exchange == "htx":
            ok = response.get("status") == "ok"
        elif exchange == "bybit":
            ok = response.get("retCode") == 0
        else:
            ok = False

        ticker = self.market_data.get_futures_price(exchange, symbol)
        fill_price = (ticker.bid + ticker.ask) / 2 if ticker else 0.0
        message = self._extract_error_message(exchange, response)
        order_id = self._extract_order_id(exchange, response)
        return {
            "success": ok,
            "message": message,
            "exchange": exchange,
            "raw": response,
            "fill_price": fill_price,
            "size": size,
            "order_id": order_id,
            "requested_notional": quantity_usd,
            "effective_notional": effective_notional,
        }

    async def place_spot_order(
        self,
        exchange: str,
        symbol: str,
        side: str,
        quantity_base: float,
        order_type: str,
        limit_price: float = 0.0,
    ) -> Dict:
        client = self.exchanges[exchange]
        size = self._round_spot_size(exchange, symbol, quantity_base)
        if size <= 0:
            return {"success": False, "message": "invalid_spot_size", "exchange": exchange}
        ref_price = limit_price
        if ref_price <= 0:
            ref_price = self.market_data.get_spot_price(exchange, symbol) or 0.0
        if ref_price <= 0:
            ticker = self.market_data.get_futures_price(exchange, symbol)
            ref_price = (ticker.bid + ticker.ask) / 2 if ticker else 0.0
        min_notional = self.market_data.get_spot_min_notional(exchange, symbol) or self.spot_min_notional_usd
        if ref_price > 0 and size * ref_price < min_notional:
            return {"success": False, "message": "spot_min_notional", "exchange": exchange}
        mapped_order_type, tif, px = self._map_order_params(exchange, order_type, limit_price)
        if px > 0:
            px = self._round_price(exchange, symbol, px, spot=True)
        if exchange == "okx":
            response = await client.place_spot_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=mapped_order_type,
                price=px if px > 0 else None,
                time_in_force=tif,
            )
            ok = response.get("code") == "0"
        elif exchange == "htx":
            response = await client.place_spot_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=mapped_order_type,
                price=px,
            )
            ok = response.get("status") == "ok"
        else:
            response = await client.place_spot_order(
                symbol=symbol,
                side=side,
                size=size,
                order_type=mapped_order_type,
                price=px,
                time_in_force=tif,
            )
            ok = response.get("retCode") == 0

        message = self._extract_error_message(exchange, response)
        order_id = self._extract_order_id(exchange, response)
        return {
            "success": ok,
            "message": message,
            "exchange": exchange,
            "raw": response,
            "order_id": order_id,
            "size": size,
        }

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
        client = self.exchanges[exchange]
        if exchange != "okx":
            return {"success": False, "message": "oco_not_supported", "exchange": exchange}
        size = quantity
        if spot:
            size = self._round_spot_size(exchange, symbol, quantity)
        else:
            size = float(int(quantity)) if quantity > 0 else 0.0
        if size <= 0:
            return {"success": False, "message": "invalid_oco_size", "exchange": exchange}
        response = await client.place_oco_order(
            symbol=symbol,
            side=side,
            size=size,
            tp_trigger=tp_trigger,
            tp_price=tp_price,
            sl_trigger=sl_trigger,
            sl_price=sl_price,
            spot=spot,
            reduce_only=reduce_only,
        )
        ok = response.get("code") == "0"
        message = self._extract_error_message(exchange, response)
        order_id = self._extract_order_id(exchange, response)
        return {
            "success": ok,
            "message": message,
            "exchange": exchange,
            "raw": response,
            "order_id": order_id,
            "size": size,
        }

    async def place_rfq(self, exchange: str, payload: Dict) -> Dict:
        client = self.exchanges[exchange]
        if exchange != "bybit":
            return {"success": False, "message": "rfq_not_supported", "exchange": exchange}
        response = await client.create_rfq(payload)
        ok = response.get("retCode") == 0
        if not ok:
            return {
                "success": False,
                "message": self._extract_error_message(exchange, response),
                "exchange": exchange,
                "raw": response,
            }
        # Optional auto-execution if caller passes quote_id
        quote_id = payload.get("quoteId") or payload.get("quote_id")
        if quote_id:
            exec_resp = await client.execute_quote(str(quote_id))
            exec_ok = exec_resp.get("retCode") == 0
            return {
                "success": exec_ok,
                "message": self._extract_error_message(exchange, exec_resp),
                "exchange": exchange,
                "raw": {"create": response, "execute": exec_resp},
            }
        return {
            "success": True,
            "message": "",
            "exchange": exchange,
            "raw": response,
        }

    async def cancel_order(self, exchange: str, order_id: str) -> None:
        # Symbol is required by exchange APIs; cancellations are currently not used by the new engine path.
        return None

    async def get_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        client = self.exchanges[exchange]
        return await client.get_order(symbol, order_id)

    async def get_spot_order(self, exchange: str, symbol: str, order_id: str) -> Dict:
        client = self.exchanges[exchange]
        return await client.get_spot_order(symbol, order_id)

    async def get_balances(self) -> Dict[str, float]:
        return await self._get_balances()

    async def close(self) -> None:
        for client in self.exchanges.values():
            if hasattr(client, "close"):
                await client.close()

    async def open_contracts(self, exchange: str, symbol: str) -> float:
        try:
            client = self.exchanges[exchange]
            result = await client.get_cross_position(symbol)
            if exchange == "okx":
                if result.get("code") == "0":
                    total = 0.0
                    for pos in result.get("data", []):
                        total += abs(_safe_float(pos.get("pos", 0.0)))
                    return total
            elif exchange == "htx":
                if result.get("status") == "ok":
                    total = 0.0
                    for pos in result.get("data", []):
                        total += abs(_safe_float(pos.get("volume", 0.0)))
                    return total
            elif exchange == "bybit":
                if result.get("retCode") == 0:
                    total = 0.0
                    for pos in result.get("result", {}).get("list", []):
                        total += abs(_safe_float(pos.get("size", 0.0)))
                    return total
        except Exception:
            pass
        return 0.0

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
        if not order_id:
            return False
        deadline = time.time() + (timeout_ms / 1000)
        while time.time() < deadline:
            try:
                if spot:
                    result = await self.get_spot_order(exchange, symbol, order_id)
                    if self._spot_order_filled(exchange, result, expected_size):
                        return True
                else:
                    result = await self.get_order(exchange, symbol, order_id)
                    if self._order_filled(exchange, result, expected_size):
                        return True
            except Exception:
                pass
            await asyncio.sleep(0.1)
        return False

    async def _get_balances(self) -> Dict[str, float]:
        now = time.time()
        if now - self._last_balance_ts >= self._balance_refresh_seconds or not self._balance_cache:
            try:
                self._balance_cache = await self.market_data.fetch_balances()
            except Exception:
                # Keep previous cache on transient failure.
                pass
            self._last_balance_ts = now
        return self._balance_cache

    def _min_notional_usd(self, exchange: str, symbol: str) -> float:
        ticker = self.market_data.get_futures_price(exchange, symbol)
        px = (ticker.bid + ticker.ask) / 2 if ticker else 0.0
        ct = self.market_data.get_contract_size(exchange, symbol)
        min_size = self.market_data.get_min_order_size(exchange, symbol)
        if exchange == "bybit":
            # Bybit linear minimum is generally around 5 USDT notional.
            return max(5.0, (min_size * px) if min_size > 0 and px > 0 else 5.0)
        if px > 0 and ct > 0:
            # One contract minimum for OKX/HTX.
            base = px * ct
            if min_size > 0:
                return max(base, min_size * px)
            return base
        # Conservative fallback when market data is temporarily unavailable.
        return 5.0

    @staticmethod
    def _map_order_params(exchange: str, order_type: str, limit_price: float) -> tuple[str, str, float]:
        kind = (order_type or "").lower()
        wants_limit = kind in {"limit", "ioc", "fok"} and limit_price > 0
        if exchange == "htx":
            # HTX rejects limit orders without price. Use market-like order when price is absent.
            if wants_limit:
                tif = "ioc" if kind in {"ioc", "fok"} else ""
                return "limit", tif, limit_price
            return "market", "", 0.0
        if exchange == "okx":
            if wants_limit:
                tif = "ioc" if kind in {"ioc", "fok"} else "gtc"
                return "limit", tif, limit_price
            return "market", "", 0.0
        # bybit and others
        if wants_limit:
            tif = "ioc" if kind in {"ioc", "fok"} else ""
            return "limit", tif, limit_price
        return "market", "", 0.0

    def _round_price(self, exchange: str, symbol: str, price: float, spot: bool = False) -> float:
        try:
            if spot:
                tick = self.market_data.get_spot_tick_size(exchange, symbol)
            else:
                tick = self.market_data.get_tick_size(exchange, symbol)
            if tick <= 0:
                return price
            rounded = round(price / tick) * tick
            # Eliminate floating-point artifacts by rounding to tick precision.
            if tick >= 1:
                return round(rounded, 0)
            decimals = max(0, -int(__import__("math").floor(__import__("math").log10(tick))))
            return round(rounded, decimals)
        except Exception:
            return price

    @staticmethod
    def _extract_order_id(exchange: str, response: Dict[str, Any]) -> str:
        if exchange == "okx":
            data = response.get("data") or []
            if isinstance(data, list) and data:
                return str(data[0].get("ordId") or data[0].get("orderId") or "")
            return ""
        if exchange == "htx":
            data = response.get("data")
            if isinstance(data, dict):
                return str(data.get("order_id") or data.get("order_id_str") or "")
            if isinstance(data, list) and data:
                return str(data[0].get("order_id") or data[0].get("order_id_str") or "")
            return ""
        if exchange == "bybit":
            return str(response.get("result", {}).get("orderId") or response.get("orderId") or "")
        return ""

    @staticmethod
    def _order_filled(exchange: str, response: Dict[str, Any], expected_size: float | None) -> bool:
        if exchange == "okx":
            data = response.get("data") or []
            if isinstance(data, list) and data:
                state = data[0].get("state")
                filled = float(data[0].get("accFillSz", 0) or 0)
                return state in {"filled", "2"} and (expected_size is None or filled >= expected_size * 0.98)
        if exchange == "htx":
            # HTX swap_order_info returns {"status": "ok", "data": [{...}]}
            data = response.get("data")
            if isinstance(data, list) and data:
                item = data[0]
                # HTX status codes: 6 = fully filled, 7 = cancelled
                status = str(item.get("status") or item.get("state") or "")
                filled = float(item.get("trade_volume", 0) or 0)
                return status in {"6", "filled"} and (expected_size is None or filled >= expected_size * 0.98)
            if isinstance(data, dict):
                status = str(data.get("status") or data.get("state") or "")
                filled = float(data.get("trade_volume", 0) or 0)
                return status in {"6", "filled"} and (expected_size is None or filled >= expected_size * 0.98)
            return False
        if exchange == "bybit":
            data = response.get("result", {})
            filled = float(data.get("cumExecQty", 0) or 0)
            return str(data.get("orderStatus") or "") in {"Filled", "filled"} and (expected_size is None or filled >= expected_size * 0.98)
        return False

    @staticmethod
    def _spot_order_filled(exchange: str, response: Dict[str, Any], expected_size: float | None) -> bool:
        if exchange == "okx":
            data = response.get("data") or []
            if isinstance(data, list) and data:
                state = data[0].get("state")
                filled = float(data[0].get("accFillSz", 0) or 0)
                return state in {"filled", "2"} and (expected_size is None or filled >= expected_size * 0.98)
        if exchange == "htx":
            # HTX spot order returns {"data": {"state": "filled", ...}}
            data = response.get("data")
            if isinstance(data, dict):
                state = str(data.get("state") or "")
                filled = float(data.get("filled-amount", 0) or 0)
                return state in {"filled"} and (expected_size is None or filled >= expected_size * 0.98)
            return False
        if exchange == "bybit":
            data = response.get("result", {})
            filled = float(data.get("cumExecQty", 0) or 0)
            return str(data.get("orderStatus") or "") in {"Filled", "filled"} and (expected_size is None or filled >= expected_size * 0.98)
        return False

    def _round_spot_size(self, exchange: str, symbol: str, size: float) -> float:
        try:
            step = self.market_data.get_spot_min_order_size(exchange, symbol) or self.spot_qty_step
            step = max(step, 1e-9)
            rounded = int(size / step) * step
            min_qty = self.market_data.get_spot_min_order_size(exchange, symbol) or self.spot_min_qty
            if rounded < min_qty:
                return 0.0
            return rounded
        except Exception:
            return 0.0

    @staticmethod
    def _extract_error_message(exchange: str, response: Dict[str, Any]) -> str:
        if exchange == "okx":
            if response.get("code") == "0":
                return ""
            data = response.get("data")
            if isinstance(data, list) and data:
                msg = data[0].get("sMsg") or data[0].get("msg")
                if msg:
                    return str(msg)
            return str(response.get("msg") or response.get("code") or "okx_reject")
        if exchange == "htx":
            if response.get("status") == "ok":
                return ""
            return str(
                response.get("err-msg")
                or response.get("err_msg")
                or response.get("err-code")
                or response.get("err_code")
                or response.get("status")
                or "htx_reject"
            )
        if exchange == "bybit":
            return str(response.get("retMsg") or response.get("retCode") or "bybit_reject")
        return ""
