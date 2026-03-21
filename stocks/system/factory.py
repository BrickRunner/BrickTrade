from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from stocks.exchange.bcs_auth import BcsTokenManager
from stocks.exchange.bcs_rest import BcsRestClient
from stocks.exchange.bcs_ws import BcsMarketDataWs
from stocks.strategies.breakout import BreakoutStrategy
from stocks.strategies.divergence import DivergenceStrategy
from stocks.strategies.mean_reversion import MeanReversionStrategy
from stocks.strategies.rsi_reversal import RsiReversalStrategy
from stocks.strategies.trend_following import TrendFollowingStrategy
from stocks.strategies.volume_spike import VolumeSpikeStrategy
from stocks.system.config import StockTradingConfig
from stocks.system.confirmation import SemiAutoConfirmationManager
from stocks.system.engine import StockTradingEngine
from stocks.system.execution import SingleLegExecutionEngine
from stocks.system.models import StockStrategyId
from stocks.system.price_buffer import CandleBufferManager
from stocks.system.risk import StockRiskEngine
from stocks.system.schedule import MOEXSchedule
from stocks.system.state import StockSystemState
from stocks.system.strategy_runner import StockStrategyRunner

logger = logging.getLogger(__name__)

_STRATEGY_BUILDERS = {
    "mean_reversion": lambda cfg: MeanReversionStrategy(
        pairs=cfg.strategy.mr_pairs,
        zscore_entry=cfg.strategy.mr_zscore_entry,
        zscore_exit=cfg.strategy.mr_zscore_exit,
    ),
    "trend_following": lambda cfg: TrendFollowingStrategy(
        ema_fast=cfg.strategy.tf_ema_fast,
        ema_slow=cfg.strategy.tf_ema_slow,
        adx_threshold=cfg.strategy.tf_adx_threshold,
        atr_sl_mult=cfg.strategy.tf_atr_sl_mult,
    ),
    "breakout": lambda cfg: BreakoutStrategy(
        lookback=cfg.strategy.bo_lookback,
        volume_multiplier=cfg.strategy.bo_volume_multiplier,
        atr_multiplier=cfg.strategy.bo_atr_multiplier,
    ),
    "volume_spike": lambda cfg: VolumeSpikeStrategy(
        volume_threshold=cfg.strategy.vs_volume_threshold,
        take_profit_pct=cfg.strategy.vs_take_profit_pct,
        stop_loss_pct=cfg.strategy.vs_stop_loss_pct,
    ),
    "divergence": lambda cfg: DivergenceStrategy(
        rsi_period=cfg.strategy.div_rsi_period,
        lookback=cfg.strategy.div_lookback,
    ),
    "rsi_reversal": lambda cfg: RsiReversalStrategy(
        oversold=cfg.strategy.rsi_oversold,
        overbought=cfg.strategy.rsi_overbought,
        adx_max=cfg.strategy.rsi_adx_max,
    ),
}


def build_strategies(config: StockTradingConfig):
    """Build enabled strategy instances from config."""
    strategies = []
    for name in config.strategy.enabled:
        builder = _STRATEGY_BUILDERS.get(name)
        if builder:
            strategies.append(builder(config))
            logger.info("factory: enabled strategy %s", name)
        else:
            logger.warning("factory: unknown strategy %s", name)
    return strategies


def build_bcs_client(config: StockTradingConfig):
    """Build BCS REST client + token manager."""
    tm = BcsTokenManager(
        refresh_token=config.credentials.refresh_token,
        client_id=config.credentials.client_id,
    )
    client = BcsRestClient(tm)
    return client, tm


def build_bcs_ws(tm: BcsTokenManager) -> BcsMarketDataWs:
    return BcsMarketDataWs(tm)


