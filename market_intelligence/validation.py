from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

from market_intelligence.models import DataHealthStatus, FeatureVector, PairSnapshot


@dataclass
class ValidationResult:
    status: DataHealthStatus
    warnings: List[str]
    score_enabled: bool
    risk_enabled: bool
    sanitized_features: Dict[str, FeatureVector]


class DataValidator:
    def __init__(self, min_metric_pct: float = 0.01, liquid_oi_threshold: float = 100.0):
        self.min_metric_pct = min_metric_pct
        self.liquid_oi_threshold = liquid_oi_threshold

    def validate(
        self,
        features: Dict[str, FeatureVector],
        snapshots: Dict[str, PairSnapshot],
        histories: Dict[str, Dict[str, List[float]]],
    ) -> ValidationResult:
        warnings: List[str] = []
        sanitized: Dict[str, FeatureVector] = {}
        symbol_invalid_core = 0

        for symbol, fv in features.items():
            v = dict(fv.values)
            z = dict(fv.normalized)
            snap = snapshots.get(symbol)
            hist = histories.get(symbol, {})

            if self._is_invalid_atr(v, hist):
                v["atr"] = None
                v["atr_pct"] = None
                v["atr_percentile"] = None
                z["atr"] = None
                z["atr_pct"] = None
                warnings.append(f"{symbol}:invalid_atr_pct")

            if self._is_invalid_bb_width(v):
                v["bb_width"] = None
                v["bb_width_pct"] = None
                v["volatility_regime_code"] = None
                z["bb_width"] = None
                warnings.append(f"{symbol}:invalid_bb_width_pct")

            oi_ok, oi_warn = self._validate_oi_delta(v, hist)
            if not oi_ok:
                v["oi_delta"] = None
                v["oi_delta_pct"] = None
                z["oi_delta"] = None
                warnings.append(f"{symbol}:{oi_warn}")

            funding_ok, funding_warn = self._validate_funding(v, snap)
            if not funding_ok:
                v["funding_rate"] = None
                v["funding_pct"] = None
                z["funding_rate"] = None
                warnings.append(f"{symbol}:{funding_warn}")

            if self._all_core_metrics_empty(v):
                symbol_invalid_core += 1
                warnings.append(f"{symbol}:feature_engine_data_incomplete")

            sanitized[symbol] = FeatureVector(
                symbol=fv.symbol,
                timestamp=fv.timestamp,
                values=v,
                normalized=z,
            )

        if not sanitized:
            return ValidationResult(
                status=DataHealthStatus.INVALID,
                warnings=["feature_engine_data_incomplete"],
                score_enabled=False,
                risk_enabled=False,
                sanitized_features=sanitized,
            )

        if symbol_invalid_core == len(sanitized):
            return ValidationResult(
                status=DataHealthStatus.INVALID,
                warnings=warnings + ["Feature engine data incomplete."],
                score_enabled=False,
                risk_enabled=False,
                sanitized_features=sanitized,
            )

        status = DataHealthStatus.OK if not warnings else DataHealthStatus.PARTIAL
        return ValidationResult(
            status=status,
            warnings=warnings,
            score_enabled=True,
            risk_enabled=True,
            sanitized_features=sanitized,
        )

    def _is_invalid_atr(self, values: Dict[str, float | None], hist: Dict[str, List[float]]) -> bool:
        atr_pct = values.get("atr_pct")
        if atr_pct is None:
            return False
        atr_source_code = float(values.get("atr_source_code") or 0.0)
        # Proxy ATR with very low value is unreliable.
        if atr_source_code == 1.0 and abs(float(atr_pct)) < 0.05:
            return True
        if abs(float(atr_pct)) >= self.min_metric_pct:
            return False
        prices = hist.get("price", [])
        if len(prices) < 2:
            return True
        unchanged = all(abs(float(prices[i]) - float(prices[i - 1])) <= 1e-9 for i in range(1, len(prices)))
        return not unchanged

    def _is_invalid_bb_width(self, values: Dict[str, float | None]) -> bool:
        bb_width_pct = values.get("bb_width_pct")
        if bb_width_pct is None:
            return False
        return abs(float(bb_width_pct)) < self.min_metric_pct

    def _validate_oi_delta(self, values: Dict[str, float | None], hist: Dict[str, List[float]]) -> Tuple[bool, str]:
        oi_hist = hist.get("oi", [])
        if len(oi_hist) < 2:
            return False, "oi_delta_unavailable"
        oi_delta_pct = values.get("oi_delta_pct")
        if oi_delta_pct is None:
            return False, "oi_delta_unavailable"
        open_interest = float(values.get("open_interest") or 0.0)
        if open_interest >= self.liquid_oi_threshold and abs(float(oi_delta_pct)) <= 1e-12:
            return False, "invalid_oi_delta_zero"
        return True, ""

    def _validate_funding(self, values: Dict[str, float | None], snap: PairSnapshot | None) -> Tuple[bool, str]:
        funding_pct = values.get("funding_pct")
        if funding_pct is None:
            return False, "funding_unavailable"
        if abs(float(funding_pct)) > 1e-12:
            return True, ""
        if not snap or not snap.funding_by_exchange:
            return False, "funding_zero_without_exchange_confirmation"
        all_zero = all(abs(float(x)) <= 1e-12 for x in snap.funding_by_exchange.values())
        return (True, "") if all_zero else (False, "funding_mismatch_cross_exchange")

    @staticmethod
    def check_staleness(
        snapshots: Dict[str, "PairSnapshot"],
        regimes: Dict[str, "RegimeState"] | None = None,
        stale_threshold: float = 600.0,
    ) -> List[str]:
        """Return warnings for stale slow-update data under stress regimes."""
        from market_intelligence.models import MarketRegime
        warnings: List[str] = []
        stress_regimes = {MarketRegime.PANIC, MarketRegime.HIGH_VOLATILITY}
        for symbol, snap in snapshots.items():
            if regimes:
                rs = regimes.get(symbol)
                if rs and rs.regime not in stress_regimes:
                    continue
            lsr_age = snap.data_staleness.get("lsr")
            if lsr_age is not None and lsr_age > stale_threshold:
                warnings.append(f"{symbol}:stale_lsr")
            liq_age = snap.data_staleness.get("liquidations")
            if liq_age is not None and liq_age > stale_threshold:
                warnings.append(f"{symbol}:stale_liquidations")
        return warnings

    @staticmethod
    def _all_core_metrics_empty(values: Dict[str, float | None]) -> bool:
        core_keys = (
            "atr_pct",
            "bb_width_pct",
            "oi_delta_pct",
            "funding_pct",
            "rolling_volatility",
            "basis_bps",
            "spread_bps",
        )
        for key in core_keys:
            value = values.get(key)
            if value is None:
                continue
            if abs(float(value)) > 1e-12:
                return False
        return True
