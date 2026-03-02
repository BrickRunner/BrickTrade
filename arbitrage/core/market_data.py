"""
Unified Market Data Engine.

Fetches prices, funding rates, and spot prices from all exchanges
via REST polling. Provides a single interface for all strategies.
"""
import asyncio
import time
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
        return len(self.common_pairs)

    # ─── Public API ───────────────────────────────────────────────────────

    async def update_all(self, symbols: Optional[Set[str]] = None) -> None:
        """Update futures prices, spot prices, and funding rates from all exchanges."""
        await asyncio.gather(
            self.update_futures_prices(),
            self.update_spot_prices(),
            self.update_funding_rates(),
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

            self.contract_sizes[exchange] = sizes
        except Exception as e:
            logger.error(f"Instrument fetch error ({exchange}): {e}")

        return symbols

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
                result = await client.get_funding_rates_all()
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
                if result.get("status") == "ok":
                    rates = {}
                    for t in result.get("data", []):
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

        except Exception as e:
            logger.error(f"{exchange.upper()} funding fetch error: {e}")

        return updated

    # ─── Balance ──────────────────────────────────────────────────────────

    async def fetch_balances(self) -> Dict[str, float]:
        """Fetch USDT balance from all exchanges. Returns {exchange: balance}."""
        balances: Dict[str, float] = {}
        tasks = {name: self._fetch_balance(name) for name in self.exchanges}
        results = await asyncio.gather(*tasks.values(), return_exceptions=True)
        for name, result in zip(tasks.keys(), results):
            if isinstance(result, Exception):
                logger.error(f"{name.upper()} balance error: {result}")
                balances[name] = 0.0
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
                if result.get("code") == 200 and result.get("data"):
                    data = result["data"]
                    if isinstance(data, list):
                        for item in data:
                            if item.get("margin_asset") == "USDT":
                                return float(item.get("withdraw_available", 0) or 0)

            elif exchange == "bybit":
                result = await client.get_balance()
                if result.get("retCode") == 0 and result.get("result"):
                    for coin in result["result"].get("list", [{}])[0].get("coin", []):
                        if coin.get("coin") == "USDT":
                            for field in ["availableToWithdraw", "walletBalance", "equity"]:
                                val = coin.get(field, "")
                                if val and val != "0":
                                    try:
                                        return float(val)
                                    except (ValueError, TypeError):
                                        continue
        except Exception as e:
            logger.error(f"{exchange.upper()} balance fetch error: {e}")

        return 0.0
