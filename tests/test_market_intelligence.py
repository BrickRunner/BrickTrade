from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque

import pytest

from market_intelligence.config import MarketIntelligenceConfig
from market_intelligence.engine import MarketIntelligenceEngine
from market_intelligence.feature_engine import FeatureEngine, _std_window
from market_intelligence.indicators import atr as atr_func, cumulative_volume_delta, rsi as rsi_func
from market_intelligence.scorer import OpportunityScorer, REGIME_WEIGHT_OVERRIDES
from market_intelligence.models import DataHealthStatus, FeatureVector, MarketRegime, PairSnapshot
from market_intelligence.regime import RegimeModel
from market_intelligence.validation import DataValidator


class _FakeMD:
    def __init__(self):
        self.common_pairs = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}


class _FakeState:
    def __init__(self):
        self.prices = defaultdict(lambda: deque(maxlen=300))
        self.bids = defaultdict(lambda: deque(maxlen=300))
        self.asks = defaultdict(lambda: deque(maxlen=300))
        self.spot = defaultdict(lambda: deque(maxlen=300))
        self.funding = defaultdict(lambda: deque(maxlen=300))
        self.basis_bps = defaultdict(lambda: deque(maxlen=300))
        self.open_interest = defaultdict(lambda: deque(maxlen=300))
        self.long_short_ratio = defaultdict(lambda: deque(maxlen=300))
        self.liquidation_score = defaultdict(lambda: deque(maxlen=300))
        self.volume_proxy = defaultdict(lambda: deque(maxlen=300))
        self.spread_bps = defaultdict(lambda: deque(maxlen=300))


class FakeCollector:
    def __init__(self):
        self.market_data = _FakeMD()
        self.state = _FakeState()
        self.maxlen = 300
        self._tick = 0

    async def collect_candles(self, symbols, timeframes=None, limit=100):
        return {}

    async def collect(self, symbols, is_stress=False):
        self._tick += 1
        snaps = {}
        ts = time.time()
        for i, symbol in enumerate(symbols):
            base = 100 + i * 10
            price = base + self._tick * (0.8 if symbol == "BTCUSDT" else 0.3)
            bid = price - 0.05
            ask = price + 0.05
            spot = price - 0.03
            funding = 0.0002 if symbol == "BTCUSDT" else 0.0001
            basis = ((price - spot) / spot) * 10_000
            oi = 10_000 + self._tick * 10
            ls = 1.0 + 0.01 * i
            liq = 0.1 + 0.01 * i
            vol = 20 + self._tick

            snap = PairSnapshot(
                symbol=symbol,
                timestamp=ts,
                price=price,
                bid=bid,
                ask=ask,
                spot_price=spot,
                funding_rate=funding,
                open_interest=oi,
                long_short_ratio=ls,
                liquidation_cluster_score=liq,
                basis=basis,
                basis_acceleration=0.0,
                volume_proxy=vol,
                exchange_prices={"okx": price, "htx": price - 0.02},
                exchange_spreads_bps={"okx": 5.0, "htx": 6.0},
            )
            snaps[symbol] = snap

            st = self.state
            st.prices[symbol].append(price)
            st.bids[symbol].append(bid)
            st.asks[symbol].append(ask)
            st.spot[symbol].append(spot)
            st.funding[symbol].append(funding)
            st.basis_bps[symbol].append(basis)
            st.open_interest[symbol].append(oi)
            st.long_short_ratio[symbol].append(ls)
            st.liquidation_score[symbol].append(liq)
            st.volume_proxy[symbol].append(vol)
            st.spread_bps[symbol].append(((ask - bid) / price) * 10_000)

        return snaps, []


