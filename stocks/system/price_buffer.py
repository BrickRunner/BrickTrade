from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Deque, Dict, List, Optional

from market_intelligence.indicators import (
    adx,
    atr,
    bollinger_bands,
    cumulative_volume_delta,
    ema,
    linear_slope,
    macd,
    market_structure,
    rsi,
    volume_spike,
    volume_trend,
    vwap,
)
from stocks.system.models import CandleBar

logger = logging.getLogger(__name__)


class CandleBuffer:
    """Fixed-size ring buffer of OHLCV candles for a single ticker."""

    def __init__(self, max_size: int = 200) -> None:
        self._max_size = max_size
        self._buf: Deque[CandleBar] = deque(maxlen=max_size)

    def append(self, candle: CandleBar) -> None:
        self._buf.append(candle)

    def __len__(self) -> int:
        return len(self._buf)

    def last_n(self, n: int) -> List[CandleBar]:
        items = list(self._buf)
        return items[-n:] if n < len(items) else items

    def all(self) -> List[CandleBar]:
        return list(self._buf)

    # Vectorised accessors used by indicator functions.
    def closes(self) -> List[float]:
        return [c.close for c in self._buf]

    def opens(self) -> List[float]:
        return [c.open for c in self._buf]

    def highs(self) -> List[float]:
        return [c.high for c in self._buf]

    def lows(self) -> List[float]:
        return [c.low for c in self._buf]

    def volumes(self) -> List[float]:
        return [c.volume for c in self._buf]


class CandleBufferManager:
    """Manages CandleBuffers for all tickers and computes indicators."""

    def __init__(self, tickers: List[str], max_size: int = 200) -> None:
        self._buffers: Dict[str, CandleBuffer] = {
            t: CandleBuffer(max_size) for t in tickers
        }
        self._indicator_cache: Dict[str, Dict[str, float]] = {}

    async def seed_from_rest(
        self, bcs_client: Any, class_code: str, timeframe: str = "M5"
    ) -> None:
        """Load historical candles from BCS REST to seed each buffer."""
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=3)
        for ticker, buf in self._buffers.items():
            try:
                candles = await bcs_client.get_candles(
                    ticker=ticker,
                    class_code=class_code,
                    timeframe=timeframe,
                    start_date=start.isoformat(),
                    end_date=now.isoformat(),
                )
                for c in candles:
                    buf.append(c)
                logger.info(
                    "price_buffer: seeded %s with %d candles", ticker, len(candles)
                )
            except Exception as exc:
                logger.warning("price_buffer: failed to seed %s: %s", ticker, exc)

    def on_candle_update(self, ticker: str, candle: CandleBar) -> None:
        buf = self._buffers.get(ticker)
        if buf is not None:
            buf.append(candle)
            self._indicator_cache.pop(ticker, None)

    def get_buffer(self, ticker: str) -> Optional[CandleBuffer]:
        return self._buffers.get(ticker)

    def compute_indicators(self, ticker: str) -> Dict[str, float]:
        """Compute all indicators for *ticker* and cache the result."""
        cached = self._indicator_cache.get(ticker)
        if cached is not None:
            return cached

        buf = self._buffers.get(ticker)
        if buf is None or len(buf) < 5:
            return {}

        closes = buf.closes()
        highs = buf.highs()
        lows = buf.lows()
        volumes = buf.volumes()
        opens = buf.opens()

        ind: Dict[str, float] = {}

        # EMAs
        ind["ema_9"] = ema(closes, 9)
        ind["ema_21"] = ema(closes, 21)
        ind["ema_50"] = ema(closes, 50)
        ind["ema_200"] = ema(closes, 200)

        # RSI
        ind["rsi_14"] = rsi(closes, 14)

        # MACD
        m_line, m_signal, m_hist = macd(closes, 12, 26, 9)
        ind["macd_line"] = m_line
        ind["macd_signal"] = m_signal
        ind["macd_histogram"] = m_hist

        # ATR
        atr_val = atr(highs, lows, closes, 14)
        ind["atr_14"] = atr_val if atr_val is not None else 0.0

        # ADX
        adx_val = adx(highs, lows, closes, 14)
        ind["adx_14"] = adx_val if adx_val is not None else 0.0

        # Bollinger Bands
        bb_lower, bb_mid, bb_upper, bb_width = bollinger_bands(closes, 20, 2.0)
        ind["bb_lower"] = bb_lower
        ind["bb_mid"] = bb_mid
        ind["bb_upper"] = bb_upper
        ind["bb_width"] = bb_width

        # VWAP
        vwap_val = vwap(highs, lows, closes, volumes, 20)
        ind["vwap_20"] = vwap_val if vwap_val is not None else 0.0

        # Volume spike
        ind["vol_spike"] = volume_spike(volumes, 20)

        # CVD
        ind["cvd_20"] = cumulative_volume_delta(
            volumes, closes, 20, opens=opens, highs=highs, lows=lows
        )

        # Linear slope
        ind["slope_10"] = linear_slope(closes, 10)

        # Volume trend
        vt = volume_trend(volumes, 5, 20)
        ind["vol_trend"] = vt if vt is not None else 0.0

        # Market structure
        ms = market_structure(highs, lows, 20)
        ind["market_structure"] = (
            1.0 if ms == "bullish" else (-1.0 if ms == "bearish" else 0.0)
        )

        self._indicator_cache[ticker] = ind
        return ind
