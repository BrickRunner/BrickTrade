from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from dataclasses import dataclass, field

from market_intelligence.collector import MarketDataCollector
from market_intelligence.config import MarketIntelligenceConfig
from market_intelligence.feature_engine import FeatureEngine
from market_intelligence import persistence
from market_intelligence.logger import JsonlLogger
from market_intelligence.models import (
    DataHealthStatus,
    FeatureVector,
    MarketIntelligenceReport,
    MarketRegime,
    OHLCV,
    OpportunityScore,
    PortfolioRiskSignal,
    RegimeState,
)
from market_intelligence.portfolio import PortfolioAnalyzer
from market_intelligence.regime import RegimeModel
from market_intelligence.scorer import OpportunityScorer
# BLOCK 2.2: Use robust correlation instead of Pearson
from market_intelligence.statistics import robust_corr, rolling_returns
from market_intelligence.validation import DataValidator, ValidationResult

logger = logging.getLogger("market_intelligence")


@dataclass
class PipelineResult:
    features: Dict[str, FeatureVector]
    validation: ValidationResult
    btc_symbol: str
    global_regime: RegimeState
    local_regimes: Dict[str, RegimeState]
    price_corr: Dict[str, float]
    spread_corr: Dict[str, float]
    price_corr_stress: Dict[str, float]
    spread_corr_stress: Dict[str, float]
    convergence_scores: Dict[str, float]
    scoring_enabled: bool
    opportunities: List[OpportunityScore]
    null_score_symbols: List[str]
    btc_metrics: Dict[str, float | str | None]


