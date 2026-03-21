from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _env_float(key: str, default: float = 0.0) -> float:
    v = _env(key)
    return float(v) if v else default


def _env_int(key: str, default: int = 0) -> int:
    v = _env(key)
    return int(v) if v else default


def _env_bool(key: str, default: bool = False) -> bool:
    v = _env(key).lower()
    if not v:
        return default
    return v in ("1", "true", "yes")


@dataclass(frozen=True)
class BcsCredentials:
    refresh_token: str
    client_id: str = "trade-api-write"


@dataclass(frozen=True)
class StockRiskConfig:
    max_total_exposure_pct: float = 1.0
    max_per_position_pct: float = 1.0
    max_daily_drawdown_pct: float = 0.03
    max_portfolio_drawdown_pct: float = 0.10
    max_open_positions: int = 5
    max_daily_trades: int = 20
    max_order_slippage_pct: float = 0.5
    kill_switch_enabled: bool = True
    trailing_stop_pct: float = 1.5  # trail SL by 1.5% from peak price
    default_sl_pct: float = 3.0     # default stop-loss if strategy doesn't set
    default_tp_pct: float = 4.5     # default take-profit if strategy doesn't set

    @classmethod
    def from_env(cls) -> StockRiskConfig:
        return cls(
            max_total_exposure_pct=_env_float("STOCK_RISK_MAX_EXPOSURE", 1.0),
            max_per_position_pct=_env_float("STOCK_RISK_MAX_PER_POSITION", 1.0),
            max_daily_drawdown_pct=_env_float("STOCK_RISK_MAX_DAILY_DD", 0.03),
            max_portfolio_drawdown_pct=_env_float("STOCK_RISK_MAX_PORTFOLIO_DD", 0.10),
            max_open_positions=_env_int("STOCK_RISK_MAX_POSITIONS", 5),
            max_daily_trades=_env_int("STOCK_RISK_MAX_DAILY_TRADES", 20),
            max_order_slippage_pct=_env_float("STOCK_RISK_MAX_SLIPPAGE", 0.5),
            kill_switch_enabled=_env_bool("STOCK_RISK_KILL_SWITCH", True),
            trailing_stop_pct=_env_float("STOCK_RISK_TRAILING_STOP", 1.5),
            default_sl_pct=_env_float("STOCK_RISK_DEFAULT_SL", 3.0),
            default_tp_pct=_env_float("STOCK_RISK_DEFAULT_TP", 4.5),
        )


@dataclass(frozen=True)
class StockExecutionConfig:
    order_timeout_ms: int = 5000
    cycle_interval_seconds: float = 5.0
    mode: str = "monitoring"            # "monitoring" | "semi_auto" | "auto"
    confirmation_timeout_sec: int = 60
    dry_run: bool = True

    @classmethod
    def from_env(cls) -> StockExecutionConfig:
        return cls(
            order_timeout_ms=_env_int("STOCK_ORDER_TIMEOUT_MS", 5000),
            cycle_interval_seconds=_env_float("STOCK_CYCLE_INTERVAL", 5.0),
            mode=_env("STOCK_MODE", "monitoring"),
            confirmation_timeout_sec=_env_int("STOCK_CONFIRMATION_TIMEOUT", 60),
            dry_run=_env_bool("STOCK_DRY_RUN", True),
        )


