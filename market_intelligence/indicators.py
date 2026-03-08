from __future__ import annotations

from typing import List, Optional, Tuple


def ema(values: List[float], period: int) -> float:
    if not values:
        return 0.0
    period = max(1, period)
    alpha = 2.0 / (period + 1)
    e = float(values[0])
    for v in values[1:]:
        e = alpha * float(v) + (1 - alpha) * e
    return e


def rsi(values: List[float], period: int = 14) -> float:
    if len(values) < period + 1:
        return 50.0
    # Initial SMA seed
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains += d
        else:
            losses -= d
    avg_gain = gains / period
    avg_loss = losses / period
    # Wilder smoothing for remaining values
    for i in range(period + 1, len(values)):
        d = values[i] - values[i - 1]
        if d >= 0:
            avg_gain = (avg_gain * (period - 1) + d) / period
            avg_loss = (avg_loss * (period - 1)) / period
        else:
            avg_gain = (avg_gain * (period - 1)) / period
            avg_loss = (avg_loss * (period - 1) - d) / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def macd(values: List[float], fast: int = 12, slow: int = 26, signal: int = 9) -> Tuple[float, float, float]:
    if len(values) < max(fast, slow):
        return 0.0, 0.0, 0.0
    # Incremental EMA: O(n) instead of O(n^2).
    alpha_fast = 2.0 / (max(1, fast) + 1)
    alpha_slow = 2.0 / (max(1, slow) + 1)
    ema_f = float(values[0])
    ema_s = float(values[0])
    macd_series: List[float] = []
    for i, v in enumerate(values):
        if i == 0:
            continue
        ema_f = alpha_fast * v + (1.0 - alpha_fast) * ema_f
        ema_s = alpha_slow * v + (1.0 - alpha_slow) * ema_s
        if i >= slow - 1:
            macd_series.append(ema_f - ema_s)
    if not macd_series:
        return 0.0, 0.0, 0.0
    macd_line = macd_series[-1]
    alpha_sig = 2.0 / (max(1, signal) + 1)
    sig = macd_series[0]
    for m in macd_series[1:]:
        sig = alpha_sig * m + (1.0 - alpha_sig) * sig
    return macd_line, sig, macd_line - sig


def atr(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < 2:
        return None
    trs: List[float] = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    if len(trs) < period:
        return sum(trs) / len(trs) if trs else None
    # Wilder smoothing: seed with SMA of first `period` values, then EMA.
    atr_val = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr_val = (atr_val * (period - 1) + trs[i]) / period
    return atr_val


def adx(highs: List[float], lows: List[float], closes: List[float], period: int = 14) -> Optional[float]:
    n = min(len(highs), len(lows), len(closes))
    if n < period + 1:
        return None

    plus_dm: List[float] = []
    minus_dm: List[float] = []
    tr_list: List[float] = []

    for i in range(1, n):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        tr_list.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))

    if len(tr_list) < period:
        return None

    # Wilder smoothing
    tr14 = sum(tr_list[:period])
    plus14 = sum(plus_dm[:period])
    minus14 = sum(minus_dm[:period])
    if tr14 <= 1e-12:
        return None

    dx_values: List[float] = []
    for i in range(period, len(tr_list)):
        tr14 = tr14 - (tr14 / period) + tr_list[i]
        plus14 = plus14 - (plus14 / period) + plus_dm[i]
        minus14 = minus14 - (minus14 / period) + minus_dm[i]
        if tr14 <= 1e-12:
            continue
        plus_di = 100.0 * (plus14 / tr14)
        minus_di = 100.0 * (minus14 / tr14)
        denom = plus_di + minus_di
        if denom <= 1e-12:
            continue
        dx_values.append(100.0 * abs(plus_di - minus_di) / denom)

    if len(dx_values) < period:
        return None
    return sum(dx_values[-period:]) / period


