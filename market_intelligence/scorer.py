from __future__ import annotations

import math
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from market_intelligence.ml_weights import OnlineWeightOptimizer
from market_intelligence.models import FeatureKey, FeatureVector, MarketRegime, OpportunityScore, RegimeState


REGIME_WEIGHT_OVERRIDES: Dict[MarketRegime, Dict[str, float]] = {
    MarketRegime.PANIC: {"w_volatility": 0.15, "w_funding": 0.30, "w_oi": 0.25, "w_liquidity": 0.25, "w_risk_penalty": 0.35},
    MarketRegime.HIGH_VOLATILITY: {"w_volatility": 0.30, "w_funding": 0.20, "w_oi": 0.25, "w_liquidity": 0.20, "w_risk_penalty": 0.30},
    MarketRegime.RANGE: {"w_volatility": 0.20, "w_funding": 0.30, "w_oi": 0.15, "w_regime": 0.25, "w_risk_penalty": 0.22},
    MarketRegime.TREND_UP: {"w_volatility": 0.25, "w_funding": 0.20, "w_oi": 0.22, "w_regime": 0.35, "w_risk_penalty": 0.25},
    MarketRegime.TREND_DOWN: {"w_volatility": 0.25, "w_funding": 0.20, "w_oi": 0.22, "w_regime": 0.35, "w_risk_penalty": 0.25},
}


