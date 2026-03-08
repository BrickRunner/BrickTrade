from __future__ import annotations

import json
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Dict, List, Optional

from market_intelligence.indicators import adx, atr, bollinger_bands, cumulative_volume_delta, ema, linear_slope, macd, market_structure, rsi, volume_spike, volume_trend, vwap
from market_intelligence.models import FeatureVector, OHLCV, PairSnapshot
from market_intelligence.statistics import RollingStats, rolling_returns


def _std_window(values: List[float], window: int) -> float:
    """Population standard deviation over the last *window* values."""
    if len(values) < 2:
        return 0.0
    w = values[-window:]
    m = sum(w) / len(w)
    return (sum((x - m) ** 2 for x in w) / len(w)) ** 0.5


class FeatureEngine:
    def __init__(self, zscore_window: int, local_window: int = 30):
        self._stats: Dict[str, Dict[str, RollingStats]] = defaultdict(dict)
        self._window = zscore_window
        self._local_window = local_window
        # BLOCK 2.1: Dynamic ATR proxy compensation
        self._atr_ratio_history: Dict[str, deque] = defaultdict(lambda: deque(maxlen=50))

    def compute(
        self,
        snapshots: Dict[str, PairSnapshot],
        histories: Dict[str, Dict[str, List[float]]],
        candles: Optional[Dict[str, Dict[str, List[OHLCV]]]] = None,
    ) -> Dict[str, FeatureVector]:
        features: Dict[str, FeatureVector] = {}

        for symbol, snap in snapshots.items():
            h = histories.get(symbol, {})
            closes = h.get("price", [])
            highs = h.get("ask", closes)
            lows = h.get("bid", closes)
            vols = h.get("volume", [])
            basis_hist = h.get("basis", [])
            funding_hist = h.get("funding", [])
            oi_hist = h.get("oi", [])
            spread_hist = h.get("spread", [])

            # Override with OHLCV candle data when available for better accuracy.
            ohlcv_1h = (candles or {}).get(symbol, {}).get("1H", [])
            if ohlcv_1h:
                ohlcv_closes = [c.close for c in ohlcv_1h]
                ohlcv_highs = [c.high for c in ohlcv_1h]
                ohlcv_lows = [c.low for c in ohlcv_1h]
                ohlcv_vols = [c.volume for c in ohlcv_1h]
            else:
                ohlcv_closes = []
                ohlcv_highs = []
                ohlcv_lows = []
                ohlcv_vols = []

            local_slice = slice(-self._local_window, None)
            closes_local = closes[local_slice]
            highs_local = highs[local_slice]
            lows_local = lows[local_slice]

            ema50 = ema(closes, 50)
            ema200 = ema(closes, 200)
            ema_cross = ema50 - ema200

            rsi_val = rsi(closes, 14)
            rsi_local = rsi(closes_local, 14)
            macd_line, macd_signal, macd_hist = macd(closes)
            macd_local, macd_signal_local, macd_hist_local = macd(closes_local)

            # BLOCK 2.1: Dynamic ATR proxy compensation
            atr_period = 14
            atr_calibration = "static"
            atr_proxy_ratio = 1.35  # default fallback
            atr_val = None
            adx_val = None

            # Compute both OHLCV and tick-proxy ATR when both are available
            if ohlcv_highs and len(ohlcv_highs) >= atr_period * 2:
                candle_atr = atr(ohlcv_highs, ohlcv_lows, ohlcv_closes, atr_period)
                candle_adx = adx(ohlcv_highs, ohlcv_lows, ohlcv_closes, atr_period)

                if len(closes) >= atr_period * 2:
                    # We also have tick data - compute tick ATR to calibrate ratio
                    tick_atr = atr(highs, lows, closes, atr_period)

                    if tick_atr is not None and tick_atr > 0 and candle_atr is not None and candle_atr > 0:
                        # Compute actual ratio
                        actual_ratio = candle_atr / tick_atr
                        self._atr_ratio_history[symbol].append(actual_ratio)

                        # Use rolling average ratio as dynamic multiplier
                        if len(self._atr_ratio_history[symbol]) >= 10:
                            atr_proxy_ratio = sum(self._atr_ratio_history[symbol]) / len(self._atr_ratio_history[symbol])
                            atr_calibration = "dynamic"

                # Use OHLCV ATR as primary
                atr_val = candle_atr
                adx_val = candle_adx
                atr_source = "ohlcv"
                atr_proxy_penalty_applied = 0.0
            else:
                # Fallback to tick-proxy ATR
                if len(closes) >= atr_period * 2:
                    tick_atr = atr(highs, lows, closes, atr_period)
                    tick_adx = adx(highs, lows, closes, atr_period)

                    # Apply dynamic compensation if available
                    if tick_atr is not None:
                        atr_val = tick_atr * atr_proxy_ratio
                        atr_calibration = "dynamic" if len(self._atr_ratio_history[symbol]) >= 10 else "static"
                    else:
                        atr_val = None

                    adx_val = tick_adx
                    atr_source = "tick_proxy"
                    atr_proxy_penalty_applied = 1.0
                else:
                    # Insufficient data
                    atr_val = None
                    adx_val = None
                    atr_source = "tick_proxy"
                    atr_proxy_penalty_applied = 1.0
            adx_local = adx(highs_local, lows_local, closes_local, 14)
            atr_local = atr(highs_local, lows_local, closes_local, 14)
            _, _, _, bb_width = bollinger_bands(closes, 20, 2.0)
            _, _, _, bb_width_local = bollinger_bands(closes_local, 20, 2.0)
            vol_data = ohlcv_vols if ohlcv_vols else vols
            vol_spike = volume_spike(vol_data, 20)
            cvd_closes = ohlcv_closes if ohlcv_closes else closes
            cvd_vols = ohlcv_vols if ohlcv_vols else vols
            if ohlcv_closes:
                cvd_val = cumulative_volume_delta(
                    cvd_vols, cvd_closes, 20,
                    opens=[c.open for c in ohlcv_1h],
                    highs=ohlcv_highs, lows=ohlcv_lows,
                )
            else:
                cvd_val = cumulative_volume_delta(cvd_vols, cvd_closes, 20)

            # VWAP (prefer OHLCV candle data)
            vwap_h = ohlcv_highs if ohlcv_highs else highs
            vwap_l = ohlcv_lows if ohlcv_lows else lows
            vwap_c = ohlcv_closes if ohlcv_closes else closes
            vwap_v = ohlcv_vols if ohlcv_vols else vols
            vwap_val = vwap(vwap_h, vwap_l, vwap_c, vwap_v, 20)
            price_vs_vwap_pct = ((snap.price - vwap_val) / vwap_val * 100.0) if vwap_val else None

            # Volume trend (short/long EMA ratio)
            vol_trend_val = volume_trend(vol_data, 5, 20)

            # Market structure detection
            struct_highs = ohlcv_highs if ohlcv_highs else highs
            struct_lows = ohlcv_lows if ohlcv_lows else lows
            mkt_struct = market_structure(struct_highs, struct_lows, 20)
            if mkt_struct == "bullish":
                mkt_struct_code: Optional[float] = 1.0
            elif mkt_struct == "bearish":
                mkt_struct_code = -1.0
            elif mkt_struct == "transition":
                mkt_struct_code = 0.0
            else:
                mkt_struct_code = None

            returns = rolling_returns(closes)
            local_returns = rolling_returns(closes_local)
            rolling_vol = _std_window(returns, 50)
            rolling_vol_local = _std_window(local_returns, 20)

            oi_delta = oi_hist[-1] - oi_hist[-2] if len(oi_hist) >= 2 else None
            oi_prev = oi_hist[-2] if len(oi_hist) >= 2 else None
            oi_delta_pct = ((oi_delta / max(abs(oi_prev), 1e-9)) * 100.0) if (oi_delta is not None and oi_prev is not None) else None

            funding_delta = funding_hist[-1] - funding_hist[-2] if len(funding_hist) >= 2 else 0.0
            basis_acc = linear_slope(basis_hist, window=10)
            spread_acc = linear_slope(spread_hist, window=10)
            basis_acc_z = basis_acc / max(_std_window(basis_hist, 50), 1e-9) if len(basis_hist) >= 2 else 0.0
            spread_acc_z = spread_acc / max(_std_window(spread_hist, 50), 1e-9) if len(spread_hist) >= 2 else 0.0
            funding_slope = linear_slope(funding_hist, window=10)

            atr_pct = ((atr_val or 0.0) / max(abs(snap.price), 1e-9)) * 100.0
            atr_local_pct = ((atr_local or 0.0) / max(abs(snap.price), 1e-9)) * 100.0
            funding_pct = snap.funding_rate * 100.0
            bb_width_pct = bb_width * 100.0
            bb_width_local_pct = bb_width_local * 100.0

            indicator_availability = {
                "adx_available": 1.0 if adx_val is not None else 0.0,
                "atr_available": 1.0 if atr_val is not None else 0.0,
                "adx_local_available": 1.0 if adx_local is not None else 0.0,
                "atr_local_available": 1.0 if atr_local is not None else 0.0,
            }

            values: Dict[str, Optional[float]] = {
                "price": snap.price,
                "ema50": ema50,
                "ema200": ema200,
                "ema_cross": ema_cross,
                "adx": adx_val,
                "adx_local": adx_local,
                "rsi": rsi_val,
                "rsi_local": rsi_local,
                "macd": macd_line,
                "macd_signal": macd_signal,
                "macd_hist": macd_hist,
                "macd_local": macd_local,
                "macd_signal_local": macd_signal_local,
                "macd_hist_local": macd_hist_local,
                "atr": atr_val,
                "atr_local": atr_local,
                "atr_pct": atr_pct,
                "atr_local_pct": atr_local_pct,
                # BLOCK 2.1: ATR proxy calibration info
                "atr_calibration_code": 0.0 if atr_calibration == "static" else 1.0,
                "atr_proxy_ratio": atr_proxy_ratio,
                "bb_width": bb_width,
                "bb_width_local": bb_width_local,
                "bb_width_pct": bb_width_pct,
                "bb_width_local_pct": bb_width_local_pct,
                "volume_spike": vol_spike,
                "cvd": cvd_val,
                "vwap": vwap_val,
                "price_vs_vwap_pct": price_vs_vwap_pct,
                "volume_trend": vol_trend_val,
                "funding_rate": snap.funding_rate,
                "funding_pct": funding_pct,
                "funding_delta": funding_delta,
                "open_interest": snap.open_interest,
                "oi_delta": oi_delta,
                "oi_delta_pct": oi_delta_pct,
                "basis_bps": snap.basis,
                "basis_acceleration": basis_acc,
                "basis_acceleration_z": basis_acc_z,
                "long_short_ratio": snap.long_short_ratio,
                "liquidation_cluster": snap.liquidation_cluster_score,
                "orderbook_imbalance": snap.orderbook_imbalance,
                "market_structure_code": mkt_struct_code,
                "data_quality_code": 0.0 if getattr(snap, 'data_quality', 'full') == "full" else 1.0,
                "rolling_volatility": rolling_vol,
                "rolling_volatility_local": rolling_vol_local,
                "spread_bps": (snap.ask - snap.bid) / max(snap.price, 1e-9) * 10_000,
                "spread_acceleration": spread_acc,
                "spread_acceleration_z": spread_acc_z,
                "atr_source_code": 0.0 if atr_source == "ohlcv" else 1.0,
                "funding_slope": funding_slope,
                "atr_proxy_penalty_applied": atr_proxy_penalty_applied,
                **indicator_availability,
            }

            normalized: Dict[str, Optional[float]] = {}
            for key, val in values.items():
                if val is None:
                    normalized[key] = None
                    continue
                stats = self._stats[symbol].setdefault(key, RollingStats(self._window))
                stats.push(val)
                normalized[key] = stats.zscore(val)

            # Multi-timeframe features from higher-timeframe candles.
            for mtf_tf in ("4H", "1D"):
                mtf_candles = (candles or {}).get(symbol, {}).get(mtf_tf, [])
                if len(mtf_candles) >= 30:
                    mtf_closes = [c.close for c in mtf_candles]
                    mtf_highs = [c.high for c in mtf_candles]
                    mtf_lows = [c.low for c in mtf_candles]
                    mtf_ema50 = ema(mtf_closes, 50) if len(mtf_closes) >= 50 else ema(mtf_closes, len(mtf_closes))
                    mtf_ema200 = ema(mtf_closes, 200) if len(mtf_closes) >= 200 else ema(mtf_closes, len(mtf_closes))
                    mtf_ema_cross = mtf_ema50 - mtf_ema200
                    mtf_adx_val = adx(mtf_highs, mtf_lows, mtf_closes, 14)
                    mtf_rsi_val = rsi(mtf_closes, 14)
                    values[f"ema_cross_{mtf_tf}"] = mtf_ema_cross
                    values[f"adx_{mtf_tf}"] = mtf_adx_val
                    values[f"rsi_{mtf_tf}"] = mtf_rsi_val
                    for k in (f"ema_cross_{mtf_tf}", f"adx_{mtf_tf}", f"rsi_{mtf_tf}"):
                        st_key = self._stats[symbol].setdefault(k, RollingStats(self._window))
                        if values[k] is not None:
                            st_key.push(values[k])
                            normalized[k] = st_key.zscore(values[k])
                        else:
                            normalized[k] = None

            # Volatility regime using ATR percentile rank in history.
            atr_stats = self._stats[symbol].setdefault("atr_pct", RollingStats(self._window))
            if atr_pct is not None:
                atr_stats.push(atr_pct)
            atr_percentile = atr_stats.percentile_rank(atr_pct if atr_pct is not None else 0.0)
            if atr_percentile < 0.30:
                volatility_regime = "low"
            elif atr_percentile <= 0.70:
                volatility_regime = "medium"
            else:
                volatility_regime = "high"
            values["atr_percentile"] = atr_percentile
            values["volatility_regime_code"] = 0.0 if volatility_regime == "low" else 1.0 if volatility_regime == "medium" else 2.0
            normalized["atr_percentile"] = atr_stats.zscore(atr_percentile)
            normalized["volatility_regime_code"] = 0.0

            # Local momentum/volatility state used in reporting.
            values["local_volatility_expansion"] = 1.0 if (rolling_vol_local > rolling_vol) else -1.0
            values["local_momentum_bias"] = 1.0 if (macd_hist_local > 0 and rsi_local >= 50) else -1.0 if (macd_hist_local < 0 and rsi_local < 50) else 0.0
            normalized["local_volatility_expansion"] = values["local_volatility_expansion"]
            normalized["local_momentum_bias"] = values["local_momentum_bias"]

            features[symbol] = FeatureVector(symbol=symbol, timestamp=snap.timestamp, values=values, normalized=normalized)

        return features

    # BLOCK 6.1: State persistence
    def save_state(self, path: Path) -> None:
        """Save feature engine state (rolling stats and ATR ratios) to JSON."""
        state: Dict[str, Any] = {
            "_window": self._window,
            "_local_window": self._local_window,
            "_stats": {
                symbol: {
                    key: {"window": stats.window, "values": stats.values}
                    for key, stats in stats_dict.items()
                }
                for symbol, stats_dict in self._stats.items()
            },
            "_atr_ratio_history": {k: list(v) for k, v in self._atr_ratio_history.items()},
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def load_state(self, path: Path) -> bool:
        """Load feature engine state from JSON. Returns True if successful."""
        try:
            if not path.exists():
                return False

            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            self._window = state.get("_window", self._window)
            self._local_window = state.get("_local_window", self._local_window)

            # Restore rolling stats
            self._stats = defaultdict(dict)
            for symbol, stats_dict in state.get("_stats", {}).items():
                for key, stats_data in stats_dict.items():
                    rs = RollingStats(window=stats_data["window"])
                    for val in stats_data["values"]:
                        rs.push(val)
                    self._stats[symbol][key] = rs

            # Restore ATR ratio history
            self._atr_ratio_history = defaultdict(lambda: deque(maxlen=50))
            for k, vals in state.get("_atr_ratio_history", {}).items():
                self._atr_ratio_history[k] = deque(vals, maxlen=50)

            return True
        except Exception:
            return False
