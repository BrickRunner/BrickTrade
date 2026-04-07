"""
Tests for DailyCalibrator module.
"""
import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from arbitrage.system.calibrator import CalibrationMetrics, DailyCalibrator


@pytest.fixture
def temp_log_dir():
    """Create temporary log directory structure."""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_dir = Path(tmpdir) / "logs"
        log_dir.mkdir()
        yield log_dir


@pytest.fixture
def sample_logs(temp_log_dir):
    """Create sample log files with various metrics."""
    date_str = "2026-03-26"
    date_dir = temp_log_dir / date_str / "08"
    date_dir.mkdir(parents=True)

    log_file = date_dir / "arbitrage.log"
    log_content = """
2026-03-26 08:00:01,123 - trading_system - INFO - execution_success: BTCUSDT slippage=5.2 bps
2026-03-26 08:00:02,456 - trading_system - INFO - latency=250.0 ms to OKX
2026-03-26 08:00:03,789 - trading_system - INFO - spread=0.85% for ETHUSDT
2026-03-26 08:00:04,012 - trading_system - WARNING - 429 from okx
2026-03-26 08:00:05,345 - trading_system - INFO - filled order BTCUSDT
2026-03-26 08:00:06,678 - trading_system - ERROR - execution_reject: insufficient margin
2026-03-26 08:00:07,901 - trading_system - INFO - hedge_needed for partial fill
2026-03-26 08:00:08,234 - trading_system - INFO - slippage=12.5 bps
2026-03-26 08:00:09,567 - trading_system - INFO - [SPREAD_NEAR_MISS] ETHUSDT
2026-03-26 08:00:10,890 - trading_system - WARNING - circuit_breaker TRIPPED for bybit
    """
    log_file.write_text(log_content)

    return temp_log_dir, date_str


def test_parse_empty_logs(temp_log_dir):
    """Test parsing when no logs exist."""
    calibrator = DailyCalibrator(log_dir=str(temp_log_dir))
    metrics = calibrator._parse_logs("2026-01-01")

    assert metrics.fills == 0
    assert metrics.rejects == 0
    assert len(metrics.slippage_bps) == 0
    assert len(metrics.latency_ms) == 0


def test_parse_logs_with_metrics(sample_logs):
    """Test parsing logs with various metrics."""
    log_dir, date_str = sample_logs
    calibrator = DailyCalibrator(log_dir=str(log_dir))
    metrics = calibrator._parse_logs(date_str)

    # Check counts (regex catches multiple fill-related keywords)
    assert metrics.fills >= 1
    assert metrics.rejects >= 1
    assert metrics.hedge_events == 1
    assert metrics.entry_near_misses == 1

    # Check rate limits
    assert "okx" in metrics.rate_limit_429s
    assert metrics.rate_limit_429s["okx"] == 1

    # Check circuit breaker
    assert "bybit" in metrics.circuit_breaker_trips
    assert metrics.circuit_breaker_trips["bybit"] == 1

    # Check numeric metrics
    assert len(metrics.slippage_bps) == 2
    assert 5.2 in metrics.slippage_bps
    assert 12.5 in metrics.slippage_bps

    assert len(metrics.latency_ms) == 1
    assert 250.0 in metrics.latency_ms

    assert len(metrics.spreads_pct) == 1
    assert 0.85 in metrics.spreads_pct


def test_compute_stats():
    """Test statistics computation."""
    calibrator = DailyCalibrator()
    metrics = CalibrationMetrics()
    metrics.slippage_bps = [5.0, 10.0, 15.0, 20.0, 25.0]
    metrics.latency_ms = [100.0, 200.0, 300.0]
    metrics.spreads_pct = [0.5, 0.8, 1.2]
    metrics.fills = 10
    metrics.rejects = 2

    stats = calibrator._compute_stats(metrics)

    assert stats["fills"] == 10
    assert stats["rejects"] == 2
    assert "slippage" in stats
    assert stats["slippage"]["median"] == 15.0
    assert stats["slippage"]["max"] == 25.0
    assert "latency" in stats
    assert stats["latency"]["median"] == 200.0


def test_recommendations_high_slippage():
    """Test recommendation for high slippage."""
    calibrator = DailyCalibrator()
    metrics = CalibrationMetrics()
    metrics.slippage_bps = [10.0, 12.0, 14.0, 16.0, 18.0] * 5  # P95 will be high

    stats = calibrator._compute_stats(metrics)
    recommendations = calibrator._generate_recommendations(stats, metrics)

    assert "RISK_MAX_SLIPPAGE_BPS" in recommendations
    rec = recommendations["RISK_MAX_SLIPPAGE_BPS"]
    assert rec["recommended"] > 12.0
    assert "exceeds current limit" in rec["reason"]


def test_recommendations_high_429s():
    """Test recommendation for too many 429s."""
    calibrator = DailyCalibrator()
    metrics = CalibrationMetrics()
    metrics.rate_limit_429s = {"okx": 60}
    metrics.fills = 5

    stats = calibrator._compute_stats(metrics)
    recommendations = calibrator._generate_recommendations(stats, metrics)

    assert "RATE_LIMITER" in recommendations
    rec = recommendations["RATE_LIMITER"]
    assert rec["exchange"] == "okx"
    assert rec["429_count"] == 60


def test_recommendations_high_hedge_rate():
    """Test recommendation for high hedge rate."""
    calibrator = DailyCalibrator()
    metrics = CalibrationMetrics()
    metrics.fills = 10
    metrics.hedge_events = 4  # 40% hedge rate

    stats = calibrator._compute_stats(metrics)
    recommendations = calibrator._generate_recommendations(stats, metrics)

    assert "HEDGE_QUALITY" in recommendations
    rec = recommendations["HEDGE_QUALITY"]
    assert rec["hedge_rate"] == 0.4
    assert "maker-taker" in rec["recommended_action"]


@pytest.mark.asyncio
async def test_full_calibration_run(sample_logs):
    """Test full calibration run."""
    log_dir, date_str = sample_logs
    output_dir = log_dir / "calibration"
    output_dir.mkdir()

    calibrator = DailyCalibrator(log_dir=str(log_dir), output_dir=str(output_dir))
    report = await calibrator.run(date_str)

    # Check report structure
    assert report.date == date_str
    assert "fills" in report.metrics
    assert report.metrics["fills"] >= 1  # At least one fill detected

    # Check report was saved
    report_file = output_dir / f"{date_str}.json"
    assert report_file.exists()


def test_percentile_calculation():
    """Test percentile calculation."""
    calibrator = DailyCalibrator()

    data = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    p50 = calibrator._percentile(data, 50)
    assert p50 == 5.5

    p95 = calibrator._percentile(data, 95)
    assert p95 == pytest.approx(9.5, abs=0.1)

    p100 = calibrator._percentile(data, 100)
    assert p100 == 10.0

    # Edge cases
    assert calibrator._percentile([], 50) == 0.0
    assert calibrator._percentile([1.0], 50) == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