class OpportunityScorer:
    def __init__(
        self,
        adaptive_ml_weighting: bool = False,
        w_volatility: float = 0.26,
        w_funding: float = 0.24,
        w_oi: float = 0.20,
        w_regime: float = 0.30,
        w_risk_penalty: float = 0.28,
        w_liquidity: float = 0.15,
        weights_file: Optional[Path] = None,
        signal_half_life_seconds: float = 1800.0,
    ):
        self.adaptive_ml_weighting = adaptive_ml_weighting
        self.w_volatility = w_volatility
        self.w_funding = w_funding
        self.w_oi = w_oi
        self.w_regime = w_regime
        self.w_risk_penalty = w_risk_penalty
        self.w_liquidity = w_liquidity

        # BLOCK 3: Signal time-decay tracking
        self._first_seen: Dict[str, float] = {}  # symbol -> timestamp первого обнаружения
        self._signal_half_life_seconds: float = signal_half_life_seconds

        # BLOCK 4.1: Initialize online weight optimizer
        self._weight_optimizer: Optional[OnlineWeightOptimizer] = None
        if adaptive_ml_weighting:
            self._weight_optimizer = OnlineWeightOptimizer()
            if weights_file and weights_file.exists():
                self._weight_optimizer.load(weights_file)

    def score(
        self,
        features: Dict[str, FeatureVector],
        local_regimes: Dict[str, RegimeState],
        correlations_to_btc: Dict[str, float],
        spread_correlations_to_btc: Dict[str, float],
        convergence_scores: Dict[str, float] | None = None,
        global_regime: MarketRegime | None = None,
    ) -> List[OpportunityScore]:
        raw_rows: List[Tuple[str, RegimeState, float, float, Dict[str, float], List[str], str, float]] = []

        # BLOCK 4.1: Use ML-optimized weights if available and sufficient data
        if self.adaptive_ml_weighting and self._weight_optimizer and self._weight_optimizer.has_sufficient_data():
            ml_weights = self._weight_optimizer.get_weights()
            w_volatility = ml_weights.get("volatility_expansion_score", self.w_volatility)
            w_funding = ml_weights.get("funding_divergence_score", self.w_funding)
            w_oi = ml_weights.get("oi_acceleration_score", self.w_oi)
            w_regime = ml_weights.get("regime_alignment_score", self.w_regime)
            w_risk_penalty = ml_weights.get("risk_penalty", self.w_risk_penalty)
            w_liquidity = ml_weights.get("liquidity_score", self.w_liquidity)
        else:
            # Adaptive weights based on global regime (fallback or default).
            overrides = REGIME_WEIGHT_OVERRIDES.get(global_regime, {}) if global_regime else {}
            w_volatility = overrides.get("w_volatility", self.w_volatility)
            w_funding = overrides.get("w_funding", self.w_funding)
            w_oi = overrides.get("w_oi", self.w_oi)
            w_regime = overrides.get("w_regime", self.w_regime)
            w_risk_penalty = overrides.get("w_risk_penalty", self.w_risk_penalty)
            w_liquidity = overrides.get("w_liquidity", self.w_liquidity)

        # FIX C.3: Normalize positive weights to sum to 1.0
        positive_sum = w_volatility + w_funding + w_oi + w_regime + w_liquidity
        if positive_sum > 0:
            w_volatility /= positive_sum
            w_funding /= positive_sum
            w_oi /= positive_sum
            w_regime /= positive_sum
            w_liquidity /= positive_sum
        # w_risk_penalty remains as-is (it's a penalty, not part of the positive sum)

        # Pre-compute max volume across batch for liquidity normalization.
        volumes = {s: float(f.values.get(FeatureKey.VOLUME_PROXY) or 0.0) for s, f in features.items()}
        max_vol = max(volumes.values()) if volumes else 0.0
        log_max = math.log1p(max_vol)

        for symbol, f in features.items():
            reg = local_regimes[symbol]
            z = f.normalized
            v = f.values

            required = (
                z.get(FeatureKey.ROLLING_VOLATILITY_LOCAL),
                z.get(FeatureKey.BB_WIDTH_LOCAL),
                z.get(FeatureKey.FUNDING_RATE),
                z.get(FeatureKey.FUNDING_DELTA),
                z.get(FeatureKey.OI_DELTA),
                v.get(FeatureKey.OI_DELTA_PCT),
            )
            if any(x is None for x in required):
                continue

            vol_expansion_score = self._clip01(
                (max(0.0, float(z.get(FeatureKey.ROLLING_VOLATILITY_LOCAL))) + max(0.0, float(z.get(FeatureKey.BB_WIDTH_LOCAL)))) / 2.2
            )
            funding_divergence_score = self._clip01(
                abs(float(z.get(FeatureKey.FUNDING_RATE))) * 0.8
                + abs(float(z.get(FeatureKey.FUNDING_DELTA))) * 4.0
                + abs(float(v.get(FeatureKey.FUNDING_PCT))) / 0.2
            )

            # Boost if funding is in extreme territory
            funding_extreme = float(v.get(FeatureKey.FUNDING_EXTREME_FLAG) or 0.0)
            if funding_extreme >= 1.0:
                funding_divergence_score *= 1.2
                funding_divergence_score = min(1.0, funding_divergence_score)

            oi_acceleration_score = self._clip01(
                abs(float(z.get(FeatureKey.OI_DELTA))) * 0.7 + abs(float(v.get(FeatureKey.OI_DELTA_PCT))) / 20.0
            )

            regime_alignment_score = 0.3
            if reg.regime in {MarketRegime.TREND_UP, MarketRegime.TREND_DOWN}:
                regime_alignment_score = 0.55 + 0.45 * reg.confidence
            elif reg.regime == MarketRegime.RANGE:
                regime_alignment_score = 0.45 + 0.35 * reg.confidence
            elif reg.regime in {MarketRegime.PANIC, MarketRegime.OVERHEATED, MarketRegime.HIGH_VOLATILITY}:
                regime_alignment_score = 0.15 + 0.25 * reg.confidence
            # CVD momentum: strong CVD in trend direction boosts alignment.
            cvd = float(v.get(FeatureKey.CVD) or 0.0)
            if reg.regime == MarketRegime.TREND_UP and cvd > 0.2:
                regime_alignment_score *= 1.0 + min(0.15, cvd * 0.3)
            elif reg.regime == MarketRegime.TREND_DOWN and cvd < -0.2:
                regime_alignment_score *= 1.0 + min(0.15, abs(cvd) * 0.3)
            regime_alignment_score = self._clip01(regime_alignment_score)

            # Order flow boost: strong delta flow in trend direction
            flow_delta_ratio = float(v.get(FeatureKey.FLOW_DELTA_RATIO) or 0.0)
            if reg.regime == MarketRegime.TREND_UP and flow_delta_ratio > 0.3:
                regime_alignment_score = min(1.0, regime_alignment_score + 0.05)
            elif reg.regime == MarketRegime.TREND_DOWN and flow_delta_ratio < -0.3:
                regime_alignment_score = min(1.0, regime_alignment_score + 0.05)

            # Orderbook imbalance confirmation/contradiction
            ob_imb_raw = float(v.get(FeatureKey.ORDERBOOK_IMBALANCE) or 0.0) if v.get(FeatureKey.ORDERBOOK_IMBALANCE) is not None else None
            ob_bonus = 0.0
            ob_reason = ""
            if ob_imb_raw is not None:
                if reg.regime == MarketRegime.TREND_UP and ob_imb_raw > 0.15:
                    ob_bonus = 0.08 * min(1.0, ob_imb_raw / 0.5)
                    ob_reason = f"ob_confirms_up={ob_imb_raw:.2f}"
                elif reg.regime == MarketRegime.TREND_DOWN and ob_imb_raw < -0.15:
                    ob_bonus = 0.08 * min(1.0, abs(ob_imb_raw) / 0.5)
                    ob_reason = f"ob_confirms_down={ob_imb_raw:.2f}"
                elif reg.regime == MarketRegime.TREND_UP and ob_imb_raw < -0.2:
                    ob_bonus = -0.05
                    ob_reason = f"ob_contradicts_up={ob_imb_raw:.2f}"
                elif reg.regime == MarketRegime.TREND_DOWN and ob_imb_raw > 0.2:
                    ob_bonus = -0.05
                    ob_reason = f"ob_contradicts_down={ob_imb_raw:.2f}"

            # Liquidity: combine volume rank with spread tightness
            vol_component = self._clip01(
                math.log1p(volumes.get(symbol, 0.0)) / max(log_max, 1e-9)
            ) if log_max > 1e-9 else 0.5
            spread_bps = float(v.get(FeatureKey.SPREAD_BPS) or 0.0)
            spread_component = self._clip01(1.0 - min(1.0, spread_bps / 30.0))
            liquidity_score = 0.6 * vol_component + 0.4 * spread_component

            # Order flow boost: high absorption indicates deep liquidity
            flow_absorption = float(v.get(FeatureKey.FLOW_ABSORPTION_SCORE) or 0.0)
            if flow_absorption > 0.5:
                liquidity_score = min(1.0, liquidity_score + 0.05)

            # NEW: Tight spread boost (spread tighter than usual = good liquidity)
            spread_percentile = float(v.get(FeatureKey.SPREAD_PERCENTILE) or 0.5)
            if spread_percentile < 0.4:
                liquidity_score = min(1.0, liquidity_score * 1.15)

            corr = abs(correlations_to_btc.get(symbol, 0.0))
            spread_corr = abs(spread_correlations_to_btc.get(symbol, 0.0))
            risk_penalty = self._clip01(
                0.45 * corr + 0.35 * spread_corr + 0.25 * max(0.0, float(z.get(FeatureKey.ROLLING_VOLATILITY) or 0.0))
            )

            raw_score = (
                w_volatility * vol_expansion_score
                + w_funding * funding_divergence_score
                + w_oi * oi_acceleration_score
                + w_regime * regime_alignment_score
                + w_liquidity * liquidity_score
                - w_risk_penalty * risk_penalty
                + ob_bonus
            )

            # Multi-timeframe convergence multiplier.
            conv = (convergence_scores or {}).get(symbol, 1.0)
            raw_score *= conv

            # BLOCK 4.1: Old adaptive_ml_weighting replaced by OnlineWeightOptimizer

            signal_power = vol_expansion_score + funding_divergence_score + oi_acceleration_score + regime_alignment_score
            if signal_power <= 1e-9:
                raw_score = 0.0

            # BLOCK 3: Signal time-decay
            import time
            now = time.time()

            # Track first-seen time
            if symbol not in self._first_seen:
                self._first_seen[symbol] = now

            # Time-decay: exponential decay with half-life
            age_seconds = now - self._first_seen[symbol]
            decay_factor = math.pow(0.5, age_seconds / self._signal_half_life_seconds)
            # Minimum decay floor: don't completely kill old signals
            decay_factor = max(0.15, decay_factor)

            # Apply decay to raw_score
            raw_score *= decay_factor

            # BLOCK 4.3: Directional bias with strength calculation
            funding_rate = float(v.get(FeatureKey.FUNDING_RATE) or 0.0)
            basis_bps = float(v.get(FeatureKey.BASIS_BPS) or 0.0)
            basis_slope = float(v.get(FeatureKey.BASIS_ACCELERATION) or 0.0)
            oi_delta_pct_val = float(v.get(FeatureKey.OI_DELTA_PCT) or 0.0)

            # Compute strength for each signal (0 to 1, signed)
            # Funding signal: strength proportional to |funding_rate| / 0.03 (3% is extreme)
            funding_signal = 0.0
            if funding_rate != 0:
                funding_signal = -1.0 * min(1.0, abs(funding_rate) / 0.03) * (1 if funding_rate > 0 else -1)
                # Negative funding (longs pay shorts) = bullish = positive signal for long

            # Funding deviation: crowding signal from mean-reversion model
            funding_deviation = float(v.get(FeatureKey.FUNDING_DEVIATION) or 0.0)
            if abs(funding_deviation) > 1.0:
                # Positive deviation (crowded longs) = bearish, negative (crowded shorts) = bullish
                funding_signal += -0.3 * min(1.0, abs(funding_deviation) / 3.0) * (1 if funding_deviation > 0 else -1)

            # Basis signal: strength proportional to |basis| / 20 bps
            basis_signal = 0.0
            if basis_bps != 0:
                basis_signal = -1.0 * min(1.0, abs(basis_bps) / 20.0) * (1 if basis_bps > 0 else -1)
                # Negative basis (backwardation) = bullish = positive signal for long

            # Slope signal: strength proportional to |slope| / 0.5
            slope_signal = 0.0
            if basis_slope != 0:
                slope_signal = -1.0 * min(1.0, abs(basis_slope) / 0.5) * (1 if basis_slope > 0 else -1)

            # BLOCK 4.3: Compute directional bias strength
            bias_strength = (funding_signal + basis_signal + slope_signal) / 3.0

            # Determine bias direction
            if bias_strength > 0.2:
                bias = "long"
            elif bias_strength < -0.2:
                bias = "short"
            else:
                bias = "neutral"

            directional_signals = {
                "funding_sign": "positive" if funding_rate > 0 else "negative" if funding_rate < 0 else "zero",
                "basis_direction": "contango" if basis_bps > 0 else "backwardation" if basis_bps < 0 else "flat",
                "oi_momentum": "rising" if oi_delta_pct_val > 0.5 else "falling" if oi_delta_pct_val < -0.5 else "stable",
                "funding_momentum": "accelerating" if float(v.get(FeatureKey.FUNDING_SLOPE) or 0.0) > 0.3 else "decelerating" if float(v.get(FeatureKey.FUNDING_SLOPE) or 0.0) < -0.3 else "stable",
            }

            reasons = [
                f"vol_exp={vol_expansion_score:.2f}",
                f"fund_div={funding_divergence_score:.2f}",
                f"oi_acc={oi_acceleration_score:.2f}",
                f"reg_align={regime_alignment_score:.2f}",
                f"liq={liquidity_score:.2f}",
                f"risk_penalty={risk_penalty:.2f}",
                f"bias={bias}",
            ]
            if ob_reason:
                reasons.append(ob_reason)

            # Order flow divergence warning
            flow_divergence = float(v.get(FeatureKey.FLOW_DELTA_DIVERGENCE) or 0.0)
            if flow_divergence >= 1.0:
                reasons.append("flow_divergence_warning")

            # Funding mean reversion signal
            funding_mr_signal = float(v.get(FeatureKey.FUNDING_MEAN_REVERSION_SIGNAL) or 0.0)
            if funding_mr_signal > 0.5:
                reasons.append("funding_mean_reversion")

            # NEW: Market impact warning (high cost to enter/exit)
            total_cost_bps = float(v.get(FeatureKey.MARKET_IMPACT_TOTAL_BPS) or 0.0)
            if total_cost_bps > 5.0:
                reasons.append(f"market_impact={total_cost_bps:.1f}bps")

            raw_rows.append(
                (
                    symbol,
                    reg,
                    raw_score,
                    risk_penalty,
                    {
                        "volatility_expansion_score": vol_expansion_score,
                        "funding_divergence_score": funding_divergence_score,
                        "oi_acceleration_score": oi_acceleration_score,
                        "regime_alignment_score": regime_alignment_score,
                        "liquidity_score": liquidity_score,
                        "risk_penalty": risk_penalty,
                        "orderbook_bonus": ob_bonus,
                        # BLOCK 3: Signal age and decay
                        "signal_age_seconds": age_seconds,
                        "decay_factor": decay_factor,
                    },
                    reasons,
                    bias,
                    bias_strength,  # BLOCK 4.3
                )
            )

        scores = [x[2] for x in raw_rows]
        score_min = min(scores) if scores else 0.0
        score_max = max(scores) if scores else 0.0
        denom = max(score_max - score_min, 1e-9)

        if len(raw_rows) > 1:
            ranked = sorted(raw_rows, key=lambda x: x[2], reverse=True)
            rank_index = {row[0]: idx for idx, row in enumerate(ranked)}
        else:
            rank_index = {}

        out: List[OpportunityScore] = []
        for symbol, reg, raw_score, risk_penalty, breakdown, reasons, bias, bias_strength in raw_rows:
            # Absolute signal quality gate (average of 4 core component scores).
            signal_quality = (
                breakdown["volatility_expansion_score"]
                + breakdown["funding_divergence_score"]
                + breakdown["oi_acceleration_score"]
                + breakdown["regime_alignment_score"]
            ) / 4.0

            if len(raw_rows) <= 1:
                # FIX: For single symbol, normalize raw_score with signal quality weighting
                # Use raw_score (which includes weights) but scale by signal_quality for consistency
                normalized_score = self._clip(0.0, 100.0, (0.6 * signal_quality + 0.4 * max(0.0, min(1.0, raw_score))) * 100.0)
            else:
                rank = rank_index[symbol]
                rank_scaled = 1.0 - (rank / max(1, len(raw_rows) - 1))
                minmax_scaled = (raw_score - score_min) / denom
                normalized_score = self._clip(
                    0.0, 100.0,
                    (0.4 * rank_scaled + 0.35 * minmax_scaled + 0.25 * signal_quality) * 100.0,
                )

            # BLOCK 4.2: Multi-level signal quality assessment
            if signal_quality > 0.4:
                signal_quality_level = "high"
                # No modification to score
            elif signal_quality >= 0.15:
                signal_quality_level = "medium"
                normalized_score *= 0.75
                reasons.append("moderate_signal")
            else:
                signal_quality_level = "low"
                normalized_score = max(normalized_score * 0.3, 5.0)
                reasons.append("low_absolute_signal")

            conf = self._clip(
                0.12,
                0.92,
                0.45 * reg.confidence + 0.20 * (1.0 - risk_penalty) + 0.35 * (normalized_score / 100.0),
            )
            # Penalize confidence for partial exchange data.
            dq_code = float(features[symbol].values.get(FeatureKey.DATA_QUALITY_CODE) or 0.0)
            if dq_code > 0.0:
                conf *= 0.80
            out.append(
                OpportunityScore(
                    symbol=symbol,
                    score=normalized_score,
                    confidence=conf,
                    regime=reg.regime,
                    reasons=reasons,
                    breakdown=breakdown,
                    directional_bias=bias,
                    signal_quality_level=signal_quality_level,  # BLOCK 4.2
                    directional_bias_strength=bias_strength,  # BLOCK 4.3
                )
            )

        out.sort(key=lambda x: (x.score, x.confidence), reverse=True)

        # BLOCK 3: Clean up expired symbols from first_seen tracking
        current_symbols = {row[0] for row in raw_rows}
        expired = [s for s in self._first_seen if s not in current_symbols]
        for s in expired:
            del self._first_seen[s]

        return out

    # BLOCK 4.4: Feedback loop for ML weight optimization
    def record_outcome(
        self,
        symbol: str,
        score: float,
        actual_outcome: float,
        timestamp: float,
        feature_vector: Dict[str, float] | None = None,
    ) -> None:
        """Record actual outcome for ML weight optimization.

        Args:
            symbol: Trading pair symbol
            score: Score that was computed
            actual_outcome: Actual outcome (e.g., spread change, PnL)
            timestamp: Unix timestamp
            feature_vector: Feature values used for scoring (optional if tracked separately)
        """
        if self._weight_optimizer and feature_vector:
            self._weight_optimizer.record(
                feature_vector=feature_vector,
                score=score,
                actual_outcome=actual_outcome,
                timestamp=timestamp,
            )

    @staticmethod
    def _clip(lo: float, hi: float, v: float) -> float:
        return max(lo, min(hi, v))

    @staticmethod
    def _clip01(v: float) -> float:
        return max(0.0, min(1.0, v))
