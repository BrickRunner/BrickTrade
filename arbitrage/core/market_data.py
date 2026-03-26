"""
Unified Market Data Engine.

Fetches prices, funding rates, and spot prices from all exchanges
via REST polling. Provides a single interface for all strategies.
"""
import asyncio
import time
import os
from typing import Dict, Optional, Set, Any, List
from dataclasses import dataclass, field

from arbitrage.utils import get_arbitrage_logger

logger = get_arbitrage_logger("market_data")


@dataclass
class TickerData:
    bid: float
    ask: float
    last: float = 0.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class FundingData:
    rate: float           # Funding rate as decimal (0.0001 = 0.01%)
    rate_pct: float       # Funding rate as percentage (0.01)
    next_time_ms: int = 0 # Next funding timestamp (ms)
    interval_h: int = 8   # Funding interval in hours


class MarketDataEngine:
    """
    Unified market data provider for all exchanges.

    Stores:
    - futures_prices[exchange][symbol] = TickerData
    - spot_prices[exchange][symbol] = float (mid price)
    - funding_rates[exchange][symbol] = FundingData
    - contract_sizes[exchange][symbol] = float
    - instruments[exchange] = Set[str]
    """

    def __init__(self, exchanges: Dict[str, Any]):
        """
        Args:
            exchanges: {"okx": OKXRestClient, "htx": HTXRestClient, "bybit": BybitRestClient}
        """
        self.exchanges = exchanges
        self.futures_prices: Dict[str, Dict[str, TickerData]] = {}
        self.spot_prices: Dict[str, Dict[str, float]] = {}
        self.funding_rates: Dict[str, Dict[str, FundingData]] = {}
        self.contract_sizes: Dict[str, Dict[str, float]] = {}
        self.tick_sizes: Dict[str, Dict[str, float]] = {}
        self.min_order_sizes: Dict[str, Dict[str, float]] = {}
        self.spot_tick_sizes: Dict[str, Dict[str, float]] = {}
        self.spot_min_order_sizes: Dict[str, Dict[str, float]] = {}
        self.spot_min_notional: Dict[str, Dict[str, float]] = {}
        self.fee_bps: Dict[str, Dict[str, float]] = {}
        self.instruments: Dict[str, Set[str]] = {}
        self.common_pairs: Set[str] = set()
        self._last_update: Dict[str, float] = {}
        self._latency: Dict[str, float] = {}

    async def initialize(self) -> int:
        """Fetch instruments from all exchanges, find common pairs. Returns pair count."""
        tasks = {}
        for name in self.exchanges:
            tasks[name] = asyncio.ensure_future(self._fetch_instruments(name))

        results = await asyncio.gather(*tasks.values(), return_exceptions=True)

        spot_tasks = {name: asyncio.ensure_future(self._fetch_spot_instruments(name)) for name in self.exchanges}
        await asyncio.gather(*spot_tasks.values(), return_exceptions=True)

        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"Failed to fetch {name} instruments: {result}")
                self.instruments[name] = set()
            else:
                self.instruments[name] = result

        # Common pairs = intersection of all exchanges that have instruments
        active = [s for s in self.instruments.values() if s]
        if len(active) >= 2:
            self.common_pairs = set.intersection(*active)
        elif len(active) == 1:
            self.common_pairs = active[0]
        else:
            self.common_pairs = set()

        for name in self.exchanges:
            count = len(self.instruments.get(name, set()))
            logger.info(f"{name.upper()}: {count} instruments")
        logger.info(f"Common pairs across exchanges: {len(self.common_pairs)}")

        if not self.common_pairs:
            exchange_counts = {name: len(self.instruments.get(name, set())) for name in self.exchanges}
            failed = [name for name, count in exchange_counts.items() if count == 0]
            if failed:
                logger.error(
                    "initialize() failed: no instruments from exchanges %s. "
                    "Check API keys and network connectivity.",
                    failed,
                )
            else:
                logger.error(
                    "initialize() warning: exchanges have instruments but no common pairs. "
                    "Counts: %s",
                    exchange_counts,
                )
        return len(self.common_pairs)

    # ─── Public API ───────────────────────────────────────────────────────

    async def update_all(self, symbols: Optional[Set[str]] = None) -> None:
        """Update futures prices, spot prices, and funding rates from all exchanges."""
        await asyncio.gather(
            self.update_futures_prices(),
            self.update_spot_prices(),
            self.update_funding_rates(),
            self.update_fee_rates(),
            return_exceptions=True,
        )

    async def update_futures_prices(self) -> None:
        """Fetch futures ticker data from all exchanges."""
        tasks = {name: self._fetch_futures_prices(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} futures price error: {result}")

    async def update_spot_prices(self) -> None:
        """Fetch spot prices from all exchanges."""
        tasks = {name: self._fetch_spot_prices(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} spot price error: {result}")

    async def update_funding_rates(self) -> None:
        """Fetch funding rates from all exchanges."""
        tasks = {name: self._fetch_funding_rates(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} funding rate error: {result}")

    def get_futures_price(self, exchange: str, symbol: str) -> Optional[TickerData]:
        return self.futures_prices.get(exchange, {}).get(symbol)

    def get_spot_price(self, exchange: str, symbol: str) -> Optional[float]:
        return self.spot_prices.get(exchange, {}).get(symbol)

    def get_funding(self, exchange: str, symbol: str) -> Optional[FundingData]:
        return self.funding_rates.get(exchange, {}).get(symbol)

    def get_contract_size(self, exchange: str, symbol: str) -> float:
        return self.contract_sizes.get(exchange, {}).get(symbol, 1.0)

    def get_tick_size(self, exchange: str, symbol: str) -> float:
        return self.tick_sizes.get(exchange, {}).get(symbol, 0.0)

    def get_min_order_size(self, exchange: str, symbol: str) -> float:
        return self.min_order_sizes.get(exchange, {}).get(symbol, 0.0)

    def get_spot_tick_size(self, exchange: str, symbol: str) -> float:
        return self.spot_tick_sizes.get(exchange, {}).get(symbol, 0.0)

    def get_spot_min_order_size(self, exchange: str, symbol: str) -> float:
        return self.spot_min_order_sizes.get(exchange, {}).get(symbol, 0.0)

    def get_spot_min_notional(self, exchange: str, symbol: str) -> float:
        return self.spot_min_notional.get(exchange, {}).get(symbol, 0.0)

    def get_fee_bps(self) -> Dict[str, Dict[str, float]]:
        return dict(self.fee_bps)

    def get_exchange_names(self) -> List[str]:
        return list(self.exchanges.keys())

    def get_latency(self, exchange: str) -> float:
        return self._latency.get(exchange, 0.0)

    # ─── Instruments ──────────────────────────────────────────────────────

    async def _fetch_instruments(self, exchange: str) -> Set[str]:
        """Fetch available trading instruments from exchange."""
        client = self.exchanges[exchange]
        symbols: Set[str] = set()
        sizes: Dict[str, float] = {}

        try:
            if exchange == "okx":
                result = await client.get_instruments(inst_type="SWAP")
                if result.get("code") == "0":
                    for inst in result.get("data", []):
                        inst_id = inst.get("instId", "")
                        if "-USDT-SWAP" in inst_id:
                            sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                            symbols.add(sym)
                            try:
                                sizes[sym] = float(inst.get("ctVal", 1))
                            except (ValueError, TypeError):
                                sizes[sym] = 1.0
                            try:
                                self.tick_sizes.setdefault(exchange, {})[sym] = float(inst.get("tickSz", 0) or 0)
                            except (ValueError, TypeError):
                                pass
                            try:
                                self.min_order_sizes.setdefault(exchange, {})[sym] = float(inst.get("minSz", 0) or 0)
                            except (ValueError, TypeError):
                                pass

            elif exchange == "htx":
                result = await client.get_instruments()
                if result.get("status") == "ok":
                    for inst in result.get("data", []):
                        cc = inst.get("contract_code", "")
                        if cc.endswith("-USDT"):
                            sym = cc.replace("-", "")
                            symbols.add(sym)
                            try:
                                sizes[sym] = float(inst.get("contract_size", 1))
                            except (ValueError, TypeError):
                                sizes[sym] = 1.0
                            try:
                                self.tick_sizes.setdefault(exchange, {})[sym] = float(inst.get("price_tick", 0) or 0)
                            except (ValueError, TypeError):
                                pass

            elif exchange == "bybit":
                result = await client.get_instruments()
                if result.get("retCode") == 0:
                    for inst in result.get("result", {}).get("list", []):
                        sym = inst.get("symbol", "")
                        if sym.endswith("USDT"):
                            symbols.add(sym)
                            try:
                                sizes[sym] = float(
                                    inst.get("lotSizeFilter", {}).get("qtyStep", 1)
                                )
                            except (ValueError, TypeError):
                                sizes[sym] = 1.0
                            try:
                                self.tick_sizes.setdefault(exchange, {})[sym] = float(
                                    inst.get("priceFilter", {}).get("tickSize", 0) or 0
                                )
                            except (ValueError, TypeError):
                                pass
                            try:
                                self.min_order_sizes.setdefault(exchange, {})[sym] = float(
                                    inst.get("lotSizeFilter", {}).get("minOrderQty", 0) or 0
                                )
                            except (ValueError, TypeError):
                                pass

            elif exchange == "binance":
                result = await client.get_instruments()
                if isinstance(result, dict) and result.get("symbols"):
                    for inst in result["symbols"]:
                        sym = inst.get("symbol", "")
                        if not sym.endswith("USDT") or inst.get("contractType") != "PERPETUAL":
                            continue
                        if inst.get("status") != "TRADING":
                            continue
                        symbols.add(sym)
                        sizes[sym] = 1.0  # Binance linear uses base asset qty
                        for f in inst.get("filters", []):
                            if f.get("filterType") == "PRICE_FILTER":
                                try:
                                    self.tick_sizes.setdefault(exchange, {})[sym] = float(f.get("tickSize", 0) or 0)
                                except (ValueError, TypeError):
                                    pass
                            if f.get("filterType") == "LOT_SIZE":
                                try:
                                    step = float(f.get("stepSize", 0) or 0)
                                    sizes[sym] = step if step > 0 else 1.0
                                except (ValueError, TypeError):
                                    pass
                                try:
                                    self.min_order_sizes.setdefault(exchange, {})[sym] = float(f.get("minQty", 0) or 0)
                                except (ValueError, TypeError):
                                    pass

            self.contract_sizes[exchange] = sizes
        except Exception as e:
            logger.error(f"Instrument fetch error ({exchange}): {e}")

        return symbols

    async def _fetch_spot_instruments(self, exchange: str) -> None:
        client = self.exchanges.get(exchange)
        if not client:
            return
        try:
            if exchange == "okx":
                result = await client.get_spot_instruments()
                if result.get("code") == "0":
                    for inst in result.get("data", []):
                        inst_id = inst.get("instId", "")
                        if not inst_id.endswith("-USDT"):
                            continue
                        sym = inst_id.replace("-", "")
                        try:
                            self.spot_tick_sizes.setdefault(exchange, {})[sym] = float(inst.get("tickSz", 0) or 0)
                        except (ValueError, TypeError):
                            pass
                        try:
                            self.spot_min_order_sizes.setdefault(exchange, {})[sym] = float(inst.get("minSz", 0) or 0)
                        except (ValueError, TypeError):
                            pass
                        try:
                            self.spot_min_notional.setdefault(exchange, {})[sym] = float(inst.get("minNotional", 0) or 0)
                        except (ValueError, TypeError):
                            pass
            elif exchange == "bybit":
                result = await client.get_spot_instruments()
                if result.get("retCode") == 0:
                    for inst in result.get("result", {}).get("list", []):
                        sym = inst.get("symbol", "")
                        if not sym.endswith("USDT"):
                            continue
                        try:
                            self.spot_tick_sizes.setdefault(exchange, {})[sym] = float(
                                inst.get("priceFilter", {}).get("tickSize", 0) or 0
                            )
                        except (ValueError, TypeError):
                            pass
                        try:
                            self.spot_min_order_sizes.setdefault(exchange, {})[sym] = float(
                                inst.get("lotSizeFilter", {}).get("minOrderQty", 0) or 0
                            )
                        except (ValueError, TypeError):
                            pass
                        try:
                            self.spot_min_notional.setdefault(exchange, {})[sym] = float(
                                inst.get("lotSizeFilter", {}).get("minOrderAmt", 0) or 0
                            )
                        except (ValueError, TypeError):
                            pass
            elif exchange == "htx":
                result = await client.get_spot_symbols()
                if result.get("status") == "ok":
                    for item in result.get("data", []):
                        sym = str(item.get("symbol", "")).upper()
                        if not sym.endswith("USDT"):
                            continue
                        price_prec = int(item.get("price-precision", 0) or 0)
                        amt_prec = int(item.get("amount-precision", 0) or 0)
                        tick = 10 ** (-price_prec) if price_prec > 0 else 0.0
                        min_qty = 10 ** (-amt_prec) if amt_prec > 0 else 0.0
                        self.spot_tick_sizes.setdefault(exchange, {})[sym] = tick
                        self.spot_min_order_sizes.setdefault(exchange, {})[sym] = min_qty
            elif exchange == "binance":
                result = await client.get_spot_instruments()
                if isinstance(result, dict) and result.get("symbols"):
                    for inst in result["symbols"]:
                        sym = inst.get("symbol", "")
                        if not sym.endswith("USDT") or inst.get("status") != "TRADING":
                            continue
                        for f in inst.get("filters", []):
                            if f.get("filterType") == "PRICE_FILTER":
                                try:
                                    self.spot_tick_sizes.setdefault(exchange, {})[sym] = float(f.get("tickSize", 0) or 0)
                                except (ValueError, TypeError):
                                    pass
                            if f.get("filterType") == "LOT_SIZE":
                                try:
                                    self.spot_min_order_sizes.setdefault(exchange, {})[sym] = float(f.get("minQty", 0) or 0)
                                except (ValueError, TypeError):
                                    pass
                            if f.get("filterType") == "NOTIONAL":
                                try:
                                    self.spot_min_notional.setdefault(exchange, {})[sym] = float(f.get("minNotional", 0) or 0)
                                except (ValueError, TypeError):
                                    pass
        except Exception as e:
            logger.error(f"{exchange.upper()} spot instrument fetch error: {e}")

    # ─── Futures Prices ───────────────────────────────────────────────────

    async def _fetch_futures_prices(self, exchange: str) -> int:
        """Fetch futures tickers. Returns count of updated symbols."""
        client = self.exchanges[exchange]
        updated = 0
        t0 = time.time()

        try:
            if exchange == "okx":
                result = await client.get_tickers(inst_type="SWAP")
                if result.get("code") == "0":
                    prices = {}
                    for t in result.get("data", []):
                        inst_id = t.get("instId", "")
                        if "-USDT-SWAP" not in inst_id:
                            continue
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        try:
                            bid = float(t.get("bidPx") or 0)
                            ask = float(t.get("askPx") or 0)
                            last = float(t.get("last") or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = TickerData(bid=bid, ask=ask, last=last)
                                updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.futures_prices["okx"] = prices

            elif exchange == "htx":
                result = await client.get_tickers()
                if result.get("status") == "ok":
                    prices = {}
                    for t in result.get("ticks", []):
                        cc = t.get("contract_code", "")
                        if not cc.endswith("-USDT"):
                            continue
                        sym = cc.replace("-", "")
                        try:
                            bid_data = t.get("bid") or []
                            ask_data = t.get("ask") or []
                            bid = float(bid_data[0]) if bid_data else 0.0
                            ask = float(ask_data[0]) if ask_data else 0.0
                            close = float(t.get("close", 0) or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = TickerData(bid=bid, ask=ask, last=close)
                                updated += 1
                        except (ValueError, TypeError, IndexError):
                            pass
                    self.futures_prices["htx"] = prices

            elif exchange == "bybit":
                result = await client.get_tickers()
                if result.get("retCode") == 0:
                    prices = {}
                    for t in result.get("result", {}).get("list", []):
                        sym = t.get("symbol", "")
                        if not sym.endswith("USDT"):
                            continue
                        try:
                            bid = float(t.get("bid1Price") or 0)
                            ask = float(t.get("ask1Price") or 0)
                            last = float(t.get("lastPrice") or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = TickerData(bid=bid, ask=ask, last=last)
                                updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.futures_prices["bybit"] = prices

            elif exchange == "binance":
                result = await client.get_tickers()
                data = result.get("data", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                prices = {}
                for t in data:
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    try:
                        bid = float(t.get("bidPrice") or 0)
                        ask = float(t.get("askPrice") or 0)
                        if bid > 0 and ask > 0:
                            prices[sym] = TickerData(bid=bid, ask=ask, last=(bid + ask) / 2)
                            updated += 1
                    except (ValueError, TypeError):
                        pass
                self.futures_prices["binance"] = prices

        except Exception as e:
            logger.error(f"{exchange.upper()} futures fetch error: {e}")

        elapsed = time.time() - t0
        self._latency[exchange] = elapsed
        self._last_update[exchange] = time.time()
        return updated

    # ─── Spot Prices ──────────────────────────────────────────────────────

    async def _fetch_spot_prices(self, exchange: str) -> int:
        """Fetch spot prices. Returns count."""
        client = self.exchanges[exchange]
        updated = 0

        try:
            if exchange == "okx":
                result = await client.get_spot_tickers()
                if result.get("code") == "0":
                    prices = {}
                    for t in result.get("data", []):
                        inst_id = t.get("instId", "")
                        if not inst_id.endswith("-USDT"):
                            continue
                        sym = inst_id.replace("-", "")
                        try:
                            bid = float(t.get("bidPx") or 0)
                            ask = float(t.get("askPx") or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = (bid + ask) / 2
                                updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.spot_prices["okx"] = prices

            elif exchange == "htx":
                result = await client.get_spot_tickers()
                if result.get("status") == "ok":
                    prices = {}
                    for t in result.get("data", []):
                        sym = t.get("symbol", "").upper()
                        try:
                            bid = float(t.get("bid") or 0)
                            ask = float(t.get("ask") or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = (bid + ask) / 2
                                updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.spot_prices["htx"] = prices

            elif exchange == "bybit":
                result = await client.get_spot_tickers()
                if result.get("retCode") == 0:
                    prices = {}
                    for t in result.get("result", {}).get("list", []):
                        sym = t.get("symbol", "")
                        try:
                            bid = float(t.get("bid1Price") or 0)
                            ask = float(t.get("ask1Price") or 0)
                            if bid > 0 and ask > 0:
                                prices[sym] = (bid + ask) / 2
                                updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.spot_prices["bybit"] = prices

            elif exchange == "binance":
                result = await client.get_spot_tickers()
                data = result.get("data", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                prices = {}
                for t in data:
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    try:
                        bid = float(t.get("bidPrice") or 0)
                        ask = float(t.get("askPrice") or 0)
                        if bid > 0 and ask > 0:
                            prices[sym] = (bid + ask) / 2
                            updated += 1
                    except (ValueError, TypeError):
                        pass
                self.spot_prices["binance"] = prices

        except Exception as e:
            logger.error(f"{exchange.upper()} spot fetch error: {e}")

        return updated

    # ─── Funding Rates ────────────────────────────────────────────────────

    async def _fetch_funding_rates(self, exchange: str) -> int:
        """Fetch funding rates. Returns count."""
        client = self.exchanges[exchange]
        updated = 0

        try:
            if exchange == "okx":
                # Pass configured symbols as OKX inst IDs for targeted fetch
                okx_inst_ids = [self._sym_to_okx_inst(s) for s in self.common_pairs] if self.common_pairs else None
                result = await client.get_funding_rates_all(symbols=okx_inst_ids)
                if result.get("code") == "0":
                    rates = {}
                    for t in result.get("data", []):
                        inst_id = t.get("instId", "")
                        if "-USDT-SWAP" not in inst_id:
                            continue
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        try:
                            rate_str = t.get("fundingRate") or t.get("nextFundingRate")
                            if rate_str is None:
                                continue
                            rate = float(rate_str)
                            next_ts = int(t.get("nextFundingTime", 0) or 0)
                            rates[sym] = FundingData(
                                rate=rate,
                                rate_pct=rate * 100,
                                next_time_ms=next_ts,
                            )
                            updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.funding_rates["okx"] = rates

            elif exchange == "htx":
                result = await client.get_funding_rates()
                # HTX v3 uses "code": 200, v1 uses "status": "ok"
                is_ok = result.get("status") == "ok" or result.get("code") == 200 or str(result.get("code")) == "200"
                if is_ok:
                    rates = {}
                    data = result.get("data", [])
                    # v3 might wrap in {"data": {"data": [...]}}
                    if isinstance(data, dict):
                        data = data.get("data", [])
                    if not isinstance(data, list):
                        data = []
                    for t in data:
                        cc = t.get("contract_code", "")
                        if not cc.endswith("-USDT"):
                            continue
                        sym = cc.replace("-", "")
                        try:
                            rate_str = t.get("funding_rate")
                            if rate_str is None:
                                continue
                            rate = float(rate_str)
                            next_ts = int(t.get("settlement_time", 0) or 0)
                            rates[sym] = FundingData(
                                rate=rate,
                                rate_pct=rate * 100,
                                next_time_ms=next_ts,
                            )
                            updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.funding_rates["htx"] = rates
                    if not rates:
                        logger.warning(f"HTX funding: response OK but 0 rates parsed. Keys: {list(result.keys())}, data type: {type(result.get('data'))}")

            elif exchange == "bybit":
                # Bybit tickers include fundingRate
                result = await client.get_tickers()
                if result.get("retCode") == 0:
                    rates = {}
                    for t in result.get("result", {}).get("list", []):
                        sym = t.get("symbol", "")
                        if not sym.endswith("USDT"):
                            continue
                        try:
                            rate_str = t.get("fundingRate")
                            if not rate_str:
                                continue
                            rate = float(rate_str)
                            next_ts = int(t.get("nextFundingTime", 0) or 0)
                            rates[sym] = FundingData(
                                rate=rate,
                                rate_pct=rate * 100,
                                next_time_ms=next_ts,
                            )
                            updated += 1
                        except (ValueError, TypeError):
                            pass
                    self.funding_rates["bybit"] = rates

            elif exchange == "binance":
                result = await client.get_funding_rates()
                data = result.get("data", []) if isinstance(result, dict) else result if isinstance(result, list) else []
                rates = {}
                for t in data:
                    sym = t.get("symbol", "")
                    if not sym.endswith("USDT"):
                        continue
                    try:
                        rate_str = t.get("lastFundingRate")
                        if not rate_str:
                            continue
                        rate = float(rate_str)
                        next_ts = int(t.get("nextFundingTime", 0) or 0)
                        rates[sym] = FundingData(
                            rate=rate,
                            rate_pct=rate * 100,
                            next_time_ms=next_ts,
                        )
                        updated += 1
                    except (ValueError, TypeError):
                        pass
                self.funding_rates["binance"] = rates

        except Exception as e:
            logger.error(f"{exchange.upper()} funding fetch error: {e}")

        return updated

    # ─── Orderbook Depth ─────────────────────────────────────────────────

    async def fetch_orderbook_depth(self, exchange: str, symbol: str, levels: int = 10) -> Optional[Dict[str, Any]]:
        """Fetch orderbook depth and return normalized {bids: [[price, qty], ...], asks: [...], timestamp: float}.
        Returns None on failure."""
        client = self.exchanges.get(exchange)
        if not client:
            return None
        try:
            if exchange == "okx":
                inst_id = self._sym_to_okx_inst(symbol)
                result = await client.get_orderbook(inst_id, sz=levels)
                data_list = result.get("data", [])
                if not data_list:
                    return None
                book = data_list[0]
                bids = [[float(row[0]), float(row[1])] for row in book.get("bids", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in book.get("asks", [])[:levels]]
            elif exchange == "htx":
                cc = self._sym_to_htx_cc(symbol)
                result = await client.get_orderbook(cc, category="linear", limit=levels)
                tick = result.get("tick", {})
                bids = [[float(row[0]), float(row[1])] for row in tick.get("bids", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in tick.get("asks", [])[:levels]]
            elif exchange == "bybit":
                result = await client.get_orderbook(symbol, category="linear", limit=levels)
                book = result.get("result", {})
                bids = [[float(row[0]), float(row[1])] for row in book.get("b", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in book.get("a", [])[:levels]]
            elif exchange == "binance":
                result = await client.get_orderbook(symbol, limit=levels)
                bids = [[float(row[0]), float(row[1])] for row in (result.get("bids") or [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in (result.get("asks") or [])[:levels]]
            else:
                return None
            return {"bids": bids, "asks": asks, "timestamp": time.time()}
        except Exception as e:
            logger.warning(f"{exchange.upper()} orderbook depth error ({symbol}): {e}")
            return None

    async def fetch_spot_orderbook_depth(self, exchange: str, symbol: str, levels: int = 10) -> Optional[Dict[str, Any]]:
        """Fetch spot orderbook depth and return normalized {bids: [[price, qty], ...], asks: [...], timestamp: float}.
        Returns None on failure."""
        client = self.exchanges.get(exchange)
        if not client:
            return None
        try:
            if exchange == "okx":
                inst_id = symbol.replace("USDT", "-USDT") if symbol.endswith("USDT") else symbol.replace("-", "")
                result = await client._public_request("GET", "/api/v5/market/books", {"instId": inst_id, "sz": levels})
                data_list = result.get("data", [])
                if not data_list:
                    return None
                book = data_list[0]
                bids = [[float(row[0]), float(row[1])] for row in book.get("bids", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in book.get("asks", [])[:levels]]
            elif exchange == "htx":
                result = await client.get_spot_orderbook(symbol, depth=levels)
                tick = result.get("tick", {})
                bids = [[float(row[0]), float(row[1])] for row in tick.get("bids", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in tick.get("asks", [])[:levels]]
            elif exchange == "bybit":
                result = await client._public_request(
                    "GET", "/v5/market/orderbook", {"category": "spot", "symbol": symbol, "limit": levels}
                )
                book = result.get("result", {})
                bids = [[float(row[0]), float(row[1])] for row in book.get("b", [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in book.get("a", [])[:levels]]
            elif exchange == "binance":
                result = await client.get_spot_orderbook(symbol, limit=levels)
                bids = [[float(row[0]), float(row[1])] for row in (result.get("bids") or [])[:levels]]
                asks = [[float(row[0]), float(row[1])] for row in (result.get("asks") or [])[:levels]]
            else:
                return None
            return {"bids": bids, "asks": asks, "timestamp": time.time()}
        except Exception as e:
            logger.warning(f"{exchange.upper()} spot orderbook error ({symbol}): {e}")
            return None

    async def update_fee_rates(self) -> None:
        tasks = {name: self._fetch_fee_rates(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} fee rate error: {result}")

    async def _fetch_fee_rates(self, exchange: str) -> None:
        client = self.exchanges[exchange]
        try:
            if exchange == "okx":
                spot = await client.get_trade_fee(inst_type="SPOT")
                perp = await client.get_trade_fee(inst_type="SWAP")
                self._parse_okx_fees(exchange, spot, perp)
                symbols_raw = os.getenv("FEE_SYMBOLS", "")
                symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
                for sym in symbols:
                    inst_spot = sym.replace("USDT", "-USDT") if sym.endswith("USDT") else sym
                    inst_swap = sym.replace("USDT", "-USDT-SWAP") if sym.endswith("USDT") else f"{sym}-SWAP"
                    spot_sym = await client.get_trade_fee(inst_type="SPOT", inst_id=inst_spot)
                    perp_sym = await client.get_trade_fee(inst_type="SWAP", inst_id=inst_swap)
                    self._parse_okx_symbol_fees(exchange, sym, spot_sym, perp_sym)
            elif exchange == "htx":
                # HTX doesn't have a dedicated fee rate endpoint for linear swaps;
                # use documented default taker rates.
                # HTX USDT-M linear swap taker: 0.05% = 5 bps (VIP0).
                # HTX spot taker: 0.20% = 20 bps (VIP0).
                self.fee_bps.setdefault(exchange, {})
                self.fee_bps[exchange]["perp"] = float(os.getenv("FEE_BPS_HTX_PERP", "5.0"))
                self.fee_bps[exchange]["spot"] = float(os.getenv("FEE_BPS_HTX_SPOT", "20.0"))
            elif exchange == "bybit":
                spot = await client.get_fee_rates(category="spot")
                perp = await client.get_fee_rates(category="linear")
                self._parse_bybit_fees(exchange, spot, perp)
            elif exchange == "binance":
                # Binance commission rate endpoint
                try:
                    result = await client.get_fee_rates()
                    if isinstance(result, dict) and "takerCommissionRate" in result:
                        taker = float(result.get("takerCommissionRate", 0) or 0) * 10_000
                        self.fee_bps[exchange] = {"spot": taker, "perp": taker}
                except Exception:
                    self.fee_bps.setdefault(exchange, {"spot": 4.0, "perp": 4.0})
        except Exception as e:
            logger.error(f"{exchange.upper()} fee fetch error: {e}")

    def _parse_okx_fees(self, exchange: str, spot: Dict[str, Any], perp: Dict[str, Any]) -> None:
        def _fee(resp, default=0.0):
            try:
                data = resp.get("data", [])
                if data:
                    # OKX returns taker fee as negative (e.g. -0.0005 = 5 bps)
                    taker = abs(float(data[0].get("taker", 0) or 0))
                    return taker * 10_000
            except Exception:
                pass
            return default
        self.fee_bps[exchange] = {"spot": _fee(spot), "perp": _fee(perp)}

    def _parse_okx_symbol_fees(self, exchange: str, symbol: str, spot: Dict[str, Any], perp: Dict[str, Any]) -> None:
        def _fee(resp, default=0.0):
            try:
                data = resp.get("data", [])
                if data:
                    # OKX returns taker fee as negative (e.g. -0.0005 = 5 bps)
                    taker = abs(float(data[0].get("taker", 0) or 0))
                    return taker * 10_000
            except Exception:
                pass
            return default
        self.fee_bps.setdefault(exchange, {})
        self.fee_bps[exchange][f"spot:{symbol}"] = _fee(spot)
        self.fee_bps[exchange][f"perp:{symbol}"] = _fee(perp)

    def _parse_bybit_fees(self, exchange: str, spot: Dict[str, Any], perp: Dict[str, Any]) -> None:
        def _fee(resp, default=0.0):
            try:
                result = resp.get("result", {})
                list_data = result.get("list", [])
                if list_data:
                    taker = float(list_data[0].get("takerFeeRate", 0) or 0)
                    return taker * 10_000
            except Exception:
                pass
            return default
        self.fee_bps[exchange] = {"spot": _fee(spot), "perp": _fee(perp)}

        def _per_symbol(resp, market: str) -> None:
            try:
                result = resp.get("result", {})
                list_data = result.get("list", [])
                for item in list_data:
                    symbol = item.get("symbol")
                    if not symbol:
                        continue
                    taker = float(item.get("takerFeeRate", 0) or 0) * 10_000
                    self.fee_bps[exchange][f"{market}:{symbol}"] = taker
            except Exception:
                return None

        _per_symbol(spot, "spot")
        _per_symbol(perp, "perp")

    # ─── Open Interest ─────────────────────────────────────────────────────

    async def fetch_open_interest(self, exchange: str) -> Dict[str, float]:
        """Fetch open interest for all USDT symbols. Returns {symbol: oi_value}."""
        client = self.exchanges.get(exchange)
        if not client:
            return {}
        result: Dict[str, float] = {}
        try:
            if exchange == "okx":
                symbols_raw = os.getenv("MI_SYMBOLS") or os.getenv("SYMBOLS") or ""
                symbols = [s.strip().upper() for s in symbols_raw.split(",") if s.strip()]
                if not symbols:
                    symbols = sorted(self.common_pairs)[:20]

                for sym in symbols:
                    # Skip symbols not in OKX instruments
                    if sym not in self.instruments.get("okx", set()):
                        continue
                    inst_id = self._sym_to_okx_inst(sym)
                    resp = await client._public_request(
                        "GET",
                        "/api/v5/public/open-interest",
                        {"instType": "SWAP", "instId": inst_id},
                    )
                    # Skip errors: 51001 = instrument doesn't exist (delisted)
                    if resp.get("code") not in ("0", "51001"):
                        continue
                    if resp.get("code") == "51001":
                        logger.debug(f"OKX OI: instrument not found {sym}")
                        continue
                    for item in resp.get("data", []):
                        if item.get("instId") != inst_id:
                            continue
                        try:
                            result[sym] = float(item.get("oi", 0) or 0)
                        except (ValueError, TypeError):
                            pass
                        break
            elif exchange == "htx":
                resp = await client._public_request("GET", "https://api.hbdm.com", "/linear-swap-api/v1/swap_open_interest", {"contract_type": "swap", "business_type": "swap"})
                if resp.get("status") == "ok":
                    for item in resp.get("data", []):
                        cc = item.get("contract_code", "")
                        if not cc.endswith("-USDT"):
                            continue
                        sym = cc.replace("-", "")
                        try:
                            result[sym] = float(item.get("volume", 0) or 0)
                        except (ValueError, TypeError):
                            pass
            elif exchange == "bybit":
                resp = await client._public_request("GET", "/v5/market/open-interest", {"category": "linear", "symbol": "", "limit": "200"})
                if resp.get("retCode") == 0:
                    for item in resp.get("result", {}).get("list", []):
                        sym = item.get("symbol", "")
                        if sym.endswith("USDT"):
                            try:
                                result[sym] = float(item.get("openInterest", 0) or 0)
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            logger.error(f"{exchange.upper()} open interest error: {e}")
        return result

    # ─── OHLCV Candles ───────────────────────────────────────────────────

    async def fetch_ohlcv(self, exchange: str, symbol: str, timeframe: str = "1H", limit: int = 100) -> List[Dict]:
        """Fetch OHLCV candles. Returns list of {ts, o, h, l, c, vol}."""
        client = self.exchanges.get(exchange)
        if not client:
            return []
        candles: List[Dict] = []
        try:
            if exchange == "okx":
                inst_id = self._sym_to_okx_inst(symbol)
                bar_map = {"1m": "1m", "5m": "5m", "1H": "1H", "4H": "4H", "1D": "1D"}
                bar = bar_map.get(timeframe, "1H")
                resp = await client._public_request("GET", "/api/v5/market/candles", {"instId": inst_id, "bar": bar, "limit": str(limit)})
                if resp.get("code") == "0":
                    for c in resp.get("data", []):
                        try:
                            candles.append({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "vol": float(c[5])})
                        except (ValueError, TypeError, IndexError):
                            pass
            elif exchange == "htx":
                cc = self._sym_to_htx_cc(symbol)
                period_map = {"1m": "1min", "5m": "5min", "1H": "60min", "4H": "4hour", "1D": "1day"}
                period = period_map.get(timeframe, "60min")
                resp = await client._public_request("GET", "https://api.hbdm.com", f"/linear-swap-ex/market/history/kline", {"contract_code": cc, "period": period, "size": str(limit)})
                if resp.get("status") == "ok":
                    for c in resp.get("data", []):
                        try:
                            candles.append({"ts": int(c.get("id", 0)) * 1000, "o": float(c.get("open", 0)), "h": float(c.get("high", 0)), "l": float(c.get("low", 0)), "c": float(c.get("close", 0)), "vol": float(c.get("amount", 0))})
                        except (ValueError, TypeError):
                            pass
            elif exchange == "bybit":
                interval_map = {"1m": "1", "5m": "5", "1H": "60", "4H": "240", "1D": "D"}
                interval = interval_map.get(timeframe, "60")
                resp = await client._public_request("GET", "/v5/market/kline", {"category": "linear", "symbol": symbol, "interval": interval, "limit": str(limit)})
                if resp.get("retCode") == 0:
                    for c in resp.get("result", {}).get("list", []):
                        try:
                            candles.append({"ts": int(c[0]), "o": float(c[1]), "h": float(c[2]), "l": float(c[3]), "c": float(c[4]), "vol": float(c[5])})
                        except (ValueError, TypeError, IndexError):
                            pass
        except Exception as e:
            logger.error(f"{exchange.upper()} OHLCV error ({symbol} {timeframe}): {e}")
        return candles

    # ─── Long/Short Ratio ────────────────────────────────────────────────

    async def fetch_long_short_ratio(self, exchange: str, symbol: str) -> Optional[float]:
        """Fetch long/short account ratio. Returns ratio or None."""
        client = self.exchanges.get(exchange)
        if not client:
            return None
        try:
            if exchange == "okx":
                inst_id = self._sym_to_okx_inst(symbol)
                # OKX V5: /api/v5/rubik/stat/contracts/long-short-account-ratio
                # Uses ccy (e.g. "BTC") not instId
                ccy = inst_id.split("-")[0]  # BTC-USDT-SWAP → BTC
                resp = await client._public_request(
                    "GET",
                    "/api/v5/rubik/stat/contracts/long-short-account-ratio",
                    {"ccy": ccy, "period": "5m"},
                )
                if resp.get("code") == "0" and resp.get("data"):
                    # data: [[ts, longShortRatio], ...]
                    row = resp["data"][0]
                    return float(row[1]) if isinstance(row, list) else float(row.get("longShortRatio", 1.0))
            elif exchange == "htx":
                cc = self._sym_to_htx_cc(symbol)
                resp = await client._public_request("GET", "https://api.hbdm.com", "/linear-swap-api/v1/swap_elite_account_ratio", {"contract_code": cc, "period": "5min"})
                if resp.get("status") == "ok" and resp.get("data"):
                    d = resp["data"][-1] if isinstance(resp["data"], list) and resp["data"] else resp["data"]
                    buy = float(d.get("buy_ratio", 0.5))
                    sell = float(d.get("sell_ratio", 0.5))
                    return buy / max(sell, 1e-9)
        except Exception as e:
            logger.error(f"{exchange.upper()} long/short ratio error ({symbol}): {e}")
        return None

    # ─── Liquidations ────────────────────────────────────────────────────

    async def fetch_recent_liquidations(self, exchange: str, symbol: str) -> float:
        """Fetch recent liquidation volume (contracts). Returns total or 0."""
        client = self.exchanges.get(exchange)
        if not client:
            return 0.0
        try:
            if exchange == "okx":
                inst_id = self._sym_to_okx_inst(symbol)
                inst_family = inst_id.replace("-SWAP", "")  # BTC-USDT
                resp = await client._public_request("GET", "/api/v5/public/liquidation-orders", {"instType": "SWAP", "instFamily": inst_family, "state": "filled"})
                if resp.get("code") == "0":
                    total = 0.0
                    for item in resp.get("data", []):
                        for d in item.get("details", []):
                            try:
                                total += abs(float(d.get("sz", 0) or 0))
                            except (ValueError, TypeError):
                                pass
                    return total
            elif exchange == "htx":
                cc = self._sym_to_htx_cc(symbol)
                resp = await client._public_request("GET", "https://api.hbdm.com", "/linear-swap-api/v3/swap_liquidation_orders", {"contract": cc, "pair": cc.replace("-USDT", ""), "trade_type": "0"})
                if resp.get("code") == 200 or resp.get("status") == "ok":
                    total = 0.0
                    data = resp.get("data", {})
                    if isinstance(data, list):
                        orders = data
                    else:
                        orders = data.get("orders", []) if isinstance(data, dict) else []
                    for item in orders:
                        try:
                            total += abs(float(item.get("amount", 0) or 0))
                        except (ValueError, TypeError):
                            pass
                    return total
        except Exception as e:
            logger.error(f"{exchange.upper()} liquidation error ({symbol}): {e}")
        return 0.0

    # ─── 24h Volume ──────────────────────────────────────────────────────

    async def fetch_24h_volumes(self, exchange: str) -> Dict[str, float]:
        """Fetch 24h volume for all USDT symbols. Returns {symbol: volume}."""
        # OKX and Bybit include vol24h in tickers; HTX in batch_merged.
        # We reuse already-fetched ticker data when possible.
        result: Dict[str, float] = {}
        client = self.exchanges.get(exchange)
        if not client:
            return result
        try:
            if exchange == "okx":
                resp = await client._public_request("GET", "/api/v5/market/tickers", {"instType": "SWAP"})
                if resp.get("code") == "0":
                    for t in resp.get("data", []):
                        inst_id = t.get("instId", "")
                        if "-USDT-SWAP" not in inst_id:
                            continue
                        sym = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
                        try:
                            result[sym] = float(t.get("volCcy24h") or t.get("vol24h") or 0)
                        except (ValueError, TypeError):
                            pass
            elif exchange == "htx":
                resp = await client._public_request("GET", "https://api.hbdm.com", "/linear-swap-ex/market/detail/batch_merged")
                if resp.get("status") == "ok":
                    for t in resp.get("ticks", []):
                        cc = t.get("contract_code", "")
                        if not cc.endswith("-USDT"):
                            continue
                        sym = cc.replace("-", "")
                        try:
                            result[sym] = float(t.get("amount", 0) or 0)
                        except (ValueError, TypeError):
                            pass
            elif exchange == "bybit":
                resp = await client._public_request("GET", "/v5/market/tickers", {"category": "linear"})
                if resp.get("retCode") == 0:
                    for t in resp.get("result", {}).get("list", []):
                        sym = t.get("symbol", "")
                        if sym.endswith("USDT"):
                            try:
                                result[sym] = float(t.get("volume24h") or 0)
                            except (ValueError, TypeError):
                                pass
        except Exception as e:
            logger.error(f"{exchange.upper()} 24h volume error: {e}")
        return result

    # ─── Symbol Mapping Helpers ──────────────────────────────────────────

    @staticmethod
    def _sym_to_okx_inst(symbol: str) -> str:
        """BTCUSDT -> BTC-USDT-SWAP"""
        base = symbol.replace("USDT", "")
        return f"{base}-USDT-SWAP"

    @staticmethod
    def _sym_to_htx_cc(symbol: str) -> str:
        """BTCUSDT -> BTC-USDT"""
        base = symbol.replace("USDT", "")
        return f"{base}-USDT"

    # ─── Balance ──────────────────────────────────────────────────────────

    async def fetch_balances(self) -> Dict[str, float]:
        """Fetch USDT balance from all exchanges. Returns {exchange: balance}.

        Returns -1.0 for exchanges where the fetch failed (timeout, auth error, etc.)
        so callers can distinguish "zero balance" from "fetch error".
        """
        balances: Dict[str, float] = {}
        tasks = {name: self._fetch_balance(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} balance error: {result}")
                balances[name] = -1.0  # signal: fetch failed, don't override cache
            else:
                balances[name] = result
        return balances

    async def _fetch_balance(self, exchange: str) -> float:
        """Fetch USDT balance for one exchange."""
        client = self.exchanges[exchange]

        try:
            if exchange == "okx":
                result = await client.get_balance()
                if result.get("code") == "0" and result.get("data"):
                    for detail in result["data"][0].get("details", []):
                        if detail.get("ccy") == "USDT":
                            return float(detail.get("availBal", 0) or 0)

            elif exchange == "htx":
                result = await client.get_balance()
                logger.debug("HTX balance response keys=%s", list(result.keys()) if isinstance(result, dict) else type(result))
                data = None
                # v3 unified: {"code": 200, "data": [...]}
                if result.get("code") == 200 and result.get("data"):
                    data = result["data"]
                # v1 cross: {"status": "ok", "data": [...]}
                elif result.get("status") == "ok" and result.get("data"):
                    data = result["data"]
                if data is not None:
                    items = data if isinstance(data, list) else [data]
                    for item in items:
                        if item.get("margin_asset") == "USDT" or not item.get("margin_asset"):
                            # Try all known balance field names
                            for fld in ("margin_available", "withdraw_available",
                                         "margin_balance", "margin_static"):
                                val = item.get(fld)
                                if val is not None:
                                    try:
                                        v = float(val)
                                        if v > 0:
                                            return v
                                    except (ValueError, TypeError):
                                        continue
                    # Parsed but all zero — log for debugging
                    logger.warning("HTX balance: all fields zero/missing in response: %s",
                                   [{k: v for k, v in item.items() if "margin" in k or "balance" in k or "available" in k or "withdraw" in k}
                                    for item in items[:2]])
                    return 0.0

            elif exchange == "bybit":
                result = await client.get_balance()
                if result.get("retCode") == 0 and result.get("result"):
                    for coin in result["result"].get("list", [{}])[0].get("coin", []):
                        if coin.get("coin") == "USDT":
                            for fld in ["availableToWithdraw", "walletBalance", "equity"]:
                                val = coin.get(fld, "")
                                if val and val != "0":
                                    try:
                                        return float(val)
                                    except (ValueError, TypeError):
                                        continue

            elif exchange == "binance":
                result = await client.get_balance()
                data = result if isinstance(result, list) else result.get("data", []) if isinstance(result, dict) else []
                for item in data:
                    if item.get("asset") == "USDT":
                        try:
                            return float(item.get("availableBalance") or item.get("balance") or 0)
                        except (ValueError, TypeError):
                            pass
        except Exception as e:
            logger.error(f"{exchange.upper()} balance fetch error: {e}")

        return -1.0  # signal: fetch failed, don't override cache
