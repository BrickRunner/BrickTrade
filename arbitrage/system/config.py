from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List


def _as_bool(value: str, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_float(value: str, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: str, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _first_env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value is not None and value != "":
            return value
    return default


@dataclass(frozen=True)
class ApiCredentials:
    api_key: str
    api_secret: str
    passphrase: str = ""


@dataclass(frozen=True)
class RiskConfig:
    max_total_exposure_pct: float = 0.65
    max_strategy_allocation_pct: float = 0.35
    max_leverage: float = 3.0
    max_daily_drawdown_pct: float = 0.05
    max_portfolio_drawdown_pct: float = 0.12
    max_order_slippage_bps: float = 25.0
    api_latency_limit_ms: int = 8_000
    api_latency_breach_limit: int = 5
    kill_switch_enabled: bool = True
    max_open_positions: int = 20
    max_orderbook_age_sec: float = 30.0  # REST polling needs larger tolerance than WS
    max_inventory_imbalance_pct: float = 0.80
    max_realized_slippage_bps: float = 18.0


@dataclass(frozen=True)
class ExecutionConfig:
    order_timeout_ms: int = 3000
    hedge_retries: int = 3
    cycle_interval_seconds: float = 0.5
    dry_run: bool = True
    max_new_positions_per_cycle: int = 1


@dataclass(frozen=True)
class StrategyConfig:
    enabled: List[str] = field(
        default_factory=lambda: [
            "spot_arbitrage",
            "cash_carry",
            "funding_arbitrage",
            "funding_spread",
            "triangular_arbitrage",
            "multi_triangular_arbitrage",
            "prefunded_arbitrage",
            "orderbook_imbalance",
            "spread_capture",
            "grid",
            "indicator",
        ]
    )
    min_edge_bps: float = 3.0
    funding_threshold_bps: float = 1.0
    basis_threshold_bps: float = 5.0
    grid_levels: int = 8
    indicator_rsi_window: int = 14
    indicator_ema_fast: int = 21
    indicator_ema_slow: int = 55

    @classmethod
    def from_env(cls) -> "StrategyConfig":
        raw_enabled = _first_env("ENABLED_STRATEGIES", default="").strip()
        if raw_enabled:
            enabled = [s.strip().lower() for s in raw_enabled.split(",") if s.strip()]
        else:
            enabled = [
                "spot_arbitrage",
                "cash_carry",
                "funding_arbitrage",
                "funding_spread",
                "triangular_arbitrage",
                "multi_triangular_arbitrage",
                "prefunded_arbitrage",
                "orderbook_imbalance",
                "spread_capture",
                "grid",
                "indicator",
            ]
        return cls(
            enabled=enabled,
            min_edge_bps=_as_float(os.getenv("STRATEGY_MIN_EDGE_BPS"), 3.0),
            funding_threshold_bps=_as_float(os.getenv("STRATEGY_FUNDING_THRESHOLD_BPS"), 1.0),
            basis_threshold_bps=_as_float(os.getenv("STRATEGY_BASIS_THRESHOLD_BPS"), 5.0),
            grid_levels=_as_int(os.getenv("STRATEGY_GRID_LEVELS"), 8),
            indicator_rsi_window=_as_int(os.getenv("STRATEGY_INDICATOR_RSI_WINDOW"), 14),
            indicator_ema_fast=_as_int(os.getenv("STRATEGY_INDICATOR_EMA_FAST"), 21),
            indicator_ema_slow=_as_int(os.getenv("STRATEGY_INDICATOR_EMA_SLOW"), 55),
        )


@dataclass(frozen=True)
class TradingSystemConfig:
    symbols: List[str]
    exchanges: List[str]
    credentials: Dict[str, ApiCredentials]
    starting_equity: float
    trade_all_symbols: bool = False
    max_symbols: int = 30
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    @classmethod
    def from_env(cls) -> "TradingSystemConfig":
        raw_symbols = _first_env("SYMBOLS", "SYMBOL", default="BTCUSDT,ETHUSDT").strip()
        trade_all_symbols = raw_symbols.upper() in {"ALL", "*", "AUTO"}
        symbols = [] if trade_all_symbols else [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
        exchanges = [e.strip().lower() for e in os.getenv("EXCHANGES", "okx,htx").split(",") if e.strip()]
        credentials: Dict[str, ApiCredentials] = {}
        for exchange in exchanges:
            prefix = exchange.upper()
            credentials[exchange] = ApiCredentials(
                api_key=_first_env(f"{prefix}_API_KEY", default=""),
                api_secret=_first_env(f"{prefix}_API_SECRET", f"{prefix}_SECRET", default=""),
                passphrase=_first_env(f"{prefix}_PASSPHRASE", default=""),
            )

        exec_dry_run = _as_bool(
            _first_env("EXEC_DRY_RUN", "ARB_DRY_RUN_MODE", default="false"),
            False,
        )

        return cls(
            symbols=symbols,
            exchanges=exchanges,
            credentials=credentials,
            starting_equity=_as_float(os.getenv("STARTING_EQUITY"), 10_000.0),
            trade_all_symbols=trade_all_symbols,
            max_symbols=_as_int(os.getenv("MAX_SYMBOLS"), 30),
            risk=RiskConfig(
                max_total_exposure_pct=_as_float(os.getenv("RISK_MAX_TOTAL_EXPOSURE_PCT"), 0.65),
                max_strategy_allocation_pct=_as_float(os.getenv("RISK_MAX_STRATEGY_ALLOC_PCT"), 0.35),
                max_leverage=_as_float(os.getenv("RISK_MAX_LEVERAGE"), 3.0),
                max_daily_drawdown_pct=_as_float(os.getenv("RISK_MAX_DAILY_DD_PCT"), 0.05),
                max_portfolio_drawdown_pct=_as_float(os.getenv("RISK_MAX_PORTFOLIO_DD_PCT"), 0.12),
                max_order_slippage_bps=_as_float(os.getenv("RISK_MAX_SLIPPAGE_BPS"), 12.0),
                api_latency_limit_ms=_as_int(os.getenv("RISK_API_LATENCY_MS"), 8_000),
                api_latency_breach_limit=_as_int(os.getenv("RISK_API_LATENCY_BREACH_LIMIT"), 5),
                kill_switch_enabled=_as_bool(os.getenv("RISK_KILL_SWITCH_ENABLED"), True),
                max_open_positions=_as_int(os.getenv("RISK_MAX_OPEN_POSITIONS"), 20),
                max_orderbook_age_sec=_as_float(os.getenv("RISK_MAX_ORDERBOOK_AGE_SEC"), 30.0),
                max_inventory_imbalance_pct=_as_float(os.getenv("RISK_MAX_INVENTORY_IMBALANCE_PCT"), 0.80),
                max_realized_slippage_bps=_as_float(os.getenv("RISK_MAX_REALIZED_SLIPPAGE_BPS"), 18.0),
            ),
            execution=ExecutionConfig(
                order_timeout_ms=_as_int(os.getenv("EXEC_ORDER_TIMEOUT_MS"), 3000),
                hedge_retries=_as_int(os.getenv("EXEC_HEDGE_RETRIES"), 3),
                cycle_interval_seconds=_as_float(os.getenv("EXEC_CYCLE_INTERVAL"), 0.5),
                dry_run=exec_dry_run,
                max_new_positions_per_cycle=_as_int(os.getenv("EXEC_MAX_NEW_POSITIONS_PER_CYCLE"), 1),
            ),
            strategy=StrategyConfig.from_env(),
        )

    def validate(self) -> None:
        if not self.trade_all_symbols and not self.symbols:
            raise ValueError("At least one symbol is required")
        if len(self.exchanges) < 2:
            raise ValueError("At least two exchanges are required for dual execution")
        if self.starting_equity <= 0:
            raise ValueError("Starting equity must be > 0")
        if not 0 < self.risk.max_total_exposure_pct <= 1:
            raise ValueError("max_total_exposure_pct must be in (0,1]")
        if not 0 < self.risk.max_strategy_allocation_pct <= 1:
            raise ValueError("max_strategy_allocation_pct must be in (0,1]")
