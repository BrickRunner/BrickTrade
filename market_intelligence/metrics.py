"""Lightweight in-process metrics collection for observability.
No external dependencies — stores metrics in memory with ring buffers."""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, List, Optional, Tuple


@dataclass
class LatencyRecord:
    stage: str
    duration_ms: float
    timestamp: float


class InProcessMetrics:
    """Thread-safe metrics collector with ring buffers.

    Tracks:
    - Pipeline stage latencies (collection, features, regime, scoring, portfolio, total)
    - Exchange API call counts and error rates
    - Regime transitions
    - Data quality degradation events
    - Scoring distribution statistics
    """

    def __init__(self, history_size: int = 500):
        self._lock = threading.Lock()
        self._history_size = history_size

        # Latency tracking per stage
        self._latencies: Dict[str, Deque[Tuple[float, float]]] = {}  # stage -> deque of (timestamp, ms)

        # Counters
        self._counters: Dict[str, int] = {}

        # Gauges (current values)
        self._gauges: Dict[str, float] = {}

        # Event log (ring buffer)
        self._events: Deque[Tuple[float, str, Dict[str, Any]]] = deque(maxlen=history_size)

        # Per-exchange error tracking
        self._exchange_calls: Dict[str, int] = {}
        self._exchange_errors: Dict[str, int] = {}

    def record_latency(self, stage: str, duration_ms: float) -> None:
        with self._lock:
            if stage not in self._latencies:
                self._latencies[stage] = deque(maxlen=self._history_size)
            self._latencies[stage].append((time.time(), duration_ms))

    def record_counter(self, name: str, value: int = 1, labels: Optional[Dict[str, str]] = None) -> None:
        key = name if not labels else f"{name}|{'|'.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + value

    def record_gauge(self, name: str, value: float, labels: Optional[Dict[str, str]] = None) -> None:
        key = name if not labels else f"{name}|{'|'.join(f'{k}={v}' for k, v in sorted(labels.items()))}"
        with self._lock:
            self._gauges[key] = value

    def record_event(self, event_type: str, details: Optional[Dict[str, Any]] = None) -> None:
        with self._lock:
            self._events.append((time.time(), event_type, details or {}))

    def record_exchange_call(self, exchange: str, success: bool) -> None:
        with self._lock:
            self._exchange_calls[exchange] = self._exchange_calls.get(exchange, 0) + 1
            if not success:
                self._exchange_errors[exchange] = self._exchange_errors.get(exchange, 0) + 1

    def get_latency_stats(self, stage: str, last_n: int = 50) -> Dict[str, float]:
        """Return min/max/avg/p95 latency for a stage."""
        with self._lock:
            records = self._latencies.get(stage, deque())
            if not records:
                return {"min_ms": 0.0, "max_ms": 0.0, "avg_ms": 0.0, "p95_ms": 0.0, "count": 0}
            values = [r[1] for r in list(records)[-last_n:]]

        values.sort()
        n = len(values)
        p95_idx = min(n - 1, int(n * 0.95))
        return {
            "min_ms": values[0],
            "max_ms": values[-1],
            "avg_ms": sum(values) / n,
            "p95_ms": values[p95_idx],
            "count": n,
        }

    def get_exchange_health(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            result = {}
            for ex in set(list(self._exchange_calls.keys()) + list(self._exchange_errors.keys())):
                calls = self._exchange_calls.get(ex, 0)
                errors = self._exchange_errors.get(ex, 0)
                result[ex] = {
                    "total_calls": calls,
                    "errors": errors,
                    "error_rate": errors / max(calls, 1),
                    "healthy": (errors / max(calls, 1)) < 0.3,
                }
            return result

    def get_snapshot(self) -> Dict[str, Any]:
        """Full metrics snapshot for health check / Telegram report."""
        stages = ["collection", "features", "regime", "scoring", "portfolio", "total"]
        latency_summary = {s: self.get_latency_stats(s) for s in stages if s in self._latencies}
        with self._lock:
            return {
                "latencies": latency_summary,
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "exchange_health": self.get_exchange_health(),
                "recent_events": [
                    {"ts": ts, "type": t, "details": d}
                    for ts, t, d in list(self._events)[-20:]
                ],
            }


# Global singleton
_metrics: Optional[InProcessMetrics] = None


def get_metrics() -> InProcessMetrics:
    global _metrics
    if _metrics is None:
        _metrics = InProcessMetrics()
    return _metrics