class MarketIntelligenceEngine:
    def __init__(self, config: MarketIntelligenceConfig, collector: MarketDataCollector):
        self.config = config
        self.collector = collector
        self.feature_engine = FeatureEngine(zscore_window=config.zscore_window)
        self.regime_model = RegimeModel(
            confidence_threshold=config.confidence_threshold,
            min_duration_cycles=config.min_regime_duration_cycles,
            smoothing_alpha=config.smoothing_alpha,
            ema_cross_coef=config.regime_ema_cross_coef,
            adx_coef=config.regime_adx_coef,
            range_ema_coef=config.regime_range_ema_coef,
            range_adx_coef=config.regime_range_adx_coef,
            rsi_overheat_coef=config.regime_rsi_overheat_coef,
            rsi_panic_coef=config.regime_rsi_panic_coef,
            vol_coef=config.regime_vol_coef,
            bb_coef=config.regime_bb_coef,
            interaction_strength=config.regime_interaction_strength,
            blowoff_adx=config.regime_blowoff_adx,
            blowoff_rsi=config.regime_blowoff_rsi,
            capitulation_adx=config.regime_capitulation_adx,
            capitulation_rsi=config.regime_capitulation_rsi,
        )
        self.scorer = OpportunityScorer(
            adaptive_ml_weighting=config.adaptive_ml_weighting,
            w_volatility=config.score_weight_volatility,
            w_funding=config.score_weight_funding,
            w_oi=config.score_weight_oi,
            w_regime=config.score_weight_regime,
            w_risk_penalty=config.score_weight_risk_penalty,
            w_liquidity=config.score_weight_liquidity,
        )
        self.portfolio = PortfolioAnalyzer()
        self.validator = DataValidator()
        self.logger = JsonlLogger(config.log_dir, config.jsonl_file_name)
        self._previous_payload: Optional[Dict] = None
        self._snapshot_file = Path(config.log_dir) / "market_intelligence_prev_snapshot.json"
        self._previous_loaded = False
        self._cycle_count = 0
        self._state_restored = False
        # BLOCK 2.3: MTF candles caching
        self._last_candles_fetch: float = 0.0
        self._candles_cache: Dict[str, Dict[str, List[OHLCV]]] = {}
        self._candles_refresh_interval: float = 1800.0  # 30 minutes

    async def run_once(self) -> MarketIntelligenceReport:
        await self._ensure_previous_loaded()
        await self._restore_persisted_state()

        symbols = self._select_symbols()
        # Determine if previous cycle was stress for adaptive slow data.
        prev_regime_name = (self._previous_payload or {}).get("global", {}).get("regime", {}).get("name", "")
        is_stress = prev_regime_name in {"panic", "high_volatility"}

        snapshots, warnings = await self.collector.collect(symbols, is_stress=is_stress)
        if not snapshots:
            payload = {
                "timestamp": time.time(),
                "status": "degraded",
                "warnings": warnings + ["no_snapshots"],
                "symbols_requested": symbols,
            }
            await self.logger.log(payload)
            raise RuntimeError("No market snapshots available")

        histories = self._histories_for_features(snapshots.keys())

        # BLOCK 2.3: Fetch OHLCV candles with caching (first run + every 30 min)
        now = time.time()
        if (now - self._last_candles_fetch) >= self._candles_refresh_interval or not self._candles_cache:
            try:
                mtf_timeframes = ["1H", "4H", "1D"]  # Always include HTF for convergence analysis
                candles = await self.collector.collect_candles(list(symbols), timeframes=mtf_timeframes)
                self._candles_cache = candles
                self._last_candles_fetch = now
                logger.info("MTF candles refreshed for %d symbols, timeframes: %s", len(symbols), mtf_timeframes)
            except Exception as e:
                logger.warning("OHLCV candle fetch failed: %s", e)
                # Use cached candles if available
                candles = self._candles_cache
        else:
            # Use cached candles
            candles = self._candles_cache

        # Offload CPU-bound feature/regime/scoring pipeline to a thread
        # to avoid blocking the event loop.
        p = await asyncio.to_thread(self._compute_pipeline, snapshots, histories, candles)

        portfolio = self.portfolio.analyze(
            p.opportunities,
            p.local_regimes,
            p.price_corr,
            p.global_regime,
            global_atr_pct=p.btc_metrics.get("atr_pct"),
            global_volatility_regime=str(p.btc_metrics.get("volatility_regime") or "unavailable"),
            data_health_status=p.validation.status,
            scoring_enabled=p.scoring_enabled,
            min_score_threshold=self.config.min_opportunity_score,
        )

        alerts = self._detect_alerts(p.features, p.local_regimes)
        local_context = self._build_local_context(p.features[p.btc_symbol])
        deltas = self._build_dynamic_deltas(p.global_regime, p.btc_metrics)

        payload = {
            "timestamp": time.time(),
            "timestamp_msk": datetime.now(ZoneInfo("Europe/Moscow")).strftime("%d.%m.%Y %H:%M:%S"),
            "status": "ok",
            "timeframes": {
                "global": self.config.global_timeframe,
                "local": self.config.local_timeframe,
            },
            "data_health": p.validation.status.value,
            "data_health_diagnostics": {
                "status": p.validation.status.value,
                "warnings": p.validation.warnings,
            },
            "scoring_enabled": p.scoring_enabled,
            "warnings": warnings,
            "scoring_diagnostics": {
                "null_score_symbols": p.null_score_symbols,
            },
            "global": {
                "symbol": p.btc_symbol,
                "regime": {
                    "name": p.global_regime.regime.value,
                    "confidence": p.global_regime.confidence,
                    "stable_for_cycles": p.global_regime.stable_for_cycles,
                    "probabilities": {k.value: v for k, v in p.global_regime.probabilities.items()},
                },
                "metrics": p.btc_metrics,
            },
            "local": {
                "symbol": p.btc_symbol,
                "context": local_context,
            },
            "convergence_scores": {s: v for s, v in p.convergence_scores.items()},
            "local_regimes": {
                s: {
                    "regime": x.regime.value,
                    "confidence": x.confidence,
                    "stable_for_cycles": x.stable_for_cycles,
                    "probabilities": {k.value: v for k, v in x.probabilities.items()},
                }
                for s, x in p.local_regimes.items()
            },
            "features": {s: p.features[s].values for s in p.features},
            "features_normalized": {s: p.features[s].normalized for s in p.features},
            "opportunities": [
                {
                    "symbol": o.symbol,
                    "score": o.score,
                    "confidence": o.confidence,
                    "regime": o.regime.value,
                    "reasons": o.reasons,
                    "breakdown": o.breakdown,
                    "directional_bias": o.directional_bias,
                }
                for o in p.opportunities
            ],
            "portfolio_risk": {
                "capital_allocation_pct": portfolio.capital_allocation_pct,
                "exposure_by_regime": {k.value: v for k, v in portfolio.exposure_by_regime.items()},
                "dynamic_risk_multiplier": portfolio.dynamic_risk_multiplier,
                "risk_multiplier": portfolio.risk_multiplier,
                "reduced_activity": portfolio.reduced_activity,
                "aggressive_mode_enabled": portfolio.aggressive_mode_enabled,
                "defensive_mode": portfolio.defensive_mode,
                "recommended_exposure_cap_pct": portfolio.recommended_exposure_cap_pct,
                "recommendation": portfolio.recommendation,
                "min_score_threshold": portfolio.min_score_threshold,
            },
            "correlations": {
                "price_to_btc": p.price_corr,
                "spread_to_btc": p.spread_corr,
                "price_to_btc_stress": p.price_corr_stress,
                "spread_to_btc_stress": p.spread_corr_stress,
                "min_samples": min(
                    (len(self.collector.state.prices[s]) for s in p.features),
                    default=0,
                ),
            },
            "dynamic_deltas": deltas,
            "extreme_alerts": alerts,
        }

        # Keep a dedicated warning for complete feature invalidity.
        if p.validation.status == DataHealthStatus.INVALID:
            payload["warnings"] = payload.get("warnings", []) + ["Feature engine data incomplete."]
        self._assert_consistency(payload)

        report = MarketIntelligenceReport(
            timestamp=payload["timestamp"],
            global_timeframe=self.config.global_timeframe,
            local_timeframe=self.config.local_timeframe,
            scoring_enabled=p.scoring_enabled,
            data_health_status=p.validation.status,
            data_health_warnings=p.validation.warnings,
            global_regime=p.global_regime,
            local_regimes=p.local_regimes,
            opportunities=p.opportunities,
            portfolio_risk=portfolio,
            extreme_alerts=alerts,
            dynamic_deltas=deltas,
            payload=payload,
        )
        await self.logger.log(payload)
        self._previous_payload = payload
        await self._save_previous_snapshot(payload)

        self._cycle_count += 1
        if self.config.persist_enabled and self._cycle_count % self.config.persist_every_n_cycles == 0:
            await self._persist_state()

        return report

    def _compute_pipeline(self, snapshots, histories, candles=None) -> PipelineResult:
        """CPU-bound computation: features, regimes, scoring. Runs in a thread."""
        features = self.feature_engine.compute(snapshots, histories, candles=candles)
        validation = self.validator.validate(features, snapshots, histories)
        features = validation.sanitized_features

        btc_symbol = "BTCUSDT" if "BTCUSDT" in features else next(iter(features.keys()))
        global_regime = self.regime_model.classify_global(features[btc_symbol])
        local_regimes: Dict[str, RegimeState] = {
            symbol: self.regime_model.classify_local(symbol, fv, global_regime)
            for symbol, fv in features.items()
        }

        price_corr = self._correlations_to_btc("prices", features.keys(), btc_symbol, regime=global_regime)
        spread_corr = self._correlations_to_btc("spread", features.keys(), btc_symbol, regime=global_regime)
        # Always compute stress-window correlations for comparison.
        stress_regime = RegimeState(
            regime=MarketRegime.PANIC, confidence=1.0,
            probabilities={}, stable_for_cycles=0,
        )
        price_corr_stress = self._correlations_to_btc("prices", features.keys(), btc_symbol, regime=stress_regime)
        spread_corr_stress = self._correlations_to_btc("spread", features.keys(), btc_symbol, regime=stress_regime)

        # Multi-timeframe convergence scores.
        convergence_scores: Dict[str, float] = {}
        for symbol, fv in features.items():
            convergence_scores[symbol] = self.regime_model.assess_timeframe_convergence(fv)

        scoring_enabled = self._can_score(validation.status, global_regime, features[btc_symbol])
        if scoring_enabled:
            opportunities = self.scorer.score(features, local_regimes, price_corr, spread_corr, convergence_scores, global_regime=global_regime.regime)
            if not opportunities:
                scoring_enabled = False
        else:
            opportunities = []
        scored_symbols = {o.symbol for o in opportunities}
        null_score_symbols = sorted([s for s in features.keys() if s not in scored_symbols])

        btc_metrics = self._extract_global_metrics(features[btc_symbol], histories.get(btc_symbol, {}).get("price", []))

        return PipelineResult(
            features=features,
            validation=validation,
            btc_symbol=btc_symbol,
            global_regime=global_regime,
            local_regimes=local_regimes,
            price_corr=price_corr,
            spread_corr=spread_corr,
            price_corr_stress=price_corr_stress,
            spread_corr_stress=spread_corr_stress,
            convergence_scores=convergence_scores,
            scoring_enabled=scoring_enabled,
            opportunities=opportunities,
            null_score_symbols=null_score_symbols,
            btc_metrics=btc_metrics,
        )

    def _select_symbols(self) -> List[str]:
        if self.config.symbols:
            return self.config.symbols[: self.config.max_symbols]
        common = sorted(s for s in self.collector.market_data.common_pairs if s.endswith("USDT"))
        return common[: self.config.max_symbols]

    def _histories_for_features(self, symbols) -> Dict[str, Dict[str, List[float]]]:
        out: Dict[str, Dict[str, List[float]]] = {}
        st = self.collector.state
        for symbol in symbols:
            out[symbol] = {
                "price": list(st.prices[symbol]),
                "bid": list(st.bids[symbol]),
                "ask": list(st.asks[symbol]),
                "spot": list(st.spot[symbol]),
                "funding": list(st.funding[symbol]),
                "basis": list(st.basis_bps[symbol]),
                "oi": list(st.open_interest[symbol]),
                "long_short": list(st.long_short_ratio[symbol]),
                "liquidation": list(st.liquidation_score[symbol]),
                "volume": list(st.volume_proxy[symbol]),
                "spread": list(st.spread_bps[symbol]),
            }
        return out

    def _correlations_to_btc(
        self, metric: str, symbols, btc_symbol: str,
        regime: Optional[RegimeState] = None,
    ) -> Dict[str, float]:
        st = self.collector.state
        # Adaptive window: scale between stress and normal based on regime confidence
        if regime and regime.regime in {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}:
            base_window = self.config.stress_correlation_window
        elif regime and regime.regime in {MarketRegime.OVERHEATED}:
            # Transitional: blend between stress and normal
            blend = min(1.0, regime.confidence)
            base_window = int(
                self.config.stress_correlation_window * blend
                + self.config.correlation_window * (1.0 - blend)
            )
        else:
            base_window = self.config.correlation_window

        # Further reduce window if regime is unstable (recently changed)
        if regime and regime.stable_for_cycles <= 2:
            window = max(self.config.stress_correlation_window, base_window // 2)
        else:
            window = base_window

        def _get_series(s: str) -> List[float]:
            source = st.spread_bps if metric == "spread" else st.prices
            return list(source[s])

        btc_values = _get_series(btc_symbol)

        btc_ret = rolling_returns(btc_values)
        out: Dict[str, float] = {}
        for symbol in symbols:
            vals = _get_series(symbol)
            r = rolling_returns(vals)
            n = min(len(r), len(btc_ret), window)
            if n < 20:
                out[symbol] = 0.0
                continue
            # BLOCK 2.2: Use robust correlation
            out[symbol] = robust_corr(r[-n:], btc_ret[-n:])
        return out

    @staticmethod
    def _extract_global_metrics(feature, price_history: List[float]) -> Dict[str, float | str | None]:
        v = feature.values
        atr_pct = v.get("atr_pct")
        oi_delta_pct = v.get("oi_delta_pct")
        funding_pct = v.get("funding_pct")
        bb_width_pct = v.get("bb_width_pct")
        atr_percentile = None if atr_pct is None else float(v.get("atr_percentile") or 0.0)

        vol_regime = "unavailable"
        if atr_pct is not None and bb_width_pct is not None:
            if atr_percentile < 0.30:
                vol_regime = "low"
            elif atr_percentile <= 0.70:
                vol_regime = "medium"
            else:
                vol_regime = "high"

        current_price = v.get("price")
        window = list(price_history[-120:]) if price_history else ([] if current_price is None else [float(current_price)])
        range_low = min(window) if window else None
        range_high = max(window) if window else None
        if vol_regime == "low":
            volatility_brief = "спокойная"
        elif vol_regime == "medium":
            volatility_brief = "умеренная"
        elif vol_regime == "high":
            volatility_brief = "повышенная"
        else:
            volatility_brief = "оценка ограничена"

        return {
            "current_price": current_price,
            "range_low": range_low,
            "range_high": range_high,
            "volatility_brief": volatility_brief,
            "atr_pct": atr_pct,
            "funding_pct": funding_pct,
            "open_interest": v.get("open_interest"),
            "oi_delta_pct": oi_delta_pct,
            "bb_width_pct": bb_width_pct,
            "volatility_regime": vol_regime,
            "atr_percentile": atr_percentile,
        }

    @staticmethod
    def _build_local_context(feature) -> Dict[str, str | float | None]:
        v = feature.values
        adx_local = v.get("adx_local")
        vol_exp = v.get("local_volatility_expansion")
        momentum = v.get("local_momentum_bias")

        if adx_local is None:
            adx_note = "unavailable (insufficient candles)"
        elif adx_local < 20:
            adx_note = "weak trend"
        elif adx_local < 30:
            adx_note = "moderate trend"
        else:
            adx_note = "strong trend"

        vol_state = "expanding" if (vol_exp or 0) > 0 else "contracting"
        if momentum is None or abs(momentum) < 0.2:
            mom = "neutral"
        elif momentum > 0:
            mom = "bullish"
        else:
            mom = "bearish"

        return {
            "adx": adx_local,
            "adx_comment": adx_note,
            "volatility_state": vol_state,
            "momentum_bias": mom,
        }

    def _build_dynamic_deltas(self, global_regime: RegimeState, btc_metrics: Dict[str, float | str | None]) -> Dict:
        prev = self._previous_payload
        if not prev:
            return {
                "first_cycle": True,
                "regime_changed": False,
                "confidence_change": None,
                "volatility_change_pct": None,
                "oi_change_pct": None,
                "funding_change_pct": None,
                "arrows": {
                    "confidence": "—",
                    "volatility": "—",
                    "oi": "—",
                    "funding": "—",
                },
                "message": "Dynamic comparison unavailable (insufficient history).",
            }

        prev_reg = prev.get("global", {}).get("regime", {}).get("name")
        prev_conf = float(prev.get("global", {}).get("regime", {}).get("confidence", 0.0))
        prev_metrics = prev.get("global", {}).get("metrics", {})

        conf_delta = global_regime.confidence - prev_conf
        vol_delta = self._delta_nullable(btc_metrics.get("atr_pct"), prev_metrics.get("atr_pct"))
        oi_delta = self._delta_nullable(btc_metrics.get("oi_delta_pct"), prev_metrics.get("oi_delta_pct"))
        funding_delta = self._delta_nullable(btc_metrics.get("funding_pct"), prev_metrics.get("funding_pct"))

        return {
            "first_cycle": False,
            "regime_changed": prev_reg != global_regime.regime.value,
            "confidence_change": conf_delta,
            "volatility_change_pct": vol_delta,
            "oi_change_pct": oi_delta,
            "funding_change_pct": funding_delta,
            "arrows": {
                "confidence": self._arrow(conf_delta),
                "volatility": self._arrow(vol_delta),
                "oi": self._arrow(oi_delta),
                "funding": self._arrow(funding_delta),
            },
        }

    @staticmethod
    def _can_score(status: DataHealthStatus, global_regime: RegimeState, btc_feature) -> bool:
        v = btc_feature.values
        return (
            status == DataHealthStatus.OK
            and global_regime.stable_for_cycles >= 3
            and v.get("atr_pct") is not None
            and v.get("bb_width_pct") is not None
            and v.get("oi_delta_pct") is not None
            and v.get("funding_pct") is not None
        )

    @staticmethod
    def _delta_nullable(cur: float | None, prev: float | None) -> float | None:
        if cur is None or prev is None:
            return None
        return float(cur) - float(prev)

    @staticmethod
    def _arrow(v: float | None, eps: float = 1e-9) -> str:
        if v is None:
            return "—"
        if v > eps:
            return "↑"
        if v < -eps:
            return "↓"
        return "—"

    def _detect_alerts(self, features, regimes) -> List[str]:
        alerts: List[str] = []
        panic_vol_threshold = self.config.alert_rsi_panic_vol
        rsi_overheat_threshold = self.config.alert_rsi_overheat
        funding_extreme_threshold = self.config.alert_funding_extreme
        for symbol, fv in features.items():
            v = fv.values
            reg = regimes[symbol].regime
            if reg == MarketRegime.PANIC and float(v.get("rolling_volatility", 0.0) or 0.0) > panic_vol_threshold:
                alerts.append(f"Crash risk: {symbol} (panic + elevated vol)")
            if reg == MarketRegime.OVERHEATED and float(v.get("rsi", 0.0) or 0.0) > rsi_overheat_threshold:
                alerts.append(f"Overheat: {symbol} (RSI={float(v.get('rsi', 0.0) or 0.0):.1f})")
            if abs(float(v.get("funding_rate", 0.0) or 0.0)) >= funding_extreme_threshold:
                alerts.append(f"Funding extreme: {symbol} ({float(v.get('funding_rate', 0.0) or 0.0):.4f})")
        return alerts

    def _assert_consistency(self, payload: Dict) -> None:
        metrics = payload.get("global", {}).get("metrics", {})
        atr_pct = metrics.get("atr_pct")
        vol_regime = metrics.get("volatility_regime")
        oi_delta = metrics.get("oi_delta_pct")
        scoring_enabled = bool(payload.get("scoring_enabled"))
        opportunities = payload.get("opportunities", [])

        if atr_pct is None and str(vol_regime).lower() == "high":
            raise RuntimeError("Consistency error: ATR is null but volatility is High.")
        if metrics.get("open_interest") is None and oi_delta is not None:
            raise RuntimeError("Consistency error: OI is null but OI delta is numeric.")
        if not scoring_enabled and opportunities:
            raise RuntimeError("Consistency error: scoring_enabled=false but opportunities exist.")

        conf_delta = payload.get("dynamic_deltas", {}).get("confidence_change")
        if conf_delta is not None and abs(float(conf_delta)) > 0.5:
            logger.warning("confidence_delta_anomaly value=%s", conf_delta)

    async def _restore_persisted_state(self) -> None:
        if self._state_restored or not self.config.persist_enabled:
            return
        self._state_restored = True
        saved = await asyncio.to_thread(persistence.load_state, self.config.persist_file)
        if not saved:
            return
        try:
            persistence.restore_collector_state(self.collector.state, saved, self.collector.maxlen)
            persistence.restore_regime_state(self.regime_model, saved)
            persistence.restore_feature_stats(self.feature_engine, saved)
            logger.info("Restored persisted MI state from %s", self.config.persist_file)
        except Exception as e:
            logger.warning("Failed to restore persisted state: %s", e)

    async def _persist_state(self) -> None:
        try:
            await asyncio.to_thread(
                persistence.save_state,
                self.config.persist_file,
                self.collector.state,
                self.regime_model._stable_state,
                self.regime_model._smooth_probs,
                persistence.extract_regime_history(self.regime_model),
                persistence.extract_feature_stats(self.feature_engine),
            )
        except Exception as e:
            logger.warning("Failed to persist MI state: %s", e)

    async def _ensure_previous_loaded(self) -> None:
        if self._previous_loaded:
            return
        self._previous_payload = await self._load_previous_snapshot()
        self._previous_loaded = True

    async def _load_previous_snapshot(self) -> Optional[Dict]:
        if not self._snapshot_file.exists():
            return None

        def _read() -> Optional[Dict]:
            try:
                return json.loads(self._snapshot_file.read_text(encoding="utf-8"))
            except Exception:
                return None

        return await asyncio.to_thread(_read)

    async def _save_previous_snapshot(self, payload: Dict) -> None:
        await asyncio.to_thread(self._snapshot_file.parent.mkdir, parents=True, exist_ok=True)

        def _write() -> None:
            self._snapshot_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

        await asyncio.to_thread(_write)