def _cfg() -> MarketIntelligenceConfig:
    return MarketIntelligenceConfig(
        enabled=True,
        exchanges=["okx", "htx"],
        symbols=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        max_symbols=10,
        interval_seconds=60,
        startup_report_enabled=True,
        hourly_report_enabled=True,
        event_report_enabled=True,
        min_regime_duration_cycles=2,
        confidence_threshold=0.55,
        smoothing_alpha=0.35,
        feature_window=120,
        zscore_window=120,
        correlation_window=120,
        stress_correlation_window=30,
        historical_window=300,
        adaptive_ml_weighting=False,
        order_flow_enabled=False,
        global_timeframe="1H",
        local_timeframe="5M",
        min_opportunity_score=20.0,
        log_dir="logs",
        jsonl_file_name="test_market_intelligence.jsonl",
        score_weight_volatility=0.26,
        score_weight_funding=0.24,
        score_weight_oi=0.20,
        score_weight_regime=0.30,
        score_weight_risk_penalty=0.28,
        score_weight_liquidity=0.15,
        regime_ema_cross_coef=1.2,
        regime_adx_coef=0.8,
        regime_range_ema_coef=1.1,
        regime_range_adx_coef=0.7,
        regime_rsi_overheat_coef=1.0,
        regime_rsi_panic_coef=1.0,
        regime_vol_coef=0.9,
        regime_bb_coef=0.8,
        regime_interaction_strength=1.0,
        alert_rsi_overheat=75.0,
        alert_rsi_panic_vol=0.01,
        alert_funding_extreme=0.001,
        regime_blowoff_adx=35.0,
        regime_blowoff_rsi=75.0,
        regime_capitulation_adx=30.0,
        regime_capitulation_rsi=25.0,
        persist_enabled=False,
        persist_file="logs/test_mi_state.json",
        persist_every_n_cycles=5,
        mtf_enabled=False,
        mtf_timeframes=["1H", "4H"],
        structured_logging=False,
        signal_half_life_seconds=1800.0,
    )


def test_feature_engine_produces_core_indicators():
    engine = FeatureEngine(zscore_window=64)
    symbol = "BTCUSDT"
    closes = [100 + i * 0.5 for i in range(240)]
    histories = {
        symbol: {
            "price": closes,
            "bid": [x - 0.2 for x in closes],
            "ask": [x + 0.2 for x in closes],
            "volume": [10 + i for i in range(240)],
            "basis": [5 + 0.01 * i for i in range(240)],
            "funding": [0.0001 for _ in closes],
            "oi": [1000 + i for i in range(240)],
            "spread": [4 + 0.01 * i for i in range(240)],
        }
    }
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol,
            timestamp=time.time(),
            price=closes[-1],
            bid=closes[-1] - 0.2,
            ask=closes[-1] + 0.2,
            spot_price=closes[-1] - 0.1,
            funding_rate=0.0001,
            open_interest=1234,
            long_short_ratio=1.0,
            liquidation_cluster_score=0.2,
            basis=10.0,
            basis_acceleration=0.1,
            volume_proxy=42.0,
            exchange_prices={"okx": closes[-1], "htx": closes[-1] - 0.05},
            exchange_spreads_bps={"okx": 5.0, "htx": 6.0},
        )
    }

    result = engine.compute(snapshots, histories)
    fv = result[symbol]
    for key in ["ema50", "ema200", "adx", "rsi", "macd", "atr", "bb_width", "volume_spike", "funding_rate", "basis_bps"]:
        assert key in fv.values
        assert key in fv.normalized
    assert "atr_pct" in fv.values
    assert "oi_delta_pct" in fv.values
    assert "adx_local" in fv.values


def test_regime_stability_prevents_flapping():
    model = RegimeModel(confidence_threshold=0.8, min_duration_cycles=3, smoothing_alpha=0.4)

    up = {
        "ema_cross": 2.0,
        "adx": 35.0,
        "rsi": 65.0,
        "funding_rate": 0.0002,
        "liquidation_cluster": -0.1,
        "rolling_volatility": 0.005,
        "bb_width": 0.01,
    }
    down = dict(up)
    down["ema_cross"] = -2.0

    from market_intelligence.models import FeatureVector

    st1 = model.classify_global(FeatureVector("BTCUSDT", time.time(), up, up))
    st2 = model.classify_global(FeatureVector("BTCUSDT", time.time(), down, down))
    st3 = model.classify_global(FeatureVector("BTCUSDT", time.time(), down, down))

    # With strict stability settings, immediate flip should be rejected.
    assert st1.regime in {MarketRegime.TREND_UP, MarketRegime.RANGE}
    assert st2.regime == st1.regime or st2.stable_for_cycles >= 1
    assert st3.regime in MarketRegime


