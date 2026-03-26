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
    max_total_exposure_pct: float = 0.30        # max 30% of capital at risk
    max_strategy_allocation_pct: float = 0.10   # max 10% per trade
    max_leverage: float = 5.0                   # leverage 2-5x
    max_daily_drawdown_pct: float = 0.05
    max_portfolio_drawdown_pct: float = 0.12
    max_order_slippage_bps: float = 25.0
    api_latency_limit_ms: int = 400             # per strategy spec
    api_latency_breach_limit: int = 5
    kill_switch_enabled: bool = True
    max_open_positions: int = 3                 # max 3 simultaneous arbs
    max_orderbook_age_sec: float = 10.0         # tighter for arb
    max_inventory_imbalance_pct: float = 0.80
    max_realized_slippage_bps: float = 18.0
    max_loss_per_trade_pct: float = 0.10  # emergency exit if single trade loses >10% equity


@dataclass(frozen=True)
class ExecutionConfig:
    order_timeout_ms: int = 3000
    hedge_retries: int = 3
    hedge_timeout_seconds: float = 15.0  # max total time for hedge sequence
    hedge_settle_seconds: float = 0.3    # pause after hedge order before verification
    cycle_interval_seconds: float = 0.5
    dry_run: bool = True
    max_new_positions_per_cycle: int = 1
    # Maker+Taker hybrid execution: place one leg as post-only maker
    # to save ~60-80% on fees for that leg.
    use_maker_taker: bool = False
    maker_timeout_ms: int = 2000         # how long to wait for maker fill
    maker_max_retries: int = 2           # how many times to re-place maker order
    maker_price_offset_bps: float = 0.5  # how far inside the spread to place maker
    reliability_rank: Dict[str, int] = field(
        default_factory=lambda: {"okx": 0, "bybit": 1, "htx": 2, "binance": 3}
    )


@dataclass(frozen=True)
class StrategyConfig:
    enabled: List[str] = field(
        default_factory=lambda: [
            "futures_cross_exchange",
        ]
    )
    min_edge_bps: float = 3.5
    funding_threshold_bps: float = 5.0
    basis_threshold_bps: float = 5.0
    # Futures cross-exchange arbitrage parameters
    min_spread_pct: float = 0.50
    target_profit_pct: float = 0.30
    max_spread_risk_pct: float = 0.40
    exit_spread_pct: float = 0.05
    funding_rate_threshold_pct: float = 0.01
    max_entry_latency_ms: float = 400.0
    min_book_depth_multiplier: float = 3.0
    # Cash & Carry strategy parameters
    cash_carry_min_funding_apr_pct: float = 5.0
    cash_carry_max_basis_spread_pct: float = 0.30
    cash_carry_min_holding_hours: float = 8.0
    cash_carry_max_holding_hours: float = 72.0
    cash_carry_min_book_depth_usd: float = 5000.0
    # Legacy (kept for backward compat but unused)
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
                "futures_cross_exchange",
            ]
        return cls(
            enabled=enabled,
            min_edge_bps=_as_float(os.getenv("STRATEGY_MIN_EDGE_BPS"), 3.5),
            funding_threshold_bps=_as_float(os.getenv("STRATEGY_FUNDING_THRESHOLD_BPS"), 5.0),
            basis_threshold_bps=_as_float(os.getenv("STRATEGY_BASIS_THRESHOLD_BPS"), 5.0),
            min_spread_pct=_as_float(os.getenv("ARB_MIN_SPREAD_PCT"), 0.50),
            target_profit_pct=_as_float(os.getenv("ARB_TARGET_PROFIT_PCT"), 0.30),
            max_spread_risk_pct=_as_float(os.getenv("ARB_MAX_SPREAD_RISK_PCT"), 0.40),
            exit_spread_pct=_as_float(os.getenv("ARB_EXIT_SPREAD_PCT"), 0.05),
            funding_rate_threshold_pct=_as_float(os.getenv("ARB_FUNDING_THRESHOLD_PCT"), 0.01),
            max_entry_latency_ms=_as_float(os.getenv("ARB_MAX_LATENCY_MS"), 400.0),
            min_book_depth_multiplier=_as_float(os.getenv("ARB_MIN_DEPTH_MULTIPLIER"), 3.0),
            cash_carry_min_funding_apr_pct=_as_float(os.getenv("CASH_CARRY_MIN_FUNDING_APR_PCT"), 5.0),
            cash_carry_max_basis_spread_pct=_as_float(os.getenv("CASH_CARRY_MAX_BASIS_SPREAD_PCT"), 0.30),
            cash_carry_min_holding_hours=_as_float(os.getenv("CASH_CARRY_MIN_HOLDING_HOURS"), 8.0),
            cash_carry_max_holding_hours=_as_float(os.getenv("CASH_CARRY_MAX_HOLDING_HOURS"), 72.0),
            cash_carry_min_book_depth_usd=_as_float(os.getenv("CASH_CARRY_MIN_BOOK_DEPTH_USD"), 5000.0),
        )