def bollinger_bands(values: List[float], period: int = 20, k: float = 2.0) -> Tuple[float, float, float, float]:
    if len(values) < period:
        avg = sum(values) / len(values) if values else 0.0
        return avg, avg, avg, 0.0
    window = values[-period:]
    mid = sum(window) / period
    var = sum((x - mid) ** 2 for x in window) / period
    std = var ** 0.5
    upper = mid + k * std
    lower = mid - k * std
    width = (upper - lower) / max(abs(mid), 1e-9)
    return lower, mid, upper, width


def cumulative_volume_delta(
    volumes: List[float], closes: List[float], window: int = 20,
    opens: List[float] | None = None, highs: List[float] | None = None, lows: List[float] | None = None,
) -> float:
    """Normalized CVD over last *window* bars.

    When OHLCV data is available, uses the candle body ratio method:
    buy_pct = (close - low) / (high - low) for better accuracy.
    Falls back to close-to-close comparison otherwise.
    """
    n = min(len(volumes), len(closes))
    if n < 2 or window < 1:
        return 0.0

    use_ohlcv = (
        highs is not None and lows is not None
        and len(highs) >= n and len(lows) >= n
    )

    start = max(1, n - window)
    buy = 0.0
    sell = 0.0
    for i in range(start, n):
        vol = volumes[i]
        if use_ohlcv:
            bar_range = highs[i] - lows[i]
            if bar_range > 0:
                buy_pct = (closes[i] - lows[i]) / bar_range
            else:
                buy_pct = 0.5
            buy += vol * buy_pct
            sell += vol * (1.0 - buy_pct)
        else:
            if closes[i] >= closes[i - 1]:
                buy += vol
            else:
                sell += vol
    total = buy + sell
    if total <= 1e-12:
        return 0.0
    return (buy - sell) / total


def vwap(
    highs: List[float], lows: List[float], closes: List[float],
    volumes: List[float], period: int = 20,
) -> Optional[float]:
    """Volume-weighted average price over the last *period* bars."""
    n = min(len(highs), len(lows), len(closes), len(volumes))
    if n < period:
        return None
    tp_vol_sum = 0.0
    vol_sum = 0.0
    for i in range(n - period, n):
        tp = (highs[i] + lows[i] + closes[i]) / 3.0
        tp_vol_sum += tp * volumes[i]
        vol_sum += volumes[i]
    if vol_sum <= 1e-12:
        return None
    return tp_vol_sum / vol_sum


def volume_trend(
    volumes: List[float], short_window: int = 5, long_window: int = 20,
) -> Optional[float]:
    """Ratio of short EMA to long EMA of volumes. >1.0 means rising volume."""
    if len(volumes) < long_window:
        return None
    short_ema = ema(volumes, short_window)
    long_ema = ema(volumes, long_window)
    if long_ema <= 1e-12:
        return None
    return short_ema / long_ema


def market_structure(highs: List[float], lows: List[float], lookback: int = 20) -> Optional[str]:
    """Detect market structure from swing points.

    Returns: 'bullish' (HH+HL), 'bearish' (LH+LL), 'transition', or None if insufficient data.
    """
    n = min(len(highs), len(lows))
    if n < 5:
        return None
    start = max(0, n - lookback)
    h = highs[start:n]
    l = lows[start:n]

    # 3-bar pivot detection
    swing_highs: List[float] = []
    swing_lows: List[float] = []
    for i in range(1, len(h) - 1):
        if h[i] >= h[i - 1] and h[i] >= h[i + 1]:
            swing_highs.append(h[i])
        if l[i] <= l[i - 1] and l[i] <= l[i + 1]:
            swing_lows.append(l[i])

    if len(swing_highs) < 2 or len(swing_lows) < 2:
        return None

    hh = swing_highs[-1] > swing_highs[-2]
    hl = swing_lows[-1] > swing_lows[-2]
    lh = swing_highs[-1] < swing_highs[-2]
    ll = swing_lows[-1] < swing_lows[-2]

    if hh and hl:
        return "bullish"
    if lh and ll:
        return "bearish"
    return "transition"


