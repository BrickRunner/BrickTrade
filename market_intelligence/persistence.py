"""Save and restore MarketIntelligence state to/from JSON for crash recovery."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import defaultdict, deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Deque, Dict, List, Protocol

from market_intelligence.collector import CollectorState
from market_intelligence.models import MarketRegime, RegimeState

if TYPE_CHECKING:
    from market_intelligence.statistics import RollingStats


class RegimeModelProtocol(Protocol):
    _stable_state: Dict[str, RegimeState]
    _smooth_probs: Dict[str, Dict[MarketRegime, float]]
    _history: Dict[str, Deque[MarketRegime]]


class FeatureEngineProtocol(Protocol):
    _stats: Dict[str, Dict[str, "RollingStats"]]
    _window: int

logger = logging.getLogger("market_intelligence")


def save_state(
    path: str | Path,
    collector_state: CollectorState,
    regime_stable: Dict[str, Any],
    regime_smooth_probs: Dict[str, Dict[str, float]],
    regime_history: Dict[str, List[str]],
    feature_stats: Dict[str, Dict[str, List[float]]],
) -> None:
    """Serialize full MI state to a JSON file (atomic write: temp→rename)."""
    data = {
        "version": 1,
        "collector": _serialize_collector(collector_state),
        "regime": {
            "stable": {
                s: {
                    "regime": rs.regime.value,
                    "confidence": rs.confidence,
                    "probabilities": {k.value: v for k, v in rs.probabilities.items()},
                    "stable_for_cycles": rs.stable_for_cycles,
                }
                for s, rs in regime_stable.items()
            },
            "smooth_probs": {
                s: {k.value if isinstance(k, MarketRegime) else k: v for k, v in probs.items()}
                for s, probs in regime_smooth_probs.items()
            },
            "history": {
                s: [r.value if isinstance(r, MarketRegime) else r for r in hist]
                for s, hist in regime_history.items()
            },
        },
        "feature_stats": feature_stats,
    }
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        # Atomic rename (works on the same filesystem).
        os.replace(tmp, str(dest))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str | Path) -> Dict[str, Any] | None:
    """Load previously saved MI state. Returns None if file doesn't exist or is corrupt."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if raw.get("version") != 1:
            logger.warning("persistence: unknown version %s, ignoring", raw.get("version"))
            return None
        return raw
    except Exception as e:
        logger.warning("persistence: failed to load %s: %s", path, e)
        return None


def restore_collector_state(state: CollectorState, saved: Dict[str, Any], maxlen: int) -> None:
    """Populate CollectorState deques from saved JSON dict."""
    c = saved.get("collector", {})
    _fields = [
        "prices", "bids", "asks", "spot", "funding", "basis_bps",
        "open_interest", "long_short_ratio", "liquidation_score", "volume_proxy", "spread_bps",
    ]
    for field_name in _fields:
        src = c.get(field_name, {})
        target = getattr(state, field_name)
        for symbol, values in src.items():
            d = target[symbol]  # creates defaultdict entry
            for v in values:
                d.append(float(v))


def restore_regime_state(
    regime_model: RegimeModelProtocol,
    saved: Dict[str, Any],
) -> None:
    """Restore RegimeModel internal state from saved JSON."""

    r = saved.get("regime", {})

    # Restore stable states
    for s, data in r.get("stable", {}).items():
        regime_model._stable_state[s] = RegimeState(
            regime=MarketRegime(data["regime"]),
            confidence=float(data["confidence"]),
            probabilities={MarketRegime(k): float(v) for k, v in data.get("probabilities", {}).items()},
            stable_for_cycles=int(data.get("stable_for_cycles", 0)),
        )

    # Restore smoothed probabilities
    for s, probs in r.get("smooth_probs", {}).items():
        regime_model._smooth_probs[s] = {
            MarketRegime(k): float(v) for k, v in probs.items()
        }

    # Restore history
    for s, hist in r.get("history", {}).items():
        regime_model._history[s] = deque(
            [MarketRegime(v) for v in hist],
            maxlen=64,
        )


def restore_feature_stats(
    feature_engine: FeatureEngineProtocol,
    saved: Dict[str, Any],
) -> None:
    """Restore FeatureEngine._stats (RollingStats) from saved JSON."""
    from market_intelligence.statistics import RollingStats

    stats_data = saved.get("feature_stats", {})
    for symbol, keys in stats_data.items():
        for key, values in keys.items():
            rs = RollingStats(feature_engine._window)
            for v in values:
                rs.push(float(v))
            feature_engine._stats[symbol][key] = rs


def extract_feature_stats(feature_engine: FeatureEngineProtocol) -> Dict[str, Dict[str, List[float]]]:
    """Extract FeatureEngine._stats into a JSON-serializable dict."""
    result: Dict[str, Dict[str, List[float]]] = {}
    for symbol, keys in feature_engine._stats.items():
        result[symbol] = {}
        for key, rs in keys.items():
            result[symbol][key] = rs.values
    return result


def extract_regime_history(regime_model: RegimeModelProtocol) -> Dict[str, List[str]]:
    """Extract regime history deques into JSON-serializable form."""
    return {
        s: [r.value if isinstance(r, MarketRegime) else str(r) for r in hist]
        for s, hist in regime_model._history.items()
    }


def extract_regime_smooth_probs(regime_model: RegimeModelProtocol) -> Dict[str, Dict[str, float]]:
    """Extract smoothed probabilities into JSON-serializable form."""
    return {
        s: {k.value if isinstance(k, MarketRegime) else k: v for k, v in probs.items()}
        for s, probs in regime_model._smooth_probs.items()
    }


def _serialize_collector(state: CollectorState) -> Dict[str, Dict[str, List[float]]]:
    """Serialize all deque-based fields of CollectorState."""
    result: Dict[str, Dict[str, List[float]]] = {}
    _fields = [
        "prices", "bids", "asks", "spot", "funding", "basis_bps",
        "open_interest", "long_short_ratio", "liquidation_score", "volume_proxy", "spread_bps",
    ]
    for field_name in _fields:
        d = getattr(state, field_name)
        result[field_name] = {symbol: list(dq) for symbol, dq in d.items()}
    return result