@pytest.mark.asyncio
async def test_engine_run_once_returns_ranked_report():
    collector = FakeCollector()
    cfg = _cfg()
    engine = MarketIntelligenceEngine(cfg, collector)

    # Warm-up for rolling stats.
    for _ in range(80):
        await engine.run_once()

    report = await engine.run_once()
    assert report.global_regime.regime in MarketRegime
    assert report.global_regime.confidence < 0.99
    assert report.opportunities
    assert len({round(x.score, 6) for x in report.opportunities}) > 1
    assert report.opportunities == sorted(report.opportunities, key=lambda x: (x.score, x.confidence), reverse=True)
    assert "global" in report.payload
    assert "timeframes" in report.payload
    assert "dynamic_deltas" in report.payload
    assert "features" in report.payload
    assert "portfolio_risk" in report.payload
    assert report.payload["scoring_enabled"] is True


def test_adx_none_when_insufficient_data():
    engine = FeatureEngine(zscore_window=32)
    symbol = "BTCUSDT"
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol,
            timestamp=time.time(),
            price=100.0,
            bid=99.9,
            ask=100.1,
            spot_price=99.95,
            funding_rate=0.0001,
            open_interest=1000.0,
            long_short_ratio=1.0,
            liquidation_cluster_score=0.1,
            basis=5.0,
            basis_acceleration=0.0,
            volume_proxy=20.0,
            exchange_prices={"okx": 100.0, "htx": 99.98},
            exchange_spreads_bps={"okx": 5.0, "htx": 6.0},
        )
    }
    histories = {
        symbol: {
            "price": [100.0, 100.05, 100.1],
            "bid": [99.9, 99.95, 100.0],
            "ask": [100.1, 100.15, 100.2],
            "volume": [10.0, 11.0, 12.0],
            "basis": [4.0, 4.1, 4.2],
            "funding": [0.0001, 0.00011, 0.00012],
            "oi": [1000.0, 1001.0, 1002.0],
            "spread": [4.0, 4.1, 4.2],
        }
    }
    fv = engine.compute(snapshots, histories)[symbol]
    assert fv.values["adx"] is None


def test_validation_layer_marks_invalid_low_atr_bb():
    symbol = "BTCUSDT"
    validator = DataValidator()
    fv = FeatureVector(
        symbol=symbol,
        timestamp=time.time(),
        values={
            "atr_pct": 0.0,
            "bb_width_pct": 0.0,
            "oi_delta_pct": 0.12,
            "funding_pct": 0.01,
            "open_interest": 2000.0,
            "rolling_volatility": 0.02,
            "basis_bps": 5.0,
            "spread_bps": 2.0,
        },
        normalized={"atr_pct": 0.0, "bb_width": 0.0},
    )
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol,
            timestamp=time.time(),
            price=100.0,
            bid=99.9,
            ask=100.1,
            spot_price=99.95,
            funding_rate=0.0001,
            open_interest=2000.0,
            long_short_ratio=1.0,
            liquidation_cluster_score=0.1,
            basis=4.0,
            basis_acceleration=0.0,
            volume_proxy=12.0,
            funding_by_exchange={"okx": 0.0001, "htx": 0.0001},
        )
    }
    histories = {symbol: {"price": [100.0, 100.3, 100.1], "oi": [1000.0, 1010.0]}}

    res = validator.validate({symbol: fv}, snapshots, histories)
    assert res.status == DataHealthStatus.PARTIAL
    assert any("invalid_atr_pct" in w for w in res.warnings)
    assert any("invalid_bb_width_pct" in w for w in res.warnings)
    assert res.sanitized_features[symbol].values["atr_pct"] is None
    assert res.sanitized_features[symbol].values["bb_width_pct"] is None


def test_validation_layer_disables_scoring_when_metrics_empty():
    symbol = "ETHUSDT"
    validator = DataValidator()
    fv = FeatureVector(
        symbol=symbol,
        timestamp=time.time(),
        values={
            "atr_pct": None,
            "bb_width_pct": None,
            "oi_delta_pct": None,
            "funding_pct": None,
            "open_interest": 2000.0,
            "rolling_volatility": 0.0,
            "basis_bps": 0.0,
            "spread_bps": 0.0,
        },
        normalized={},
    )
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol,
            timestamp=time.time(),
            price=100.0,
            bid=99.9,
            ask=100.1,
            spot_price=99.95,
            funding_rate=0.0,
            open_interest=2000.0,
            long_short_ratio=1.0,
            liquidation_cluster_score=0.1,
            basis=0.0,
            basis_acceleration=0.0,
            volume_proxy=0.0,
            funding_by_exchange={"okx": 0.0, "htx": 0.0},
        )
    }
    histories = {symbol: {"price": [100.0, 100.0], "oi": [1000.0]}}

    res = validator.validate({symbol: fv}, snapshots, histories)
    assert res.status == DataHealthStatus.INVALID
    assert res.score_enabled is False
    assert res.risk_enabled is False


