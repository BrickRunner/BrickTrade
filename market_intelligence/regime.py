from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, Deque, Dict, List

from market_intelligence.models import FeatureVector, MarketRegime, RegimeState

REGIME_MIN_CYCLES: Dict[MarketRegime, int] = {
    MarketRegime.PANIC: 1,
    MarketRegime.HIGH_VOLATILITY: 1,
    MarketRegime.OVERHEATED: 1,
    MarketRegime.TREND_UP: 2,
    MarketRegime.TREND_DOWN: 2,
    MarketRegime.RANGE: 3,
}


class RegimeModel:
    def __init__(
        self,
        confidence_threshold: float,
        min_duration_cycles: int,
        smoothing_alpha: float,
        ema_cross_coef: float = 1.2,
        adx_coef: float = 0.8,
        range_ema_coef: float = 1.1,
        range_adx_coef: float = 0.7,
        rsi_overheat_coef: float = 1.0,
        rsi_panic_coef: float = 1.0,
        vol_coef: float = 0.9,
        bb_coef: float = 0.8,
        interaction_strength: float = 1.0,
        blowoff_adx: float = 35.0,
        blowoff_rsi: float = 75.0,
        capitulation_adx: float = 30.0,
        capitulation_rsi: float = 25.0,
    ):
        self.confidence_threshold = confidence_threshold
        self.min_duration_cycles = min_duration_cycles
        self.smoothing_alpha = smoothing_alpha
        self.ema_cross_coef = ema_cross_coef
        self.adx_coef = adx_coef
        self.range_ema_coef = range_ema_coef
        self.range_adx_coef = range_adx_coef
        self.rsi_overheat_coef = rsi_overheat_coef
        self.rsi_panic_coef = rsi_panic_coef
        self.vol_coef = vol_coef
        self.bb_coef = bb_coef
        self.interaction_strength = interaction_strength
        self.blowoff_adx = blowoff_adx
        self.blowoff_rsi = blowoff_rsi
        self.capitulation_adx = capitulation_adx
        self.capitulation_rsi = capitulation_rsi
        self._stable_state: Dict[str, RegimeState] = {}
        self._smooth_probs: Dict[str, Dict[MarketRegime, float]] = {}
        self._history: Dict[str, Deque[MarketRegime]] = defaultdict(lambda: deque(maxlen=64))
        # BLOCK 3.2: Regime momentum tracking
        self._candidate_history: Dict[str, Deque[MarketRegime]] = defaultdict(lambda: deque(maxlen=5))
        self._consecutive_candidate_count: Dict[str, int] = {}

    def classify_global(self, btc_feature: FeatureVector) -> RegimeState:
        return self._classify("__global__", btc_feature)

    def classify_local(self, symbol: str, feature: FeatureVector, global_state: RegimeState) -> RegimeState:
        local = self._classify(symbol, feature)
        blended = dict(local.probabilities)
        blended[global_state.regime] = blended.get(global_state.regime, 0.0) + 0.12
        total = sum(blended.values())
        if total > 0:
            blended = {k: v / total for k, v in blended.items()}
        regime, conf = max(blended.items(), key=lambda x: x[1])
        conf = self._cap_confidence(conf, feature)
        return self._apply_stability(symbol, regime, conf, blended, feature=feature)

    def _classify(self, key: str, feature: FeatureVector) -> RegimeState:
        z = feature.normalized
        v = feature.values
        adx_v = float(v.get("adx") or 0.0)
        ema_cross_z = float(z.get("ema_cross") or 0.0)
        rsi_v = float(v.get("rsi") or 50.0)
        vol_z = float(z.get("rolling_volatility") or 0.0)
        bb_z = float(z.get("bb_width") or 0.0)
        funding_z = float(z.get("funding_rate") or 0.0)
        liq_z = float(z.get("liquidation_cluster") or 0.0)
        vol_trend_z = float(z.get("volume_trend") or 0.0)

        atr_pctile = float(v.get("atr_percentile") or 0.5)
        atr_pctile_bonus = 0.3 * max(0.0, (atr_pctile - 0.7) / 0.3)

        logits: Dict[MarketRegime, float] = {
            MarketRegime.TREND_UP: self.ema_cross_coef * max(0.0, ema_cross_z) + self.adx_coef * max(0.0, (adx_v - 18.0) / 25.0) + 0.2,
            MarketRegime.TREND_DOWN: self.ema_cross_coef * max(0.0, -ema_cross_z) + self.adx_coef * max(0.0, (adx_v - 18.0) / 25.0) + 0.2,
            MarketRegime.RANGE: self.range_ema_coef * max(0.0, 1.0 - abs(ema_cross_z)) + self.range_adx_coef * max(0.0, 1.0 - adx_v / 30.0) + 0.25,
            MarketRegime.OVERHEATED: self.rsi_overheat_coef * max(0.0, (rsi_v - 68.0) / 18.0) + 0.6 * max(0.0, funding_z) + 0.15,
            MarketRegime.PANIC: self.rsi_panic_coef * max(0.0, (32.0 - rsi_v) / 18.0) + 0.7 * max(0.0, liq_z) + 0.15,
            MarketRegime.HIGH_VOLATILITY: self.vol_coef * max(0.0, vol_z) * 0.6 + self.bb_coef * max(0.0, bb_z) * 0.4 + atr_pctile_bonus + 0.2,
        }

        # Nonlinear interaction terms
        s = self.interaction_strength
        # Blow-off top: strong trend + extreme RSI + expanding volatility
        if adx_v >= self.blowoff_adx and rsi_v >= self.blowoff_rsi and bb_z >= 1.0:
            logits[MarketRegime.OVERHEATED] += 0.5 * s
            logits[MarketRegime.TREND_UP] -= 0.2 * s
        # Capitulation: strong trend down + panic RSI + high liquidations
        if adx_v >= self.capitulation_adx and rsi_v <= self.capitulation_rsi and liq_z >= 1.0:
            logits[MarketRegime.PANIC] += 0.5 * s
            logits[MarketRegime.TREND_DOWN] -= 0.2 * s
        # Volatility squeeze -> breakout: low vol + narrow BB + low ADX
        if vol_z <= -0.5 and bb_z <= -0.5 and adx_v < 20:
            logits[MarketRegime.RANGE] += 0.3 * s
        # Divergence: trend + extreme funding in same direction = overheated/panic
        if ema_cross_z > 1.0 and funding_z > 0.5:
            logits[MarketRegime.OVERHEATED] += 0.3 * s
        if ema_cross_z < -1.0 and funding_z < -0.5:
            logits[MarketRegime.PANIC] += 0.3 * s
        # Counter-trend funding divergence: warns of potential reversal
        if ema_cross_z > 1.0 and funding_z < -0.8:
            logits[MarketRegime.RANGE] += 0.2 * s  # trend may be weakening
        if ema_cross_z < -1.0 and funding_z > 0.8:
            logits[MarketRegime.RANGE] += 0.2 * s  # downtrend may be weakening

        # Volume trend: rising volume confirms trend, falling confirms range.
        logits[MarketRegime.TREND_UP] += 0.15 * max(0.0, vol_trend_z)
        logits[MarketRegime.TREND_DOWN] += 0.15 * max(0.0, vol_trend_z)
        logits[MarketRegime.RANGE] += 0.1 * max(0.0, -vol_trend_z)

        # Market structure refinement
        mkt_struct = v.get("market_structure_code")
        if mkt_struct is not None:
            if mkt_struct == 1.0:  # bullish
                logits[MarketRegime.TREND_UP] += 0.15
                logits[MarketRegime.TREND_DOWN] -= 0.1
            elif mkt_struct == -1.0:  # bearish
                logits[MarketRegime.TREND_DOWN] += 0.15
                logits[MarketRegime.TREND_UP] -= 0.1
            elif mkt_struct == 0.0:  # transition
                logits[MarketRegime.RANGE] += 0.1

        # Orderbook pressure: significant imbalance reinforces trend direction
        ob_imb = v.get("orderbook_imbalance")
        if ob_imb is not None:
            ob_val = float(ob_imb)
            if ob_val > 0.2:
                logits[MarketRegime.TREND_UP] += 0.12 * min(1.0, ob_val / 0.5)
                logits[MarketRegime.TREND_DOWN] -= 0.06 * min(1.0, ob_val / 0.5)
            elif ob_val < -0.2:
                logits[MarketRegime.TREND_DOWN] += 0.12 * min(1.0, abs(ob_val) / 0.5)
                logits[MarketRegime.TREND_UP] -= 0.06 * min(1.0, abs(ob_val) / 0.5)

        # Funding acceleration: rapidly changing funding warns of regime shift
        funding_slope_v = float(v.get("funding_slope") or 0.0)
        if abs(funding_slope_v) > 0.5:
            logits[MarketRegime.HIGH_VOLATILITY] += 0.15 * min(1.0, abs(funding_slope_v))

        probs = self._softmax(logits)
        regime, conf = max(probs.items(), key=lambda x: x[1])
        conf = self._cap_confidence(conf, feature)
        return self._apply_stability(key, regime, conf, probs, feature=feature)

    def _is_extreme(self, feature: FeatureVector | None) -> bool:
        """Detect extreme market conditions that should bypass stability delay."""
        if feature is None:
            return False
        v = feature.values
        rsi_v = float(v.get("rsi") or 50.0)
        vol_spike_v = float(v.get("volume_spike") or 1.0)
        vol_z = float(v.get("rolling_volatility") or 0.0)

        if rsi_v <= 15.0 or rsi_v >= 85.0:
            return True
        if vol_spike_v >= 4.0:
            return True
        if abs(vol_z) >= 3.0:
            return True
        # Liquidation cascade
        liq_z = float(feature.normalized.get("liquidation_cluster") or 0.0)
        if liq_z >= 2.5:
            return True
        # Funding extreme
        funding_z = float(feature.normalized.get("funding_rate") or 0.0)
        if abs(funding_z) >= 3.0:
            return True
        return False

    def _apply_stability(
        self,
        key: str,
        regime: MarketRegime,
        confidence: float,
        probabilities: Dict[MarketRegime, float],
        feature: FeatureVector | None = None,
    ) -> RegimeState:
        prev_probs = self._smooth_probs.get(key)
        if prev_probs:
            smoothed = {
                k: self.smoothing_alpha * probabilities.get(k, 0.0) + (1.0 - self.smoothing_alpha) * prev_probs.get(k, 0.0)
                for k in probabilities
            }
            s_total = sum(smoothed.values())
            if s_total > 0:
                smoothed = {k: v / s_total for k, v in smoothed.items()}
        else:
            smoothed = dict(probabilities)
        self._smooth_probs[key] = smoothed

        candidate_regime, candidate_conf = max(smoothed.items(), key=lambda x: x[1])
        candidate_conf = self._cap_confidence(candidate_conf)

        # BLOCK 3.1: Compute transition probability
        # When no regime dominates (max confidence < threshold), we're in transition
        transition_probability = 1.0 - candidate_conf

        prev_state = self._stable_state.get(key)

        if prev_state is None:
            state = RegimeState(
                candidate_regime,
                candidate_conf,
                smoothed,
                stable_for_cycles=1,
                transition_probability=transition_probability
            )
            self._stable_state[key] = state
            self._history[key].append(candidate_regime)
            # BLOCK 3.2: Initialize candidate tracking
            self._candidate_history[key].append(candidate_regime)
            self._consecutive_candidate_count[key] = 1
            return state

        # BLOCK 3.2: Track candidate regime momentum
        self._candidate_history[key].append(candidate_regime)

        # Count consecutive occurrences of same candidate
        if len(self._candidate_history[key]) >= 2:
            if all(r == candidate_regime for r in list(self._candidate_history[key])[-3:]):
                # Same candidate for last 3 cycles - strong momentum
                self._consecutive_candidate_count[key] = 3
            elif all(r == candidate_regime for r in list(self._candidate_history[key])[-2:]):
                self._consecutive_candidate_count[key] = 2
            else:
                self._consecutive_candidate_count[key] = 1
        else:
            self._consecutive_candidate_count[key] = 1

        stable_cycles = prev_state.stable_for_cycles + 1 if prev_state.regime == candidate_regime else 1
        required_cycles = REGIME_MIN_CYCLES.get(candidate_regime, self.min_duration_cycles)

        # BLOCK 3.2: Regime momentum - reduce required cycles if strong momentum
        consecutive = self._consecutive_candidate_count.get(key, 1)
        if consecutive >= 3 and prev_state.regime != candidate_regime:
            # Strong momentum - reduce required cycles by 1 (but min 1)
            required_cycles = max(1, required_cycles - 1)

        # Fast-path: bypass stability delay during extreme conditions
        extreme = self._is_extreme(feature)
        if extreme and candidate_conf >= self.confidence_threshold:
            accept_change = True
        else:
            accept_change = candidate_conf >= self.confidence_threshold and stable_cycles >= required_cycles

        if prev_state.regime != candidate_regime and not accept_change:
            kept_conf = self._cap_confidence(float(smoothed.get(prev_state.regime, prev_state.confidence)))
            kept = RegimeState(
                prev_state.regime,
                kept_conf,
                smoothed,
                prev_state.stable_for_cycles + 1,
                transition_probability=transition_probability
            )
            self._stable_state[key] = kept
            self._history[key].append(kept.regime)
            return kept

        state = RegimeState(
            candidate_regime,
            candidate_conf,
            smoothed,
            stable_for_cycles=stable_cycles,
            transition_probability=transition_probability
        )
        self._stable_state[key] = state
        self._history[key].append(state.regime)
        return state

    @staticmethod
    def _softmax(logits: Dict[MarketRegime, float]) -> Dict[MarketRegime, float]:
        if not logits:
            return {r: 1.0 / 6.0 for r in MarketRegime}
        max_logit = max(logits.values())
        exp_vals: Dict[MarketRegime, float] = {}
        for k in MarketRegime:
            x = logits.get(k, 0.0)
            ex = math.exp(max(-30.0, min(30.0, x - max_logit)))
            exp_vals[k] = ex
        total = sum(exp_vals.values())
        if total <= 1e-12:
            return {r: 1.0 / len(MarketRegime) for r in MarketRegime}
        return {k: v / total for k, v in exp_vals.items()}

    @staticmethod
    def _cap_confidence(conf: float, feature: FeatureVector | None = None) -> float:
        # Avoid unrealistically perfect confidence; allow higher values only in true extremes.
        hard_cap = 0.92
        if feature is not None:
            adx_v = float(feature.values.get("adx") or 0.0)
            rsi_v = float(feature.values.get("rsi") or 50.0)
            vol_z = float(feature.normalized.get("rolling_volatility") or 0.0)
            extreme = adx_v >= 45 and (rsi_v >= 78 or rsi_v <= 22) and abs(vol_z) >= 2.0
            hard_cap = 0.97 if extreme else 0.92
        return max(0.0, min(hard_cap, conf))

    def assess_timeframe_convergence(self, feature: FeatureVector) -> float:
        """Score multi-timeframe convergence (0.7 = divergent, 1.15 = strongly convergent).

        Compares local-timeframe signals with higher-timeframe features (4H, 1D)
        when available. Returns 1.0 when no MTF data exists.
        """
        v = feature.values
        local_ema = v.get("ema_cross")
        local_rsi = v.get("rsi")

        mtf_checks: list[float] = []
        for tf in ("4H", "1D"):
            htf_ema = v.get(f"ema_cross_{tf}")
            htf_rsi = v.get(f"rsi_{tf}")
            htf_adx = v.get(f"adx_{tf}")
            if htf_ema is None or htf_rsi is None:
                continue

            # Trend direction agreement
            if local_ema is not None:
                ema_agree = 1.0 if (local_ema > 0) == (htf_ema > 0) else -1.0
            else:
                ema_agree = 0.0

            # RSI zone agreement (both bullish, both bearish, or mixed)
            if local_rsi is not None:
                local_bull = local_rsi > 55
                local_bear = local_rsi < 45
                htf_bull = htf_rsi > 55
                htf_bear = htf_rsi < 45
                if (local_bull and htf_bull) or (local_bear and htf_bear):
                    rsi_agree = 1.0
                elif (local_bull and htf_bear) or (local_bear and htf_bull):
                    rsi_agree = -1.0
                else:
                    rsi_agree = 0.0
            else:
                rsi_agree = 0.0

            # ADX strength bonus (strong HTF trend adds conviction)
            adx_bonus = 0.0
            if htf_adx is not None and htf_adx >= 25:
                adx_bonus = 0.15

            tf_score = 0.5 * ema_agree + 0.35 * rsi_agree + adx_bonus
            mtf_checks.append(tf_score)

        if not mtf_checks:
            return 1.0

        avg = sum(mtf_checks) / len(mtf_checks)
        # Map [-1, 1] → [0.7, 1.15]
        return max(0.7, min(1.15, 1.0 + avg * 0.15))

    def history(self, key: str) -> List[MarketRegime]:
        return list(self._history.get(key, []))

    def regime_distribution(self, key: str, window: int = 64) -> Dict[MarketRegime, float]:
        """BLOCK 3.3: Get historical regime distribution over last N cycles.

        Args:
            key: Symbol or "__global__" for global regime
            window: Number of cycles to analyze (default 64)

        Returns:
            Dictionary mapping each regime to its proportion in [0, 1]
        """
        hist = list(self._history.get(key, []))
        if not hist:
            # Return uniform distribution if no history
            return {r: 1.0 / len(MarketRegime) for r in MarketRegime}

        # Take last window cycles
        recent = hist[-window:] if len(hist) > window else hist

        # Count occurrences
        counts: Dict[MarketRegime, int] = {r: 0 for r in MarketRegime}
        for regime in recent:
            counts[regime] += 1

        # Convert to proportions
        total = len(recent)
        return {r: count / total for r, count in counts.items()}

    # BLOCK 6.1: State persistence
    def save_state(self, path: Path) -> None:
        """Save regime model state to JSON file for persistence across restarts."""
        state: Dict[str, Any] = {
            "_stable_state": {
                k: {
                    "regime": v.regime.value,
                    "confidence": v.confidence,
                    "probabilities": {r.value: p for r, p in v.probabilities.items()},
                    "stable_for_cycles": v.stable_for_cycles,
                    "transition_probability": v.transition_probability,
                    "warm_start": v.warm_start,
                }
                for k, v in self._stable_state.items()
            },
            "_smooth_probs": {
                k: {r.value: p for r, p in probs.items()}
                for k, probs in self._smooth_probs.items()
            },
            "_history": {k: [r.value for r in list(deq)] for k, deq in self._history.items()},
            "_candidate_history": {k: [r.value for r in list(deq)] for k, deq in self._candidate_history.items()},
            "_consecutive_candidate_count": dict(self._consecutive_candidate_count),
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)

    def load_state(self, path: Path) -> bool:
        """Load regime model state from JSON file. Returns True if successful."""
        try:
            if not path.exists():
                return False

            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)

            # Restore _stable_state
            self._stable_state = {
                k: RegimeState(
                    regime=MarketRegime(v["regime"]),
                    confidence=v["confidence"],
                    probabilities={MarketRegime(r): p for r, p in v["probabilities"].items()},
                    stable_for_cycles=v["stable_for_cycles"],
                    transition_probability=v.get("transition_probability", 0.0),
                    warm_start=v.get("warm_start", True),  # Mark as warm start
                )
                for k, v in state.get("_stable_state", {}).items()
            }

            # Restore _smooth_probs
            self._smooth_probs = {
                k: {MarketRegime(r): p for r, p in probs.items()}
                for k, probs in state.get("_smooth_probs", {}).items()
            }

            # Restore _history
            self._history = defaultdict(lambda: deque(maxlen=64))
            for k, regimes in state.get("_history", {}).items():
                self._history[k] = deque([MarketRegime(r) for r in regimes], maxlen=64)

            # Restore _candidate_history
            self._candidate_history = defaultdict(lambda: deque(maxlen=5))
            for k, regimes in state.get("_candidate_history", {}).items():
                self._candidate_history[k] = deque([MarketRegime(r) for r in regimes], maxlen=5)

            # Restore _consecutive_candidate_count
            self._consecutive_candidate_count = state.get("_consecutive_candidate_count", {})

            return True
        except Exception:
            return False