@dataclass(frozen=True)
class TradingSystemConfig:
    symbols: List[str]
    exchanges: List[str]
    credentials: Dict[str, ApiCredentials]
    starting_equity: float
    trade_all_symbols: bool = False
    max_symbols: int = 30
    symbol_blacklist: List[str] = field(default_factory=list)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)

    @classmethod
    def from_env(cls) -> "TradingSystemConfig":
        raw_symbols = _first_env(
            "SYMBOLS", "SYMBOL",
            default=(
                "BTCUSDT,ETHUSDT,SOLUSDT,BNBUSDT,XRPUSDT,"
                "ADAUSDT,DOGEUSDT,LINKUSDT,AVAXUSDT,LTCUSDT,"
                "DOTUSDT,MATICUSDT,UNIUSDT,ATOMUSDT,NEARUSDT,"
                "APTUSDT,ARBUSDT,OPUSDT,FILUSDT,SUIUSDT,"
                "PEPEUSDT,SHIBUSDT,TRXUSDT,TONUSDT,INJUSDT"
            ),
        ).strip()
        trade_all_symbols = raw_symbols.upper() in {"ALL", "*", "AUTO"}
        symbols = [] if trade_all_symbols else [s.strip().upper() for s in raw_symbols.split(",") if s.strip()]
        exchanges = [e.strip().lower() for e in os.getenv("EXCHANGES", "bybit,okx,htx").split(",") if e.strip()]
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

        raw_blacklist = os.getenv("SYMBOL_BLACKLIST", "").strip()
        blacklist = [s.strip().upper() for s in raw_blacklist.split(",") if s.strip()] if raw_blacklist else []

        return cls(
            symbols=symbols,
            exchanges=exchanges,
            credentials=credentials,
            starting_equity=_as_float(os.getenv("STARTING_EQUITY"), 10_000.0),
            trade_all_symbols=trade_all_symbols,
            max_symbols=_as_int(os.getenv("MAX_SYMBOLS"), 30),
            symbol_blacklist=blacklist,
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
                max_loss_per_trade_pct=_as_float(os.getenv("RISK_MAX_LOSS_PER_TRADE_PCT"), 0.10),
            ),
            execution=ExecutionConfig(
                order_timeout_ms=_as_int(os.getenv("EXEC_ORDER_TIMEOUT_MS"), 3000),
                hedge_retries=_as_int(os.getenv("EXEC_HEDGE_RETRIES"), 3),
                hedge_timeout_seconds=_as_float(os.getenv("EXEC_HEDGE_TIMEOUT_SECONDS"), 15.0),
                hedge_settle_seconds=_as_float(os.getenv("EXEC_HEDGE_SETTLE_SECONDS"), 0.3),
                cycle_interval_seconds=_as_float(os.getenv("EXEC_CYCLE_INTERVAL"), 0.5),
                dry_run=exec_dry_run,
                max_new_positions_per_cycle=_as_int(os.getenv("EXEC_MAX_NEW_POSITIONS_PER_CYCLE"), 1),
                use_maker_taker=_as_bool(os.getenv("EXEC_USE_MAKER_TAKER"), False),
                maker_timeout_ms=_as_int(os.getenv("EXEC_MAKER_TIMEOUT_MS"), 2000),
                maker_max_retries=_as_int(os.getenv("EXEC_MAKER_MAX_RETRIES"), 2),
                maker_price_offset_bps=_as_float(os.getenv("EXEC_MAKER_PRICE_OFFSET_BPS"), 0.5),
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
        if self.risk.max_strategy_allocation_pct > self.risk.max_total_exposure_pct:
            raise ValueError(
                f"max_strategy_allocation_pct ({self.risk.max_strategy_allocation_pct}) "
                f"must be <= max_total_exposure_pct ({self.risk.max_total_exposure_pct})"
            )