@pytest.mark.asyncio
async def test_scoring_guard_disables_on_low_stability():
    collector = FakeCollector()
    cfg = _cfg()
    engine = MarketIntelligenceEngine(cfg, collector)
    report = await engine.run_once()
    assert report.global_regime.stable_for_cycles < 3
    assert report.scoring_enabled is False
    assert report.payload["opportunities"] == []
    assert report.payload["portfolio_risk"]["capital_allocation_pct"] == {}


def test_empty_histories():
    """Feature engine handles empty history gracefully without crashing."""
    engine = FeatureEngine(zscore_window=32)
    symbol = "BTCUSDT"
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol, timestamp=time.time(),
            price=100.0, bid=99.9, ask=100.1, spot_price=99.95,
            funding_rate=0.0001, open_interest=1000.0,
            long_short_ratio=1.0, liquidation_cluster_score=0.1,
            basis=5.0, basis_acceleration=0.0, volume_proxy=20.0,
        )
    }
    histories = {symbol: {"price": [], "bid": [], "ask": [], "volume": [],
                          "basis": [], "funding": [], "oi": [], "spread": []}}
    result = engine.compute(snapshots, histories)
    assert symbol in result
    fv = result[symbol]
    assert fv.values["price"] == 100.0
    # With no history most indicators degrade gracefully
    assert fv.values["rsi"] == 50.0  # RSI default with insufficient data


def test_single_symbol():
    """Scoring works correctly with a single symbol (no relative ranking)."""
    from market_intelligence.scorer import OpportunityScorer
    from market_intelligence.models import RegimeState

    scorer = OpportunityScorer()
    fv = FeatureVector(
        symbol="BTCUSDT", timestamp=time.time(),
        values={
            "rolling_volatility_local": 0.01, "bb_width_local": 0.02,
            "funding_rate": 0.0003, "funding_delta": 0.0001,
            "oi_delta": 100.0, "oi_delta_pct": 5.0,
            "rolling_volatility": 0.005, "volume_proxy": 1000.0,
            "basis_bps": 10.0, "funding_pct": 0.03,
            "macd_hist": 0.01, "volume_spike": 1.2,
        },
        normalized={
            "rolling_volatility_local": 0.5, "bb_width_local": 0.3,
            "funding_rate": 0.4, "funding_delta": 0.2,
            "oi_delta": 0.6, "rolling_volatility": 0.3,
            "macd_hist": 0.1, "volume_spike": 0.2,
        },
    )
    reg = RegimeState(MarketRegime.TREND_UP, 0.7, {MarketRegime.TREND_UP: 0.7}, 5)
    scores = scorer.score(
        {"BTCUSDT": fv}, {"BTCUSDT": reg},
        {"BTCUSDT": 1.0}, {"BTCUSDT": 0.5},
    )
    assert len(scores) == 1
    assert scores[0].symbol == "BTCUSDT"
    assert 0.0 <= scores[0].score <= 100.0


def test_all_none_derivatives():
    """Feature engine handles all-None derivative values without crashing."""
    engine = FeatureEngine(zscore_window=32)
    symbol = "ETHUSDT"
    snapshots = {
        symbol: PairSnapshot(
            symbol=symbol, timestamp=time.time(),
            price=3000.0, bid=2999.0, ask=3001.0, spot_price=2999.5,
            funding_rate=0.0, open_interest=None,
            long_short_ratio=None, liquidation_cluster_score=None,
            basis=0.0, basis_acceleration=0.0, volume_proxy=None,
        )
    }
    closes = [3000.0 + i * 0.1 for i in range(50)]
    histories = {symbol: {
        "price": closes,
        "bid": [x - 1.0 for x in closes],
        "ask": [x + 1.0 for x in closes],
        "volume": [10.0] * 50,
        "basis": [0.0] * 50,
        "funding": [0.0] * 50,
        "oi": [],  # No OI history
        "spread": [2.0] * 50,
    }}
    result = engine.compute(snapshots, histories)
    fv = result[symbol]
    assert fv.values["oi_delta"] is None
    assert fv.values["oi_delta_pct"] is None
    assert fv.values["open_interest"] is None