@dataclass(frozen=True)
class StockStrategyConfig:
    enabled: List[str] = field(default_factory=lambda: [
        "mean_reversion", "trend_following", "breakout",
        "volume_spike", "divergence", "rsi_reversal",
    ])
    # Mean Reversion
    mr_zscore_entry: float = 1.5
    mr_zscore_exit: float = 0.3
    mr_pairs: List[str] = field(default_factory=lambda: ["SBER:SBERP", "TATN:TATNP", "SNGS:SNGSP"])
    # Trend Following
    tf_ema_fast: int = 9
    tf_ema_slow: int = 21
    tf_adx_threshold: float = 15.0
    tf_atr_sl_mult: float = 2.0
    # Breakout
    bo_volume_multiplier: float = 1.2
    bo_atr_multiplier: float = 1.5
    bo_lookback: int = 20
    # Volume Spike
    vs_volume_threshold: float = 1.5
    vs_take_profit_pct: float = 0.5
    vs_stop_loss_pct: float = 0.3
    # Divergence
    div_rsi_period: int = 14
    div_lookback: int = 30
    # RSI Reversal
    rsi_oversold: float = 35.0
    rsi_overbought: float = 65.0
    rsi_adx_max: float = 30.0
    # Buffer
    candle_timeframe: str = "M5"
    candle_history_size: int = 200

    @classmethod
    def from_env(cls) -> StockStrategyConfig:
        _default_enabled = [
            "mean_reversion", "trend_following", "breakout",
            "volume_spike", "divergence", "rsi_reversal",
        ]
        enabled_raw = _env("STOCK_STRATEGIES", "")
        enabled = [s.strip() for s in enabled_raw.split(",") if s.strip()] if enabled_raw else _default_enabled
        pairs_raw = _env("STOCK_MR_PAIRS", "SBER:SBERP,TATN:TATNP,SNGS:SNGSP")
        pairs = [p.strip() for p in pairs_raw.split(",") if p.strip()]
        return cls(
            enabled=enabled,
            mr_zscore_entry=_env_float("STOCK_MR_ZSCORE_ENTRY", 1.5),
            mr_zscore_exit=_env_float("STOCK_MR_ZSCORE_EXIT", 0.3),
            mr_pairs=pairs,
            tf_ema_fast=_env_int("STOCK_TF_EMA_FAST", 9),
            tf_ema_slow=_env_int("STOCK_TF_EMA_SLOW", 21),
            tf_adx_threshold=_env_float("STOCK_TF_ADX_THRESHOLD", 15.0),
            tf_atr_sl_mult=_env_float("STOCK_TF_ATR_SL_MULT", 2.0),
            bo_volume_multiplier=_env_float("STOCK_BO_VOLUME_MULT", 1.2),
            bo_atr_multiplier=_env_float("STOCK_BO_ATR_MULT", 1.5),
            bo_lookback=_env_int("STOCK_BO_LOOKBACK", 20),
            vs_volume_threshold=_env_float("STOCK_VS_VOLUME_THRESHOLD", 1.5),
            vs_take_profit_pct=_env_float("STOCK_VS_TP_PCT", 0.5),
            vs_stop_loss_pct=_env_float("STOCK_VS_SL_PCT", 0.3),
            div_rsi_period=_env_int("STOCK_DIV_RSI_PERIOD", 14),
            div_lookback=_env_int("STOCK_DIV_LOOKBACK", 30),
            rsi_oversold=_env_float("STOCK_RSI_OVERSOLD", 35.0),
            rsi_overbought=_env_float("STOCK_RSI_OVERBOUGHT", 65.0),
            rsi_adx_max=_env_float("STOCK_RSI_ADX_MAX", 30.0),
            candle_timeframe=_env("STOCK_CANDLE_TIMEFRAME", "M5"),
            candle_history_size=_env_int("STOCK_CANDLE_HISTORY", 200),
        )


@dataclass(frozen=True)
class StockTradingConfig:
    tickers: List[str]
    class_code: str
    credentials: BcsCredentials
    starting_equity: float
    risk: StockRiskConfig = field(default_factory=StockRiskConfig)
    execution: StockExecutionConfig = field(default_factory=StockExecutionConfig)
    strategy: StockStrategyConfig = field(default_factory=StockStrategyConfig)

    @classmethod
    def from_env(cls) -> StockTradingConfig:
        raw_tickers = _env("STOCK_TICKERS", "SBER,GAZP,LKOH,SBERP,ROSN,GMKN,NVTK,YNDX,MTSS,MGNT,PLZL,SNGS,SNGSP,TATN,TATNP,VTBR,MOEX,NLMK,CHMF,ALRS,PHOR,IRAO,RUAL,POLY,AFLT")
        tickers = [t.strip() for t in raw_tickers.split(",") if t.strip()]

        refresh = _env("BCS_REFRESH_TOKEN")
        client_id = _env("BCS_CLIENT_ID", "")
        if not client_id:
            mode = _env("STOCK_MODE", "monitoring")
            client_id = "trade-api-read" if mode == "monitoring" else "trade-api-write"
        credentials = BcsCredentials(refresh_token=refresh, client_id=client_id)

        return cls(
            tickers=tickers,
            class_code=_env("STOCK_CLASS_CODE", "TQBR"),
            credentials=credentials,
            starting_equity=_env_float("STOCK_STARTING_EQUITY", 100_000),
            risk=StockRiskConfig.from_env(),
            execution=StockExecutionConfig.from_env(),
            strategy=StockStrategyConfig.from_env(),
        )

    def validate(self) -> None:
        if not self.tickers:
            raise ValueError("STOCK_TICKERS must contain at least one ticker")
        if not self.credentials.refresh_token:
            raise ValueError("BCS_REFRESH_TOKEN is required")
        if self.starting_equity <= 0:
            raise ValueError("STOCK_STARTING_EQUITY must be positive")
        if self.execution.mode not in ("monitoring", "semi_auto", "auto"):
            raise ValueError(f"Invalid STOCK_MODE: {self.execution.mode}")
