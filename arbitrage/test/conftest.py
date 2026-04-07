"""Shared pytest fixtures for arbitrage tests."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def event_loop():
    """Provide a fresh event loop for each test."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


def make_arbitrage_config(**overrides):
    """Create ArbitrageConfig with sensible defaults."""
    from arbitrage.utils.config import ArbitrageConfig
    cfg = ArbitrageConfig(
        max_position_pct=0.10,
        max_concurrent_positions=3,
        emergency_margin_ratio=0.1,
        max_delta_percent=0.01,
        leverage=10,
        entry_threshold=0.08,
        exit_threshold=0.03,
    )
    for k, v in overrides.items():
        object.__setattr__(cfg, k, v)
    return cfg


def make_bot_state():
    """Create a BotState with in-memory persistence (no disk I/O)."""
    from arbitrage.core.state import BotState
    return BotState(persist_path=":memory:")


def make_risk_state(starting_equity=10_000.0):
    """Create a SystemState for testing."""
    from arbitrage.system.state import SystemState
    return SystemState(starting_equity=starting_equity, positions_file=":memory:")


def make_system_risk_engine(state=None):
    """Create a RiskEngine (system-level) with defaults."""
    from arbitrage.system.risk import RiskEngine
    from arbitrage.system.config import RiskConfig
    if state is None:
        state = make_risk_state()
    config = RiskConfig()
    return RiskEngine(config=config, state=state)