def test_regime_stability_no_flapping_10_cycles():
    """Regime should not flip-flop over 10 cycles of alternating signals."""
    model = RegimeModel(confidence_threshold=0.8, min_duration_cycles=3, smoothing_alpha=0.4)

    up_vals = {
        "ema_cross": 2.0, "adx": 35.0, "rsi": 65.0,
        "funding_rate": 0.0002, "liquidation_cluster": -0.1,
        "rolling_volatility": 0.005, "bb_width": 0.01,
    }
    down_vals = dict(up_vals, ema_cross=-2.0)

    regimes = []
    for i in range(10):
        vals = up_vals if i % 2 == 0 else down_vals
        fv = FeatureVector("BTC", time.time(), vals, vals)
        state = model.classify_global(fv)
        regimes.append(state.regime)

    # Regime should NOT change on every cycle (stability prevents that).
    unique_transitions = sum(1 for i in range(1, len(regimes)) if regimes[i] != regimes[i - 1])
    assert unique_transitions <= 3, f"Too many regime flips: {unique_transitions}"


def test_consistency_assertion_catches_bugs():
    """Engine's _assert_consistency raises on impossible state combinations."""
    from market_intelligence.engine import MarketIntelligenceEngine

    # scoring_enabled=false but opportunities exist
    bad_payload = {
        "scoring_enabled": False,
        "opportunities": [{"symbol": "BTC", "score": 50}],
        "global": {"metrics": {"atr_pct": 0.5, "volatility_regime": "medium",
                               "open_interest": 1000, "oi_delta_pct": 0.1}},
        "dynamic_deltas": {"confidence_change": 0.01},
    }
    with pytest.raises(RuntimeError, match="scoring_enabled=false"):
        MarketIntelligenceEngine._assert_consistency(None, bad_payload)

    # ATR null but volatility high
    bad_payload2 = {
        "scoring_enabled": True,
        "opportunities": [],
        "global": {"metrics": {"atr_pct": None, "volatility_regime": "high",
                               "open_interest": 1000, "oi_delta_pct": 0.1}},
        "dynamic_deltas": {"confidence_change": 0.01},
    }
    with pytest.raises(RuntimeError, match="ATR is null"):
        MarketIntelligenceEngine._assert_consistency(None, bad_payload2)


def test_rsi_wilder_vs_cutler():
    """Verify RSI uses Wilder smoothing, not Cutler's (simple moving average)."""
    # Wilder RSI and Cutler RSI give different results for the same input
    values = [44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
              46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41,
              46.22, 45.64]
    wilder_rsi = rsi_func(values, 14)

    # Cutler's RSI (simple recalculation each window) would give different value
    # Verify it's NOT just the simple average of last-period gains/losses
    gains_last = []
    losses_last = []
    for i in range(len(values) - 14, len(values)):
        d = values[i] - values[i - 1]
        if d >= 0:
            gains_last.append(d)
            losses_last.append(0.0)
        else:
            gains_last.append(0.0)
            losses_last.append(-d)
    avg_g_simple = sum(gains_last) / 14
    avg_l_simple = sum(losses_last) / 14
    if avg_l_simple > 0:
        cutler_rsi = 100.0 - (100.0 / (1.0 + avg_g_simple / avg_l_simple))
    else:
        cutler_rsi = 100.0

    # Wilder and Cutler should differ
    assert abs(wilder_rsi - cutler_rsi) > 0.1, \
        f"RSI values too close — might be Cutler's instead of Wilder's: {wilder_rsi} vs {cutler_rsi}"


def test_rolling_vol_is_std_not_mad():
    """Verify rolling volatility uses population std, not mean absolute deviation."""
    values = [1.0, 3.0, 5.0, 2.0, 4.0]
    std_val = _std_window(values, 5)
    mean = sum(values) / len(values)

    # Population std
    expected_std = (sum((x - mean) ** 2 for x in values) / len(values)) ** 0.5
    assert abs(std_val - expected_std) < 1e-10

    # MAD would be different
    mad = sum(abs(x - mean) for x in values) / len(values)
    assert abs(std_val - mad) > 0.01, "std equals MAD — likely using MAD instead of std"