def linear_slope(values: List[float], window: int = 10) -> float:
    """Least-squares linear regression slope over last `window` values.
    Normalized by mean absolute value for cross-asset comparability.
    Returns 0.0 if insufficient data."""
    if len(values) < max(3, window):
        return 0.0
    w = values[-window:]
    n = len(w)
    x_mean = (n - 1) / 2.0
    y_mean = sum(w) / n
    num = sum((i - x_mean) * (w[i] - y_mean) for i in range(n))
    den = sum((i - x_mean) ** 2 for i in range(n))
    if den <= 1e-12:
        return 0.0
    slope = num / den
    abs_mean = sum(abs(v) for v in w) / n
    if abs_mean <= 1e-12:
        return 0.0
    return slope / abs_mean


def volume_spike(volume: List[float], period: int = 20) -> float:
    if len(volume) < period + 1:
        return 0.0
    baseline = sum(volume[-period - 1:-1]) / period
    if baseline <= 1e-12:
        return 0.0
    return volume[-1] / baseline


# ===== PHASE 4: FUNDING RATE MEAN-REVERSION MODEL =====

def funding_zscore_adaptive(
    funding_history: List[float],
    short_window: int = 12,
    long_window: int = 72,
) -> dict:
    """Analyze funding rate with mean-reversion model.

    Instead of simple linear normalization, models funding as mean-reverting process:
    - Computes short-term vs long-term mean for regime detection
    - Identifies extreme tails (>2 sigma) as crowding signals
    - Tracks funding acceleration (is crowding getting worse?)

    Returns dict with:
        funding_deviation: how far from long-term mean (in sigma)
        funding_regime: 1.0 (crowded long), -1.0 (crowded short), 0.0 (neutral)
        funding_mean_reversion_signal: expected reversion strength [0, 1]
        funding_acceleration: rate of change of deviation
        funding_extreme: 1.0 if >2sigma, 0.0 otherwise
    """
    result = {
        "funding_deviation": 0.0,
        "funding_regime": 0.0,
        "funding_mean_reversion_signal": 0.0,
        "funding_acceleration": 0.0,
        "funding_extreme": 0.0,
    }

    if len(funding_history) < max(short_window, 6):
        return result

    # Long-term statistics
    long_data = funding_history[-long_window:] if len(funding_history) >= long_window else funding_history
    long_mean = sum(long_data) / len(long_data)
    long_var = sum((x - long_mean) ** 2 for x in long_data) / max(len(long_data) - 1, 1)
    long_std = long_var ** 0.5

    if long_std < 1e-12:
        return result

    # Short-term mean
    short_data = funding_history[-short_window:]
    short_mean = sum(short_data) / len(short_data)

    # Deviation from long-term mean in sigma
    deviation = (short_mean - long_mean) / long_std
    result["funding_deviation"] = deviation

    # Funding regime
    if deviation > 1.0:
        result["funding_regime"] = min(1.0, deviation / 3.0)
    elif deviation < -1.0:
        result["funding_regime"] = max(-1.0, deviation / 3.0)

    # Mean reversion signal: stronger when further from mean
    # Based on Ornstein-Uhlenbeck intuition: reversion force proportional to displacement
    abs_dev = abs(deviation)
    if abs_dev > 1.0:
        result["funding_mean_reversion_signal"] = min(1.0, (abs_dev - 1.0) / 2.0)

    # Extreme flag
    if abs_dev >= 2.0:
        result["funding_extreme"] = 1.0

    # Acceleration: is funding getting more extreme?
    if len(funding_history) >= short_window * 2:
        prev_short = funding_history[-(short_window * 2):-short_window]
        prev_mean = sum(prev_short) / len(prev_short)
        prev_dev = (prev_mean - long_mean) / long_std
        result["funding_acceleration"] = deviation - prev_dev

    return result


# ===== PHASE 5: LIQUIDATION CASCADE MODEL =====

