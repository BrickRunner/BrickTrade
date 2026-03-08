from __future__ import annotations

from collections import defaultdict
from typing import Dict, List, Optional

from market_intelligence.models import DataHealthStatus, FeatureVector, MarketRegime, OpportunityScore, PortfolioRiskSignal, RegimeState


class PortfolioAnalyzer:
    def analyze(
        self,
        opportunities: List[OpportunityScore],
        local_regimes: Dict[str, RegimeState],
        correlations_to_btc: Dict[str, float],
        global_regime: RegimeState,
        global_atr_pct: float | None,
        global_volatility_regime: str,
        data_health_status: DataHealthStatus,
        scoring_enabled: bool,
        min_score_threshold: float = 20.0,
        # BLOCK 3.3: Historical regime distribution
        regime_distribution: Dict[MarketRegime, float] | None = None,
        # BLOCK 5.1: Tail risk protection (requires raw volatility data)
        features: Optional[Dict[str, FeatureVector]] = None,
        # BLOCK 5.2: Drawdown-aware allocation
        current_portfolio_drawdown_pct: float = 0.0,
    ) -> PortfolioRiskSignal:
        # Hard lock: no synthetic allocations when scoring is disabled.
        if not scoring_enabled:
            defensive_mult = self._defensive_multiplier(global_regime, global_volatility_regime, data_health_status)
            return PortfolioRiskSignal(
                capital_allocation_pct={},
                exposure_by_regime={},
                dynamic_risk_multiplier={},
                risk_multiplier=defensive_mult,
                reduced_activity=True,
                min_score_threshold=min_score_threshold,
                aggressive_mode_enabled=False,
                recommendation="Defensive mode due to incomplete validated data.",
                defensive_mode=True,
                recommended_exposure_cap_pct=20.0,
            )

        if not opportunities:
            return PortfolioRiskSignal(
                capital_allocation_pct={},
                exposure_by_regime={},
                dynamic_risk_multiplier={},
                risk_multiplier=0.25,
                reduced_activity=True,
                min_score_threshold=min_score_threshold,
                aggressive_mode_enabled=False,
                recommendation="No actionable opportunities.",
                defensive_mode=True,
                recommended_exposure_cap_pct=20.0,
            )

        avg_abs_corr = sum(abs(correlations_to_btc.get(x.symbol, 0.0)) for x in opportunities) / max(1, len(opportunities))

        conf_factor = max(0.20, min(1.0, global_regime.confidence))

        # BLOCK 3.1: Transition probability factor
        # When transition_probability > 0.4, reduce risk significantly
        transition_factor = 1.0
        if global_regime.transition_probability > 0.4:
            transition_factor = 0.7  # Reduce risk by 30% during transition
        elif global_regime.transition_probability > 0.3:
            transition_factor = 0.85  # Reduce risk by 15% during moderate transition

        if global_volatility_regime == "low":
            vol_factor = 0.55
        elif global_volatility_regime == "medium":
            vol_factor = 0.85
        elif global_volatility_regime == "high":
            vol_factor = 0.65
        else:
            vol_factor = 0.30

        atr_value = float(global_atr_pct or 0.0)
        atr_penalty = min(0.45, max(0.0, atr_value / 6.0))
        stability_factor = min(1.0, 0.35 + 0.15 * max(0, global_regime.stable_for_cycles))
        corr_factor = max(0.35, 1.0 - 0.6 * avg_abs_corr)

        base_risk = conf_factor * vol_factor * stability_factor * corr_factor * (1.0 - atr_penalty) * transition_factor

        # BLOCK 3.3: Historical regime distribution adjustment
        # If >60% time in stressful regimes, reduce risk multiplier by additional 20%
        if regime_distribution:
            stress_regimes = {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY, MarketRegime.OVERHEATED}
            stress_pct = sum(regime_distribution.get(r, 0.0) for r in stress_regimes)
            if stress_pct > 0.6:
                base_risk *= 0.8  # Additional 20% reduction

        # BLOCK 5.2: Drawdown-aware allocation
        # If portfolio is in drawdown > 10%, reduce risk proportionally
        if current_portfolio_drawdown_pct > 10.0:
            drawdown_penalty = 1.0 - 0.02 * current_portfolio_drawdown_pct
            base_risk *= max(0.5, drawdown_penalty)  # Cap at 50% of base risk

        risk_multiplier = max(0.05, min(1.0, base_risk))

        top = opportunities[: min(len(opportunities), 12)]
        avg_score = sum(x.score for x in top) / max(1, len(top))
        reduced_activity = avg_score < min_score_threshold

        aggressive_mode_enabled = True
        if global_regime.stable_for_cycles < 3 or data_health_status != DataHealthStatus.OK or global_volatility_regime == "unavailable":
            aggressive_mode_enabled = False
            risk_multiplier = min(risk_multiplier, 0.25)

        if reduced_activity:
            risk_multiplier = min(risk_multiplier, 0.49)

        allocation: Dict[str, float] = {}
        symbol_mult: Dict[str, float] = {}

        ranked = sorted(top, key=lambda x: (x.score, x.confidence), reverse=True)
        n = len(ranked)
        rank_weights = {x.symbol: float(n - idx) for idx, x in enumerate(ranked)}
        weight_sum = sum(rank_weights.values()) or 1.0

        for item in ranked:
            base = rank_weights[item.symbol] / weight_sum
            corr = abs(correlations_to_btc.get(item.symbol, 0.0))
            corr_mult = max(0.25, 1.0 - 0.55 * corr)

            reg = local_regimes[item.symbol].regime
            reg_mult = 1.0
            if reg in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
                reg_mult = 0.45
            elif reg == MarketRegime.OVERHEATED:
                reg_mult = 0.65
            elif reg == MarketRegime.RANGE:
                reg_mult = 0.85

            local_mult = max(0.10, min(1.0, corr_mult * reg_mult * risk_multiplier))
            symbol_mult[item.symbol] = local_mult
            allocation[item.symbol] = base * local_mult

        total = sum(allocation.values())
        if total > 0:
            for s in list(allocation):
                allocation[s] = 100.0 * allocation[s] / total

        # BLOCK 5.1: Tail risk protection using VaR (95th percentile)
        if features:
            volatilities = []
            for sym in allocation:
                if sym in features and features[sym].normalized.get("rolling_volatility") is not None:
                    vol = float(features[sym].normalized.get("rolling_volatility"))
                    volatilities.append(vol)

            if len(volatilities) >= 3:  # Need at least 3 samples for percentile
                volatilities_sorted = sorted(volatilities)
                var_95_index = int(0.95 * len(volatilities_sorted))
                var_95 = volatilities_sorted[var_95_index]

                # Apply tail risk penalty to symbols with extreme volatility
                for sym in list(allocation):
                    if sym in features and features[sym].normalized.get("rolling_volatility") is not None:
                        vol = float(features[sym].normalized.get("rolling_volatility"))
                        if vol > var_95 and var_95 > 0:
                            penalty_factor = (vol - var_95) / var_95
                            tail_penalty = 1.0 - 0.2 * penalty_factor
                            tail_penalty = max(0.6, tail_penalty)  # Cap at 40% reduction
                            allocation[sym] *= tail_penalty

                # Renormalize after tail risk adjustment
                total_after_tail = sum(allocation.values())
                if total_after_tail > 0:
                    for s in allocation:
                        allocation[s] = 100.0 * allocation[s] / total_after_tail

        # Cap individual allocation at 25% (iterate to handle redistribution overflow)
        MAX_SINGLE_PAIR_PCT = 25.0
        for _ in range(5):  # max iterations to converge
            capped = False
            for s in list(allocation):
                if allocation[s] > MAX_SINGLE_PAIR_PCT:
                    allocation[s] = MAX_SINGLE_PAIR_PCT
                    capped = True
            if not capped:
                break
            total_after_cap = sum(allocation.values())
            if total_after_cap > 0 and abs(total_after_cap - 100.0) > 0.01:
                uncapped = {s: v for s, v in allocation.items() if v < MAX_SINGLE_PAIR_PCT}
                uncapped_total = sum(uncapped.values())
                if uncapped_total > 0:
                    excess = 100.0 - total_after_cap
                    for s in uncapped:
                        allocation[s] += excess * (uncapped[s] / uncapped_total)
                else:
                    break

        # Correlation-aware concentration penalty
        corr_threshold = 0.8
        high_corr_pairs = [s for s in allocation if abs(correlations_to_btc.get(s, 0.0)) > corr_threshold]
        if len(high_corr_pairs) > 2:
            sorted_hc = sorted(high_corr_pairs, key=lambda s: allocation[s], reverse=True)
            for s in sorted_hc[2:]:
                allocation[s] *= 0.7
            total_rebalanced = sum(allocation.values())
            if total_rebalanced > 0:
                for s in allocation:
                    allocation[s] = 100.0 * allocation[s] / total_rebalanced
            # Re-apply 25% cap after correlation rebalance
            for s in list(allocation):
                if allocation[s] > MAX_SINGLE_PAIR_PCT:
                    allocation[s] = MAX_SINGLE_PAIR_PCT

        # BLOCK 5.3: Sector concentration (base currency)
        # Extract base currency and check if any base dominates > 40%
        base_allocation = defaultdict(float)
        symbol_to_base = {}
        for sym in allocation:
            # Extract base currency (everything before USDT, USDC, USD, etc.)
            base = self._extract_base_currency(sym)
            symbol_to_base[sym] = base
            base_allocation[base] += allocation[sym]

        # Apply penalty to over-concentrated bases
        for base, base_pct in base_allocation.items():
            if base_pct > 40.0:
                penalty = 0.8  # Reduce by 20%
                for sym in allocation:
                    if symbol_to_base.get(sym) == base:
                        allocation[sym] *= penalty

        # Renormalize after sector concentration adjustment
        total_after_sector = sum(allocation.values())
        if total_after_sector > 0:
            for s in allocation:
                allocation[s] = 100.0 * allocation[s] / total_after_sector

        # Re-apply 25% cap after all adjustments to ensure hard limit (iterative approach)
        for _ in range(5):  # max iterations to converge
            capped = False
            for s in list(allocation):
                if allocation[s] > MAX_SINGLE_PAIR_PCT:
                    allocation[s] = MAX_SINGLE_PAIR_PCT
                    capped = True
            if not capped:
                break
            final_total = sum(allocation.values())
            if final_total > 0 and abs(final_total - 100.0) > 0.01:
                uncapped = {s: v for s, v in allocation.items() if v < MAX_SINGLE_PAIR_PCT}
                uncapped_total = sum(uncapped.values())
                if uncapped_total > 0:
                    excess = 100.0 - final_total
                    for s in uncapped:
                        allocation[s] += excess * (uncapped[s] / uncapped_total)
                else:
                    break

        exposure = defaultdict(float)
        for s, pct in allocation.items():
            exposure[local_regimes[s].regime] += pct

        if reduced_activity:
            recommendation = "Low opportunity environment. Reduce activity."
        elif aggressive_mode_enabled:
            recommendation = "Controlled risk-on mode."
        else:
            recommendation = "Balanced risk mode."

        # Dynamic exposure cap
        if global_regime.regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
            exposure_cap = 15.0
        elif global_regime.regime == MarketRegime.OVERHEATED:
            exposure_cap = 30.0
        elif not aggressive_mode_enabled:
            exposure_cap = 50.0
        else:
            exposure_cap = 100.0

        # Further reduce if data health is degraded
        if data_health_status == DataHealthStatus.PARTIAL:
            exposure_cap = min(exposure_cap, 40.0)
        elif data_health_status == DataHealthStatus.INVALID:
            exposure_cap = 10.0

        return PortfolioRiskSignal(
            capital_allocation_pct=allocation,
            exposure_by_regime=dict(exposure),
            dynamic_risk_multiplier=symbol_mult,
            risk_multiplier=risk_multiplier,
            reduced_activity=reduced_activity,
            min_score_threshold=min_score_threshold,
            aggressive_mode_enabled=aggressive_mode_enabled,
            recommendation=recommendation,
            defensive_mode=not aggressive_mode_enabled,
            recommended_exposure_cap_pct=exposure_cap,
        )

    @staticmethod
    def _defensive_multiplier(
        global_regime: RegimeState,
        global_volatility_regime: str,
        data_health_status: DataHealthStatus,
    ) -> float:
        base = 0.25
        if data_health_status != DataHealthStatus.OK:
            base *= 0.6
        if global_regime.stable_for_cycles < 3:
            base *= 0.7
        if global_volatility_regime == "unavailable":
            base *= 0.6
        return max(0.05, min(0.25, base))

    @staticmethod
    def _extract_base_currency(symbol: str) -> str:
        """Extract base currency from trading pair symbol.

        Examples:
            ETHUSDT -> ETH
            BTCUSDC -> BTC
            SOLUSDT -> SOL
        """
        # Common quote currencies to strip
        quote_currencies = ["USDT", "USDC", "USD", "BUSD", "TUSD", "DAI"]
        symbol_upper = symbol.upper()

        for quote in quote_currencies:
            if symbol_upper.endswith(quote):
                return symbol_upper[: -len(quote)]

        # Fallback: assume last 3-4 chars are quote
        if len(symbol) > 4:
            return symbol_upper[:-4]
        return symbol_upper