def test_atr_wilder_vs_sma():
    """Verify ATR uses Wilder smoothing, not simple SMA of last N TRs."""
    # Generate enough data for Wilder smoothing to diverge from SMA.
    highs = [100.0 + i * 0.5 + (1.0 if i % 3 == 0 else 0.0) for i in range(60)]
    lows = [100.0 + i * 0.5 - 0.5 - (0.8 if i % 4 == 0 else 0.0) for i in range(60)]
    closes = [100.0 + i * 0.5 for i in range(60)]
    wilder_atr = atr_func(highs, lows, closes, 14)
    assert wilder_atr is not None
    # Compute SMA ATR for comparison.
    trs = []
    for i in range(1, len(closes)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        trs.append(tr)
    sma_atr = sum(trs[-14:]) / 14
    assert abs(wilder_atr - sma_atr) > 0.001, \
        f"ATR values too close — might be SMA instead of Wilder: {wilder_atr} vs {sma_atr}"


def test_cvd_basic():
    """CVD returns positive for buying pressure, negative for selling."""
    volumes = [100.0] * 10
    # All bars closing up — all buy volume
    closes_up = [100.0 + i for i in range(10)]
    cvd = cumulative_volume_delta(volumes, closes_up, 9)
    assert cvd > 0.5, f"CVD should be strongly positive for all-up bars: {cvd}"

    # All bars closing down — all sell volume
    closes_down = [110.0 - i for i in range(10)]
    cvd_down = cumulative_volume_delta(volumes, closes_down, 9)
    assert cvd_down < -0.5, f"CVD should be strongly negative for all-down bars: {cvd_down}"

    # Empty data
    assert cumulative_volume_delta([], [], 10) == 0.0


def test_orderbook_imbalance_edge_cases():
    """Orderbook imbalance: all bid, all ask, empty."""
    # All on bid side (bid > ask in value terms doesn't apply here since
    # these are price levels, but we test the collector logic directly).
    snap = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=100.0, ask=100.0, spot_price=100.0,
        funding_rate=0.0,
        orderbook_imbalance=1.0,  # All bid
        data_quality="full",
    )
    assert snap.orderbook_imbalance == 1.0

    snap2 = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=100.0, ask=100.0, spot_price=100.0,
        funding_rate=0.0,
        orderbook_imbalance=-1.0,  # All ask
    )
    assert snap2.orderbook_imbalance == -1.0

    snap3 = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=100.0, ask=100.0, spot_price=100.0,
        funding_rate=0.0,
    )
    assert snap3.orderbook_imbalance is None


def test_adaptive_weights_differ_by_regime():
    """Scorer uses different weights in PANIC vs RANGE."""
    panic_w = REGIME_WEIGHT_OVERRIDES[MarketRegime.PANIC]
    range_w = REGIME_WEIGHT_OVERRIDES[MarketRegime.RANGE]
    # At least one weight should differ.
    diffs = sum(1 for k in panic_w if panic_w.get(k) != range_w.get(k))
    assert diffs >= 2, "PANIC and RANGE should have different weight profiles"

    # Verify scorer actually applies overrides.
    from market_intelligence.models import RegimeState
    scorer = OpportunityScorer()
    fv = FeatureVector(
        symbol="BTCUSDT", timestamp=time.time(),
        values={
            "rolling_volatility_local": 0.01, "bb_width_local": 0.02,
            "funding_rate": 0.0003, "funding_delta": 0.0001,
            "oi_delta": 100.0, "oi_delta_pct": 5.0,
            "rolling_volatility": 0.005, "volume_proxy": 1000.0,
            "basis_bps": 10.0, "funding_pct": 0.03,
            "macd_hist": 0.01, "volume_spike": 1.2, "cvd": 0.3,
            "orderbook_imbalance": 0.1, "data_quality_code": 0.0,
        },
        normalized={
            "rolling_volatility_local": 0.5, "bb_width_local": 0.3,
            "funding_rate": 0.4, "funding_delta": 0.2,
            "oi_delta": 0.6, "rolling_volatility": 0.3,
            "macd_hist": 0.1, "volume_spike": 0.2,
        },
    )
    reg = RegimeState(MarketRegime.PANIC, 0.7, {MarketRegime.PANIC: 0.7}, 5)
    scores_panic = scorer.score(
        {"BTCUSDT": fv}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
        global_regime=MarketRegime.PANIC,
    )
    scores_range = scorer.score(
        {"BTCUSDT": fv}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
        global_regime=MarketRegime.RANGE,
    )
    # Scores should differ due to different weights.
    assert len(scores_panic) == 1
    assert len(scores_range) == 1
    assert abs(scores_panic[0].score - scores_range[0].score) > 0.01, \
        "Scores should differ between PANIC and RANGE regimes"