def liquidation_cascade_risk(
    liquidation_scores: List[float],
    price_changes: List[float],
    oi_deltas: List[float],
    window: int = 10,
) -> dict:
    """Nonlinear liquidation cascade risk model.

    Models the positive feedback loop:
    liquidation → price drop → more liquidations → flash crash

    Key insight: cascade risk is NONLINEAR — it accelerates past thresholds.

    Returns:
        cascade_risk: 0-1 probability of cascade
        cascade_stage: 0 (none), 1 (early), 2 (developing), 3 (active)
        cascade_direction: -1 (long squeeze), 1 (short squeeze), 0 (none)
    """
    result = {"cascade_risk": 0.0, "cascade_stage": 0.0, "cascade_direction": 0.0}

    if len(liquidation_scores) < 3:
        return result

    w = min(window, len(liquidation_scores))
    recent_liq = liquidation_scores[-w:]
    recent_price = price_changes[-w:] if len(price_changes) >= w else []
    recent_oi = oi_deltas[-w:] if len(oi_deltas) >= w else []

    # Average liquidation intensity
    avg_liq = sum(recent_liq) / len(recent_liq) if recent_liq else 0.0
    max_liq = max(recent_liq) if recent_liq else 0.0

    # Liquidation acceleration: are liquidations increasing?
    if len(recent_liq) >= 4:
        first_half = sum(recent_liq[:len(recent_liq)//2]) / max(len(recent_liq)//2, 1)
        second_half = sum(recent_liq[len(recent_liq)//2:]) / max(len(recent_liq) - len(recent_liq)//2, 1)
        liq_acceleration = second_half - first_half
    else:
        liq_acceleration = 0.0

    # Price-liquidation correlation (feedback loop indicator)
    price_liq_feedback = 0.0
    if recent_price and len(recent_price) == len(recent_liq):
        # Negative price change + high liquidations = cascade
        for i in range(len(recent_price)):
            if recent_price[i] < 0 and recent_liq[i] > avg_liq:
                price_liq_feedback += abs(recent_price[i]) * recent_liq[i]

    # OI contraction during liquidation = forced closure
    oi_contraction = 0.0
    if recent_oi:
        neg_oi = [d for d in recent_oi if d < 0]
        oi_contraction = abs(sum(neg_oi)) / max(len(recent_oi), 1)

    # Nonlinear cascade risk score
    # Key: risk grows QUADRATICALLY past thresholds
    base_risk = 0.0
    if max_liq > 0.5:
        base_risk += 0.3 * min(1.0, max_liq)
    if liq_acceleration > 0.2:
        base_risk += 0.3 * min(1.0, liq_acceleration ** 1.5)  # nonlinear!
    if price_liq_feedback > 0.1:
        base_risk += 0.2 * min(1.0, price_liq_feedback)
    if oi_contraction > 0:
        base_risk += 0.2 * min(1.0, oi_contraction)

    cascade_risk = min(1.0, base_risk)
    result["cascade_risk"] = cascade_risk

    # Cascade stage classification
    if cascade_risk >= 0.7:
        result["cascade_stage"] = 3.0  # Active cascade
    elif cascade_risk >= 0.4:
        result["cascade_stage"] = 2.0  # Developing
    elif cascade_risk >= 0.15:
        result["cascade_stage"] = 1.0  # Early warning
    else:
        result["cascade_stage"] = 0.0

    # Direction: negative price = long liquidation, positive = short squeeze
    if recent_price:
        avg_price_change = sum(recent_price) / len(recent_price)
        if avg_price_change < -0.001 and cascade_risk > 0.15:
            result["cascade_direction"] = -1.0  # long squeeze
        elif avg_price_change > 0.001 and cascade_risk > 0.15:
            result["cascade_direction"] = 1.0   # short squeeze

    return result


# ===== PHASE 6: MARKET MICROSTRUCTURE =====

def spread_dynamics(
    spreads_bps: List[float],
    window: int = 20,
) -> dict:
    """Analyze bid-ask spread dynamics as a leading indicator.

    Widening spreads precede volatility; tightening precedes calm.
    Sudden spread widening = liquidity withdrawal = danger signal.

    Returns:
        spread_regime_code: -1=tight, 0=normal, 1=wide, 2=extreme
        spread_expansion_rate: rate of widening (positive = widening)
        spread_percentile: current spread vs historical
        liquidity_withdrawal: 0-1 score, 1 = sudden extreme widening
    """
    result = {
        "spread_regime_code": 0.0,
        "spread_expansion_rate": 0.0,
        "spread_percentile": 0.5,
        "liquidity_withdrawal": 0.0,
    }

    if len(spreads_bps) < max(5, window // 2):
        return result

    data = spreads_bps[-window:]
    n = len(data)
    mean_sp = sum(data) / n
    var_sp = sum((x - mean_sp) ** 2 for x in data) / max(n - 1, 1)
    std_sp = var_sp ** 0.5

    current = data[-1]

    if std_sp < 1e-12:
        return result

    z = (current - mean_sp) / std_sp

    # Spread regime
    if z > 2.0:
        result["spread_regime_code"] = 2.0
    elif z > 1.0:
        result["spread_regime_code"] = 1.0
    elif z < -1.0:
        result["spread_regime_code"] = -1.0
    else:
        result["spread_regime_code"] = 0.0

    # Expansion rate (slope of last 5 values)
    if len(data) >= 5:
        recent = data[-5:]
        slope_num = sum((i - 2) * (recent[i] - sum(recent)/5) for i in range(5))
        slope_den = sum((i - 2) ** 2 for i in range(5))
        if slope_den > 0:
            result["spread_expansion_rate"] = slope_num / slope_den

    # Percentile
    sorted_data = sorted(data)
    rank = sum(1 for x in sorted_data if x <= current) / n
    result["spread_percentile"] = rank

    # Liquidity withdrawal: sudden jump in spread
    if n >= 3:
        prev_avg = sum(data[-4:-1]) / 3
        if prev_avg > 0:
            jump = (current - prev_avg) / prev_avg
            if jump > 0.5:  # >50% jump in spread
                result["liquidity_withdrawal"] = min(1.0, jump)

    return result


def estimate_market_impact(
    orderbook_bid_volume: float,
    orderbook_ask_volume: float,
    avg_trade_volume: float,
    spread_bps: float,
) -> dict:
    """Estimate cost of entering/exiting a position.

    Models:
    - Immediate impact (half-spread cost)
    - Depth impact (how much volume needs to be consumed)
    - Total estimated cost in bps

    Returns:
        immediate_cost_bps: half-spread
        depth_impact_bps: estimated additional slippage
        total_cost_bps: immediate + depth
        entry_feasibility: 0-1, how easily a position can be opened
    """
    immediate_cost = spread_bps / 2.0

    # Depth impact: trade size relative to available liquidity
    avg_depth = (orderbook_bid_volume + orderbook_ask_volume) / 2.0 if (orderbook_bid_volume + orderbook_ask_volume) > 0 else 1.0
    trade_to_depth = avg_trade_volume / max(avg_depth, 1e-12)

    # Square-root market impact model (standard in microstructure)
    # Impact ≈ k * sqrt(V/ADV) where V = trade size, ADV = average depth
    depth_impact = spread_bps * (trade_to_depth ** 0.5) if trade_to_depth < 10.0 else spread_bps * 3.16

    total_cost = immediate_cost + depth_impact

    # Entry feasibility: can we enter without massive slippage?
    feasibility = max(0.0, min(1.0, 1.0 - min(1.0, trade_to_depth)))

    return {
        "immediate_cost_bps": immediate_cost,
        "depth_impact_bps": depth_impact,
        "total_cost_bps": total_cost,
        "entry_feasibility": feasibility,
    }