class LiveStockMarketDataProvider:
    """Adapts BCS REST + WS + CandleBufferManager into StockMarketDataProvider."""

    def __init__(
        self,
        bcs_client: BcsRestClient,
        buffer_mgr: CandleBufferManager,
        class_code: str = "TQBR",
    ) -> None:
        self._client = bcs_client
        self._buffer_mgr = buffer_mgr
        self._class_code = class_code
        self._quote_cache: Dict[str, Any] = {}
        self._portfolio_cache: Dict[str, Any] = {}
        self._portfolio_ts: float = 0.0
        self._lot_sizes: Dict[str, int] = {}  # ticker -> lot size

    async def load_lot_sizes(self, tickers: list[str]) -> None:
        """Fetch lot sizes from BCS instruments API."""
        try:
            instruments = await self._client.get_instruments(tickers, self._class_code)
            if isinstance(instruments, list):
                for inst in instruments:
                    if isinstance(inst, dict) and inst.get("ticker"):
                        lot_size = int(inst.get("lotSize", 1) or 1)
                        self._lot_sizes[inst["ticker"]] = lot_size
                logger.info(
                    "stock_provider: loaded lot sizes for %d tickers: %s",
                    len(self._lot_sizes),
                    {k: v for k, v in self._lot_sizes.items() if v > 1},
                )
        except Exception as exc:
            logger.warning("stock_provider: failed to load lot sizes: %s", exc)

    async def get_snapshot(self, ticker: str):
        from stocks.system.models import StockQuote, StockSnapshot
        import time as _time

        # Refresh portfolio every 30s.
        now = _time.time()
        if now - self._portfolio_ts > 30:
            try:
                raw = await self._client.get_portfolio()
                # BCS returns a flat list of all portfolio items (money + positions).
                if isinstance(raw, list):
                    self._portfolio_cache = raw
                elif isinstance(raw, dict):
                    self._portfolio_cache = [raw]
                else:
                    self._portfolio_cache = []
                self._portfolio_ts = now
                # Log distinct term values on first fetch to help calibrate filter.
                terms = set()
                for it in self._portfolio_cache:
                    if isinstance(it, dict):
                        terms.add(it.get("term"))
                logger.info("stock_provider: portfolio %d raw items, terms=%s",
                            len(self._portfolio_cache), terms)
            except Exception:
                pass

        # Quote from cache (updated by WS) or fallback to last candle.
        buf = self._buffer_mgr.get_buffer(ticker)
        candles = buf.all() if buf else []

        quote = self._quote_cache.get(ticker)
        if quote is None and candles:
            last_c = candles[-1]
            quote = StockQuote(
                ticker=ticker,
                bid=last_c.close,
                ask=last_c.close,
                last=last_c.close,
                volume=last_c.volume,
            )

        if quote is None:
            quote = StockQuote(ticker=ticker, bid=0, ask=0, last=0, volume=0)

        indicators = self._buffer_mgr.compute_indicators(ticker)

        # BCS portfolio is a flat list of items duplicated per settlement term
        # (T0, T1, T2, etc.). Filter to T2 (main settlement) to avoid duplicates.
        items = self._portfolio_cache if isinstance(self._portfolio_cache, list) else [self._portfolio_cache] if self._portfolio_cache else []
        portfolio_value = 0.0
        cash = 0.0
        position_qty = 0
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("term") not in ("T2", None):
                continue
            val_rub = float(item.get("currentValueRub", 0) or 0)
            portfolio_value += val_rub
            # Cash = RUB money limits
            if item.get("type") == "moneyLimit" and item.get("currency") == "RUB":
                cash += float(item.get("quantity", 0) or 0)
            # Position quantity for this ticker
            if item.get("ticker") == ticker and item.get("type") != "moneyLimit":
                position_qty = int(item.get("quantity", 0) or 0)

        return StockSnapshot(
            ticker=ticker,
            quote=quote,
            candles=candles,
            portfolio_value=portfolio_value,
            cash_available=cash,
            current_position_qty=position_qty,
            indicators=indicators,
            lot_size=self._lot_sizes.get(ticker, 1),
        )

    def update_quote(self, quote) -> None:
        """Called by WS callback to update quote cache."""
        self._quote_cache[quote.ticker] = quote

    async def health(self) -> Dict[str, float]:
        return {"portfolio_age": __import__("time").time() - self._portfolio_ts}


class LiveStockExecutionVenue:
    """Adapts BcsRestClient to StockExecutionVenue protocol."""

    def __init__(self, bcs_client: BcsRestClient, class_code: str = "TQBR") -> None:
        self._client = bcs_client
        self._class_code = class_code

    async def place_order(
        self, ticker: str, side: str, quantity_lots: int,
        order_type: str, limit_price: float = 0.0,
    ) -> Dict[str, Any]:
        side_int = 1 if side == "buy" else 2
        otype_int = 1 if order_type == "market" else 2
        return await self._client.place_order(
            ticker, self._class_code, side_int, otype_int, quantity_lots, limit_price
        )

    async def cancel_order(self, order_id: str) -> None:
        await self._client.cancel_order(order_id)

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self._client.get_order(order_id)

    async def get_portfolio(self) -> Dict[str, Any]:
        return await self._client.get_portfolio()

    async def get_positions(self) -> Dict[str, int]:
        portfolio = await self._client.get_portfolio()
        result: Dict[str, int] = {}
        for pos in portfolio.get("positions", []):
            result[pos.get("ticker", "")] = int(pos.get("quantity", 0))
        return result


def build_stock_engine(
    config: StockTradingConfig,
    confirmation: Optional[SemiAutoConfirmationManager] = None,
):
    """Build the complete stock trading engine from config."""
    bcs_client, tm = build_bcs_client(config)
    ws = build_bcs_ws(tm)

    buffer_mgr = CandleBufferManager(config.tickers, config.strategy.candle_history_size)
    provider = LiveStockMarketDataProvider(bcs_client, buffer_mgr, config.class_code)
    venue = LiveStockExecutionVenue(bcs_client, config.class_code)

    state = StockSystemState(config.starting_equity)
    risk = StockRiskEngine(config.risk, state)
    execution = SingleLegExecutionEngine(config.execution, config.risk, venue, state)

    strategies_list = build_strategies(config)
    runner = StockStrategyRunner(strategies=strategies_list)

    engine = StockTradingEngine(
        config=config,
        provider=provider,
        risk=risk,
        execution=execution,
        strategies=runner,
        state=state,
        schedule=MOEXSchedule(),
        confirmation=confirmation,
    )

    return engine, bcs_client, ws, tm, buffer_mgr, provider, state