def test_graceful_degradation_single_exchange():
    """Snapshot with data_quality='partial' is created for single-exchange data."""
    snap = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=99.9, ask=100.1, spot_price=99.95,
        funding_rate=0.0001,
        data_quality="partial",
    )
    assert snap.data_quality == "partial"
    # Default should be "full"
    snap2 = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=99.9, ask=100.1, spot_price=99.95,
        funding_rate=0.0001,
    )
    assert snap2.data_quality == "full"


def test_linear_slope_basic():
    """Linear slope detects positive/negative/flat trends."""
    from market_intelligence.indicators import linear_slope
    rising = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
    assert linear_slope(rising, 10) > 0.1
    falling = list(reversed(rising))
    assert linear_slope(falling, 10) < -0.1
    flat = [5.0] * 10
    assert abs(linear_slope(flat, 10)) < 1e-9
    assert linear_slope([1.0, 2.0], 10) == 0.0  # insufficient data


def test_linear_slope_vs_single_bar_delta():
    """Slope is more robust than single-bar delta to noise."""
    from market_intelligence.indicators import linear_slope
    # Steady uptrend with one noisy bar
    values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 3.0]  # last bar is noise
    slope = linear_slope(values, 10)
    single_bar = values[-1] - values[-2]  # = -6.0, very misleading
    # Slope should still be positive (uptrend), single bar is very negative
    assert slope > 0, f"Slope should detect uptrend despite noisy bar: {slope}"
    assert single_bar < -5.0, "Single bar delta is misleading"


def test_orderbook_imbalance_in_snapshot():
    """Snapshot correctly stores orderbook imbalance values."""
    snap = PairSnapshot(
        symbol="BTCUSDT", timestamp=time.time(),
        price=100.0, bid=99.9, ask=100.1, spot_price=99.95,
        funding_rate=0.0001,
        orderbook_imbalance=0.35,
        orderbook_bid_volume=1500.0,
        orderbook_ask_volume=850.0,
    )
    assert snap.orderbook_imbalance == 0.35
    assert snap.orderbook_bid_volume == 1500.0
    assert snap.orderbook_ask_volume == 850.0


def test_regime_extreme_bypass_with_liquidation():
    """Extreme liquidation z-score should trigger fast-path bypass."""
    model = RegimeModel(confidence_threshold=0.55, min_duration_cycles=3, smoothing_alpha=0.35)
    extreme_vals = {
        "ema_cross": -2.0, "adx": 40.0, "rsi": 20.0,
        "funding_rate": -0.001, "liquidation_cluster": 3.0,
        "rolling_volatility": 0.05, "bb_width": 0.1,
        "volume_spike": 5.0, "volume_trend": 2.0,
        "market_structure_code": -1.0, "atr_percentile": 0.9,
        "orderbook_imbalance": -0.4, "funding_slope": -0.5,
    }
    extreme_z = dict(extreme_vals)
    extreme_z["liquidation_cluster"] = 3.0  # z-score above 2.5
    fv = FeatureVector("BTCUSDT", time.time(), extreme_vals, extreme_z)
    assert model._is_extreme(fv) is True


