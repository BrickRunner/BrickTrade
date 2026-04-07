from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, List, Optional, Tuple

from arbitrage.core.market_data import MarketDataEngine
from market_intelligence.models import OHLCV, PairSnapshot
from market_intelligence.rate_limiter import get_exchange_rate_limiters

logger = logging.getLogger("market_intelligence")


@dataclass
class ExchangeCircuitBreaker:
    """Data collection circuit breaker (separate from trade execution breaker).

    This circuit breaker protects the market_intelligence module's REST polling
    against exchange outages with shorter cooldowns (300s vs 600s for the
    trading circuit breaker in arbitrage/system/circuit_breaker.py).

    Different concerns:
    - Trading breaker: prevents placing orders on unhealthy exchanges (money at risk)
    - Data breaker: stops wasted REST polling on unhealthy exchanges (rate limit risk)

    Both track the same exchanges but with independent error counts and cooldown timers.
    """
    exchange: str
    failure_count: int = 0
    last_failure: float = 0.0
    disabled_until: float = 0.0

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure = time.time()
        backoff = min(300.0, 30.0 * (2 ** min(self.failure_count - 1, 4)))
        self.disabled_until = self.last_failure + backoff
        logger.warning("Circuit breaker: %s disabled for %.0fs (failures=%d)", self.exchange, backoff, self.failure_count)

    def record_success(self) -> None:
        if self.failure_count > 0:
            logger.info("Circuit breaker: %s re-enabled after recovery", self.exchange)
        self.failure_count = 0
        self.disabled_until = 0.0

    def is_available(self) -> bool:
        if self.failure_count == 0:
            return True
        return time.time() >= self.disabled_until


# Funding settlement interval in hours per exchange (used to normalize rates to 8h equivalent).
FUNDING_INTERVAL_HOURS: Dict[str, float] = {
    "okx": 8,
    "htx": 8,
    "binance": 8,
    "bybit": 8,
}


@dataclass
class CollectorState:
    prices: Dict[str, Deque[float]]
    bids: Dict[str, Deque[float]]
    asks: Dict[str, Deque[float]]
    spot: Dict[str, Deque[float]]
    funding: Dict[str, Deque[float]]
    basis_bps: Dict[str, Deque[float]]
    open_interest: Dict[str, Deque[float]]
    long_short_ratio: Dict[str, Deque[float]]
    liquidation_score: Dict[str, Deque[float]]
    volume_proxy: Dict[str, Deque[float]]
    spread_bps: Dict[str, Deque[float]]
    orderbook_imbalance: Dict[str, Deque[float]]
    # OHLCV candles keyed by (symbol, timeframe)
    ohlcv: Dict[str, Dict[str, List[OHLCV]]] = field(default_factory=lambda: defaultdict(dict))
    # Timestamp of last update per symbol per metric (e.g. {"BTCUSDT": {"lsr": 1700000000}})
    last_update_ts: Dict[str, Dict[str, float]] = field(default_factory=lambda: defaultdict(dict))