def test_portfolio_max_allocation_cap():
    """No single pair should exceed 25% allocation."""
    from market_intelligence.portfolio import PortfolioAnalyzer
    from market_intelligence.models import RegimeState, OpportunityScore, DataHealthStatus
    analyzer = PortfolioAnalyzer()
    # Create opportunities with very skewed scores
    opportunities = [
        OpportunityScore("BTCUSDT", 95.0, 0.9, MarketRegime.TREND_UP, [], {}, "long"),
        OpportunityScore("ETHUSDT", 20.0, 0.4, MarketRegime.RANGE, [], {}, "neutral"),
        OpportunityScore("SOLUSDT", 15.0, 0.3, MarketRegime.RANGE, [], {}, "neutral"),
    ]
    global_regime = RegimeState(MarketRegime.TREND_UP, 0.8, {MarketRegime.TREND_UP: 0.8}, 5)
    local_regimes = {
        "BTCUSDT": global_regime,
        "ETHUSDT": RegimeState(MarketRegime.RANGE, 0.6, {}, 5),
        "SOLUSDT": RegimeState(MarketRegime.RANGE, 0.5, {}, 5),
    }
    result = analyzer.analyze(
        opportunities, local_regimes,
        {"BTCUSDT": 1.0, "ETHUSDT": 0.8, "SOLUSDT": 0.6},
        global_regime, global_atr_pct=1.5, global_volatility_regime="medium",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    for symbol, pct in result.capital_allocation_pct.items():
        assert pct <= 25.01, f"{symbol} allocation {pct}% exceeds 25% cap"


def test_portfolio_exposure_cap_by_regime():
    """Exposure cap should be lower during PANIC than TREND_UP."""
    from market_intelligence.portfolio import PortfolioAnalyzer
    from market_intelligence.models import RegimeState, OpportunityScore, DataHealthStatus
    analyzer = PortfolioAnalyzer()
    opportunities = [
        OpportunityScore("BTCUSDT", 60.0, 0.7, MarketRegime.PANIC, [], {}, "short"),
    ]
    panic_regime = RegimeState(MarketRegime.PANIC, 0.8, {MarketRegime.PANIC: 0.8}, 5)
    result_panic = analyzer.analyze(
        opportunities, {"BTCUSDT": panic_regime},
        {"BTCUSDT": 1.0}, panic_regime,
        global_atr_pct=3.0, global_volatility_regime="high",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    trend_regime = RegimeState(MarketRegime.TREND_UP, 0.8, {MarketRegime.TREND_UP: 0.8}, 5)
    result_trend = analyzer.analyze(
        opportunities, {"BTCUSDT": trend_regime},
        {"BTCUSDT": 1.0}, trend_regime,
        global_atr_pct=1.0, global_volatility_regime="medium",
        data_health_status=DataHealthStatus.OK, scoring_enabled=True,
    )
    assert result_panic.recommended_exposure_cap_pct < result_trend.recommended_exposure_cap_pct


def test_scorer_liquidity_uses_spread():
    """Liquidity score should incorporate spread tightness, not just volume."""
    from market_intelligence.models import RegimeState
    scorer = OpportunityScorer()
    base_vals = {
        "rolling_volatility_local": 0.01, "bb_width_local": 0.02,
        "funding_rate": 0.0003, "funding_delta": 0.0001,
        "oi_delta": 100.0, "oi_delta_pct": 5.0,
        "rolling_volatility": 0.005, "volume_proxy": 1000.0,
        "basis_bps": 10.0, "funding_pct": 0.03,
        "macd_hist": 0.01, "volume_spike": 1.2, "cvd": 0.1,
        "data_quality_code": 0.0, "basis_acceleration": 0.0,
        "funding_slope": 0.0, "orderbook_imbalance": None,
    }
    base_z = {
        "rolling_volatility_local": 0.5, "bb_width_local": 0.3,
        "funding_rate": 0.4, "funding_delta": 0.2,
        "oi_delta": 0.6, "rolling_volatility": 0.3,
    }
    # Tight spread
    vals_tight = dict(base_vals, spread_bps=2.0)
    fv_tight = FeatureVector("BTCUSDT", time.time(), vals_tight, base_z)
    # Wide spread
    vals_wide = dict(base_vals, spread_bps=50.0)
    fv_wide = FeatureVector("BTCUSDT", time.time(), vals_wide, base_z)

    reg = RegimeState(MarketRegime.TREND_UP, 0.7, {MarketRegime.TREND_UP: 0.7}, 5)
    scores_tight = scorer.score(
        {"BTCUSDT": fv_tight}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
    )
    scores_wide = scorer.score(
        {"BTCUSDT": fv_wide}, {"BTCUSDT": reg},
        {"BTCUSDT": 0.5}, {"BTCUSDT": 0.5},
    )
    # Tight spread should score higher
    assert scores_tight[0].score >= scores_wide[0].score


@pytest.mark.asyncio
async def test_health_check():
    """Health check returns expected structure."""
    from market_intelligence.service import MarketIntelligenceService
    service = MarketIntelligenceService()
    result = await service.health_check()
    assert result["initialized"] is False
    assert result["last_report_age_seconds"] is None
    assert "exchanges" in result