class MarketDataCollector:
    def __init__(self, market_data: MarketDataEngine, exchanges: List[str], maxlen: int = 720):
        self.market_data = market_data
        self.exchanges = exchanges
        self.maxlen = maxlen
        self._initialized = False
        self._lock = asyncio.Lock()
        self.state = CollectorState(
            prices=defaultdict(lambda: deque(maxlen=maxlen)),
            bids=defaultdict(lambda: deque(maxlen=maxlen)),
            asks=defaultdict(lambda: deque(maxlen=maxlen)),
            spot=defaultdict(lambda: deque(maxlen=maxlen)),
            funding=defaultdict(lambda: deque(maxlen=maxlen)),
            basis_bps=defaultdict(lambda: deque(maxlen=maxlen)),
            open_interest=defaultdict(lambda: deque(maxlen=maxlen)),
            long_short_ratio=defaultdict(lambda: deque(maxlen=maxlen)),
            liquidation_score=defaultdict(lambda: deque(maxlen=maxlen)),
            volume_proxy=defaultdict(lambda: deque(maxlen=maxlen)),
            spread_bps=defaultdict(lambda: deque(maxlen=maxlen)),
            orderbook_imbalance=defaultdict(lambda: deque(maxlen=maxlen)),
        )
        # Caches for real exchange data
        self._oi_cache: Dict[str, float] = {}
        self._volume_cache: Dict[str, float] = {}
        self._volume_per_exchange_cache: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: volume}}
        self._lsr_cache: Dict[str, float] = {}
        self._liq_cache: Dict[str, float] = {}
        self._ob_cache: Dict[str, float] = {}
        self._ob_bid_vol_cache: Dict[str, float] = {}
        self._ob_ask_vol_cache: Dict[str, float] = {}
        # BLOCK 1.3: Enhanced orderbook metrics
        self._ob_concentration_cache: Dict[str, float] = {}
        self._ob_confidence_cache: Dict[str, float] = {}
        self._ob_depth_levels_cache: Dict[str, int] = {}
        # BLOCK 1.2: Dynamic funding interval cache
        self._funding_interval_cache: Dict[str, float] = {}  # (exchange, symbol) -> hours
        self._funding_interval_cache_ts: Dict[str, float] = {}  # timestamp of cache
        self._funding_interval_ttl: float = 3600.0  # 1 hour TTL
        self._lsr_last_update: float = 0.0
        self._liq_last_update: float = 0.0
        # BLOCK 1.4: Reduced slow data intervals
        self._slow_update_interval: float = 120.0  # 2 min for rate-limited endpoints (was 5 min)
        self._stress_slow_interval: float = 30.0  # 30 sec during stress regimes (was 1 min)
        self._urgent_update_flag: bool = False  # Urgent update trigger
        self._cache_max_age: float = 900.0  # 15 minutes TTL for cached data
        self._circuit_breakers: Dict[str, ExchangeCircuitBreaker] = {
            ex: ExchangeCircuitBreaker(exchange=ex) for ex in exchanges
        }

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return
            await self.market_data.initialize()
            await self.market_data.update_all()
            self._initialized = True

    def trigger_urgent_slow_update(self) -> None:
        """BLOCK 1.4: Trigger urgent update of slow data (bypasses interval check)."""
        self._urgent_update_flag = True
        logger.info("Urgent slow data update flag set")

    def _get_cached(self, cache: dict, symbol: str, metric_key: str, now: float) -> Optional[float]:
        """Return cached value if still within TTL, otherwise None."""
        val = cache.get(symbol)
        if val is None:
            return None
        ts = self.state.last_update_ts.get(symbol, {}).get(metric_key, 0)
        if ts and (now - ts) > self._cache_max_age:
            return None
        return val

    async def _fetch_funding_interval(self, exchange: str, symbol: str) -> Optional[float]:
        """BLOCK 1.2: Fetch funding interval from exchange API if available.

        Returns interval in hours, or None if API doesn't provide this data.
        """
        # Most exchanges don't provide a dynamic API for this, so we primarily rely on
        # the static mapping. In a real implementation, you would call exchange-specific
        # endpoints here if they exist.
        # For now, this is a placeholder for future enhancement.
        return None

    def _get_funding_interval(self, exchange: str, symbol: str, now: float) -> float:
        """BLOCK 1.2: Get funding interval with caching.

        Returns interval in hours. Uses cache with 1-hour TTL, falls back to static mapping.
        """
        cache_key = f"{exchange}:{symbol}"

        # Check cache
        cached_val = self._funding_interval_cache.get(cache_key)
        cached_ts = self._funding_interval_cache_ts.get(cache_key, 0)

        if cached_val is not None and (now - cached_ts) < self._funding_interval_ttl:
            return cached_val

        # Fallback to static mapping
        interval = FUNDING_INTERVAL_HOURS.get(exchange, 8.0)

        # Cache the result
        self._funding_interval_cache[cache_key] = interval
        self._funding_interval_cache_ts[cache_key] = now

        return interval

    async def _rate_limited_update(self, coro_func, label: str) -> None:
        """Acquire a rate-limit token for each exchange before calling update."""
        limiters = get_exchange_rate_limiters()
        for ex in self.exchanges:
            await limiters.get(ex).acquire()
        try:
            await coro_func()
        except Exception as e:
            logger.warning("update %s failed: %s", label, e)

    async def collect(self, symbols: List[str], is_stress: bool = False) -> Tuple[Dict[str, PairSnapshot], List[str]]:
        await self.initialize()
        update_labels = ["futures", "spot", "funding", "open_interest", "volume"]
        results = await asyncio.gather(
            self._rate_limited_update(self.market_data.update_futures_prices, "futures"),
            self._rate_limited_update(self.market_data.update_spot_prices, "spot"),
            self._rate_limited_update(self.market_data.update_funding_rates, "funding"),
            self._rate_limited_update(self._update_open_interest, "open_interest"),
            self._rate_limited_update(self._update_24h_volume, "volume"),
            return_exceptions=True,
        )
        for label, result in zip(update_labels, results):
            if isinstance(result, Exception):
                logger.warning("gather task '%s' failed: %s", label, result)
        # Slow-update rate-limited endpoints (LSR, liquidations) — every 5 min.
        try:
            await self.update_slow_data(symbols, is_stress=is_stress)
        except Exception as e:
            logger.warning("slow data update failed: %s", e)

        # Orderbook depth (after gather to avoid overwhelming rate limits).
        try:
            await self._update_orderbook_depth(symbols)
        except Exception as e:
            logger.warning("orderbook depth update failed: %s", e)

        snapshots: Dict[str, PairSnapshot] = {}
        warnings: List[str] = []
        now = time.time()

        for symbol in symbols:
            ex_prices: Dict[str, float] = {}
            ex_spreads: Dict[str, float] = {}
            bids: List[float] = []
            asks: List[float] = []
            spots: List[float] = []
            funding_values: List[float] = []
            funding_by_exchange: Dict[str, float] = {}

            for ex in self.exchanges:
                if not self._circuit_breakers[ex].is_available():
                    continue
                t = self.market_data.get_futures_price(ex, symbol)
                if t and t.bid > 0 and t.ask > 0:
                    mid = (t.bid + t.ask) / 2.0
                    ex_prices[ex] = mid
                    bids.append(t.bid)
                    asks.append(t.ask)
                    ex_spreads[ex] = ((t.ask - t.bid) / max(mid, 1e-9)) * 10_000
                s = self.market_data.get_spot_price(ex, symbol)
                if s and s > 0:
                    spots.append(s)
                f = self.market_data.get_funding(ex, symbol)
                if f:
                    # BLOCK 1.2: Normalize funding rate to 8-hour equivalent using dynamic interval
                    interval_h = self._get_funding_interval(ex, symbol, now)
                    normalized_rate = f.rate * (8.0 / interval_h)
                    funding_values.append(normalized_rate)
                    funding_by_exchange[ex] = normalized_rate

            if len(ex_prices) < 1:
                warnings.append(f"no_exchange_data:{symbol}")
                continue

            data_quality = "full" if len(ex_prices) >= 2 else "partial"
            if data_quality == "partial":
                single_ex = next(iter(ex_prices))
                warnings.append(f"single_exchange_data:{symbol}:{single_ex}")

            # BLOCK 1.1: Volume-weighted aggregation
            aggregation_method = "simple_average"
            if len(ex_prices) > 1:
                # Try volume-weighted aggregation
                ex_volumes = {}
                for ex in ex_prices:
                    ex_vol = self._volume_per_exchange_cache.get(ex, {}).get(symbol, 0.0)
                    ex_volumes[ex] = ex_vol
                total_volume = sum(v for v in ex_volumes.values() if v > 0)

                if total_volume > 0:
                    # Volume-weighted price
                    price = sum(ex_prices[ex] * ex_volumes[ex] for ex in ex_prices if ex_volumes[ex] > 0) / total_volume
                    # Volume-weighted bid
                    bid_by_ex = {ex: bids[i] for i, ex in enumerate(ex_prices.keys()) if i < len(bids)}
                    bid = sum(bid_by_ex[ex] * ex_volumes[ex] for ex in bid_by_ex if ex in ex_volumes and ex_volumes[ex] > 0) / total_volume if bid_by_ex else price
                    # Volume-weighted ask
                    ask_by_ex = {ex: asks[i] for i, ex in enumerate(ex_prices.keys()) if i < len(asks)}
                    ask = sum(ask_by_ex[ex] * ex_volumes[ex] for ex in ask_by_ex if ex in ex_volumes and ex_volumes[ex] > 0) / total_volume if ask_by_ex else price
                    # Volume-weighted spot
                    spot_by_ex = {ex: spots[i] for i, ex in enumerate(ex_prices.keys()) if i < len(spots)}
                    spot = sum(spot_by_ex[ex] * ex_volumes[ex] for ex in spot_by_ex if ex in ex_volumes and ex_volumes[ex] > 0) / total_volume if spot_by_ex else price
                    # Volume-weighted funding
                    funding = sum(funding_by_exchange[ex] * ex_volumes[ex] for ex in funding_by_exchange if ex in ex_volumes and ex_volumes[ex] > 0) / total_volume if total_volume > 0 else (sum(funding_values) / len(funding_values) if funding_values else 0.0)
                    aggregation_method = "volume_weighted"
                else:
                    # Fallback to simple average if no volume data
                    price = sum(ex_prices.values()) / len(ex_prices)
                    bid = sum(bids) / len(bids) if bids else price
                    ask = sum(asks) / len(asks) if asks else price
                    spot = sum(spots) / len(spots) if spots else price
                    funding = sum(funding_values) / len(funding_values) if funding_values else 0.0
            else:
                # Single exchange - no aggregation needed
                price = sum(ex_prices.values()) / len(ex_prices)
                bid = sum(bids) / len(bids) if bids else price
                ask = sum(asks) / len(asks) if asks else price
                spot = sum(spots) / len(spots) if spots else price
                funding = sum(funding_values) / len(funding_values) if funding_values else 0.0

            basis_bps = ((price - spot) / max(spot, 1e-9)) * 10_000

            prev_basis = self.state.basis_bps[symbol][-1] if self.state.basis_bps[symbol] else basis_bps
            basis_acc = basis_bps - prev_basis

            # Orderbook imbalance from bid/ask depth volumes across exchanges.
            ob_imbalance: Optional[float] = self._ob_cache.get(symbol)
            ob_bid_vol: Optional[float] = self._ob_bid_vol_cache.get(symbol)
            ob_ask_vol: Optional[float] = self._ob_ask_vol_cache.get(symbol)
            # BLOCK 1.3: Enhanced orderbook metrics
            ob_concentration: Optional[float] = self._ob_concentration_cache.get(symbol)
            ob_confidence: Optional[float] = self._ob_confidence_cache.get(symbol)
            ob_depth_levels: Optional[int] = self._ob_depth_levels_cache.get(symbol)

            # Real data from caches (populated by _update_* methods).
            # Fallback to None instead of synthetic proxies.
            # OI and volume are fetched every cycle; LSR/liq use TTL-aware cache.
            real_oi = self._oi_cache.get(symbol)
            real_volume = self._volume_cache.get(symbol)
            real_lsr = self._get_cached(self._lsr_cache, symbol, "lsr", now)
            real_liq = self._get_cached(self._liq_cache, symbol, "liquidations", now)

            # Compute staleness for slow-update metrics
            staleness: Dict[str, float] = {}
            sym_ts = self.state.last_update_ts.get(symbol, {})
            for metric_key in ("lsr", "liquidations"):
                ts_val = sym_ts.get(metric_key)
                if ts_val is not None:
                    staleness[metric_key] = now - ts_val

            # BLOCK 1.4: Slow data age tracking
            slow_data_age: Dict[str, float] = {}
            for metric_key in ("lsr", "liquidations"):
                ts_val = sym_ts.get(metric_key)
                if ts_val is not None:
                    slow_data_age[metric_key] = now - ts_val

            # BLOCK 1.2: Calculate average funding interval across exchanges
            funding_interval_avg = None
            if funding_by_exchange:
                intervals = [self._get_funding_interval(ex, symbol, now) for ex in funding_by_exchange]
                funding_interval_avg = sum(intervals) / len(intervals) if intervals else None

            snap = PairSnapshot(
                symbol=symbol,
                timestamp=now,
                price=price,
                bid=bid,
                ask=ask,
                spot_price=spot,
                funding_rate=funding,
                open_interest=real_oi,
                long_short_ratio=real_lsr,
                liquidation_cluster_score=real_liq,
                basis=basis_bps,
                basis_acceleration=basis_acc,
                volume_proxy=real_volume,
                exchange_prices=ex_prices,
                exchange_spreads_bps=ex_spreads,
                funding_by_exchange=funding_by_exchange,
                data_staleness=staleness,
                orderbook_imbalance=ob_imbalance,
                orderbook_bid_volume=ob_bid_vol,
                orderbook_ask_volume=ob_ask_vol,
                data_quality=data_quality,
                # BLOCK 1.1: Volume-weighted aggregation
                aggregation_method=aggregation_method,
                # BLOCK 1.2: Dynamic funding normalization
                funding_interval_hours=funding_interval_avg,
                # BLOCK 1.3: Orderbook imbalance improvements
                orderbook_depth_levels=ob_depth_levels,
                orderbook_concentration=ob_concentration,
                orderbook_confidence=ob_confidence,
                # BLOCK 1.4: Slow data age tracking
                slow_data_age_seconds=slow_data_age,
            )
            snapshots[symbol] = snap
            self._push_history(snap)

        return snapshots, warnings

    def _push_history(self, snap: PairSnapshot) -> None:
        s = snap.symbol
        self.state.prices[s].append(snap.price)
        self.state.bids[s].append(snap.bid)
        self.state.asks[s].append(snap.ask)
        self.state.spot[s].append(snap.spot_price)
        self.state.funding[s].append(snap.funding_rate)
        self.state.basis_bps[s].append(snap.basis)
        # Only push non-None values to history to avoid polluting rolling stats.
        if snap.open_interest is not None:
            self.state.open_interest[s].append(snap.open_interest)
        if snap.long_short_ratio is not None:
            self.state.long_short_ratio[s].append(snap.long_short_ratio)
        if snap.liquidation_cluster_score is not None:
            self.state.liquidation_score[s].append(snap.liquidation_cluster_score)
        if snap.volume_proxy is not None:
            self.state.volume_proxy[s].append(snap.volume_proxy)
        avg_spread = sum(snap.exchange_spreads_bps.values()) / max(1, len(snap.exchange_spreads_bps))
        self.state.spread_bps[s].append(avg_spread)
        if snap.orderbook_imbalance is not None:
            self.state.orderbook_imbalance[s].append(snap.orderbook_imbalance)

    async def _update_orderbook_depth(self, symbols: List[str]) -> None:
        """BLOCK 1.3: Fetch orderbook depth from all exchanges, compute weighted imbalance per symbol.

        Uses 20 levels with exponential decay weighting to reduce spoof impact.
        Computes concentration and confidence metrics.
        """
        import math

        for symbol in symbols:
            weighted_bid_vol = 0.0
            weighted_ask_vol = 0.0
            total_bid_vol = 0.0
            total_ask_vol = 0.0
            top3_bid_vol = 0.0
            top3_ask_vol = 0.0
            sources = 0
            levels_used = 0

            for ex in self.exchanges:
                cb = self._circuit_breakers[ex]
                if not cb.is_available():
                    continue
                limiters = get_exchange_rate_limiters()
                await limiters.get(ex).acquire()
                try:
                    # BLOCK 1.3: Request 20 levels instead of 10
                    depth = await self.market_data.fetch_orderbook_depth(ex, symbol, levels=20)
                    if depth and depth["bids"] and depth["asks"]:
                        # BLOCK 1.3: Apply exponential decay weighting
                        # weight_i = exp(-0.15 * i) — closer levels have more weight
                        for i, (price, qty) in enumerate(depth["bids"]):
                            weight = math.exp(-0.15 * i)
                            weighted_bid_vol += qty * weight
                            total_bid_vol += qty
                            if i < 3:
                                top3_bid_vol += qty

                        for i, (price, qty) in enumerate(depth["asks"]):
                            weight = math.exp(-0.15 * i)
                            weighted_ask_vol += qty * weight
                            total_ask_vol += qty
                            if i < 3:
                                top3_ask_vol += qty

                        sources += 1
                        levels_used = max(levels_used, min(len(depth["bids"]), len(depth["asks"])))
                    cb.record_success()
                except Exception as e:
                    cb.record_failure()
                    logger.warning("Orderbook depth %s/%s: %s", ex, symbol, e)

            if sources > 0 and (weighted_bid_vol + weighted_ask_vol) > 0:
                # BLOCK 1.3: Compute weighted imbalance
                imbalance = (weighted_bid_vol - weighted_ask_vol) / (weighted_bid_vol + weighted_ask_vol)
                self._ob_cache[symbol] = imbalance
                self._ob_bid_vol_cache[symbol] = total_bid_vol
                self._ob_ask_vol_cache[symbol] = total_ask_vol
                self._ob_depth_levels_cache[symbol] = levels_used

                # BLOCK 1.3: Compute depth concentration (top-3 levels / total)
                total_vol = total_bid_vol + total_ask_vol
                top3_vol = top3_bid_vol + top3_ask_vol
                concentration = (top3_vol / total_vol) if total_vol > 0 else 0.0
                self._ob_concentration_cache[symbol] = concentration

                # BLOCK 1.3: Compute confidence (lower if high concentration = potential spoof)
                # High concentration (>0.7) reduces confidence
                if concentration > 0.7:
                    confidence = max(0.3, 1.0 - (concentration - 0.7) / 0.3)
                else:
                    confidence = 1.0
                self._ob_confidence_cache[symbol] = confidence

    async def _update_open_interest(self) -> None:
        """Fetch real OI from all exchanges, aggregate by symbol."""
        merged: Dict[str, float] = {}
        for ex in self.exchanges:
            cb = self._circuit_breakers[ex]
            if not cb.is_available():
                continue
            try:
                oi_data = await self.market_data.fetch_open_interest(ex)
                for sym, val in oi_data.items():
                    merged[sym] = merged.get(sym, 0.0) + val
                cb.record_success()
            except Exception as e:
                cb.record_failure()
                logger.warning("OI fetch %s: %s", ex, e)
        self._oi_cache.update(merged)

    async def _update_24h_volume(self) -> None:
        """Fetch real 24h volume from all exchanges, store per-exchange and aggregated."""
        merged: Dict[str, float] = {}
        per_exchange: Dict[str, Dict[str, float]] = {}  # {exchange: {symbol: volume}}
        for ex in self.exchanges:
            cb = self._circuit_breakers[ex]
            if not cb.is_available():
                continue
            try:
                vol_data = await self.market_data.fetch_24h_volumes(ex)
                per_exchange[ex] = vol_data
                for sym, val in vol_data.items():
                    merged[sym] = merged.get(sym, 0.0) + val
                cb.record_success()
            except Exception as e:
                cb.record_failure()
                logger.warning("Volume fetch %s: %s", ex, e)
        self._volume_cache.update(merged)
        self._volume_per_exchange_cache = per_exchange

    async def update_slow_data(self, symbols: List[str], is_stress: bool = False) -> None:
        """BLOCK 1.4: Fetch rate-limited data (long/short ratio, liquidations).

        Intervals:
        - Normal: 120 sec (2 min)
        - Stress: 30 sec
        - Urgent: immediate (bypasses interval check)

        Parallelizes across symbols while respecting per-exchange rate limits.
        """
        now = time.time()
        interval = self._stress_slow_interval if is_stress else self._slow_update_interval

        # BLOCK 1.4: Check if urgent update is triggered
        if not self._urgent_update_flag and now - self._lsr_last_update < interval:
            return

        # Reset urgent flag after processing
        if self._urgent_update_flag:
            logger.info("Urgent slow data update triggered")
            self._urgent_update_flag = False

        self._lsr_last_update = now
        self._liq_last_update = now

        limiters = get_exchange_rate_limiters()

        async def _fetch_slow_for_symbol(symbol: str) -> None:
            # Long/short ratio
            lsr_values: List[float] = []
            for ex in self.exchanges:
                cb = self._circuit_breakers[ex]
                if not cb.is_available():
                    continue
                await limiters.get(ex).acquire()
                try:
                    val = await self.market_data.fetch_long_short_ratio(ex, symbol)
                    if val is not None:
                        lsr_values.append(val)
                    cb.record_success()
                except Exception as e:
                    cb.record_failure()
                    logger.warning("LSR fetch %s/%s: %s", ex, symbol, e)
            if lsr_values:
                self._lsr_cache[symbol] = sum(lsr_values) / len(lsr_values)
                self.state.last_update_ts[symbol]["lsr"] = now

            # Liquidations
            liq_total = 0.0
            for ex in self.exchanges:
                cb = self._circuit_breakers[ex]
                if not cb.is_available():
                    continue
                await limiters.get(ex).acquire()
                try:
                    val = await self.market_data.fetch_recent_liquidations(ex, symbol)
                    liq_total += val
                    cb.record_success()
                except Exception as e:
                    cb.record_failure()
                    logger.warning("Liq fetch %s/%s: %s", ex, symbol, e)
            if liq_total > 0:
                oi_val = self._oi_cache.get(symbol)
                if oi_val and oi_val > 0:
                    liq_score = liq_total / oi_val * 100.0
                else:
                    liq_score = liq_total
                self._liq_cache[symbol] = liq_score
                self.state.last_update_ts[symbol]["liquidations"] = now

        await asyncio.gather(
            *[_fetch_slow_for_symbol(s) for s in symbols],
            return_exceptions=True,
        )

    async def collect_candles(
        self, symbols: List[str], timeframes: List[str] = ("1H",), limit: int = 100
    ) -> Dict[str, Dict[str, List[OHLCV]]]:
        """Fetch OHLCV candles for given symbols/timeframes from the first available exchange."""
        await self.initialize()
        limiters = get_exchange_rate_limiters()
        result: Dict[str, Dict[str, List[OHLCV]]] = {}
        for symbol in symbols:
            result[symbol] = {}
            for tf in timeframes:
                candles: List[OHLCV] = []
                for ex in self.exchanges:
                    await limiters.get(ex).acquire()
                    try:
                        raw = await self.market_data.fetch_ohlcv(ex, symbol, tf, limit)
                        if raw:
                            candles = [
                                OHLCV(
                                    timestamp=int(c["ts"]),
                                    open=float(c["o"]),
                                    high=float(c["h"]),
                                    low=float(c["l"]),
                                    close=float(c["c"]),
                                    volume=float(c["vol"]),
                                )
                                for c in raw
                            ]
                            break  # use first exchange that returns data
                    except Exception as e:
                        logger.warning("OHLCV fetch %s/%s/%s: %s", ex, symbol, tf, e)
                if candles:
                    candles.sort(key=lambda c: c.timestamp)
                    result[symbol][tf] = candles
                    self.state.ohlcv[symbol][tf] = candles
        return result
