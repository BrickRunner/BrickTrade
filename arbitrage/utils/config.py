"""
Конфигурация арбитражного бота
"""
import os
from typing import Optional
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class ExchangeConfig:
    """Конфигурация биржи"""
    api_key: str
    api_secret: str
    passphrase: Optional[str] = None
    testnet: bool = False


@dataclass
class ArbitrageConfig:
    """Конфигурация арбитражного бота"""

    # OKX настройки (обязательные только для торговли)
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_passphrase: str = ""

    # HTX настройки (опциональные — для мониторинга используются публичные API)
    htx_api_key: str = ""
    htx_api_secret: str = ""

    # Bybit настройки (опциональные — публичные API для мониторинга)
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    # Опциональные параметры
    okx_testnet: bool = False
    htx_testnet: bool = False
    bybit_testnet: bool = False

    # Торговые параметры
    symbol: str = "BTCUSDT"
    position_size: float = 0.01
    leverage: int = 1  # 1 = без плеча

    # Пороги входа/выхода
    entry_threshold: float = 0.5   # % спред для входа
    exit_threshold: float = 0.1    # % спред для выхода

    # Риск-менеджмент
    max_position_pct: float = 0.30   # 30% депозита на сделку
    max_risk_per_trade: float = 0.30  # 30% депозита
    max_delta_percent: float = 0.01   # 1% баланса

    # Кулдаун пары после сделки (секунды)
    pair_cooldown_seconds: int = 300  # 5 минут

    # Исполнение ордеров
    order_timeout_ms: int = 200  # Таймаут на исполнение ордера

    # WebSocket
    ws_ping_interval: int = 20  # Интервал ping для WS
    ws_ping_timeout: int = 10   # Таймаут ping для WS

    # Прочее
    log_level: str = "INFO"
    log_dir: str = "logs"

    # Режим отладки (ВАЖНО: в mock режиме НЕ используются реальные биржи)
    debug_mode: bool = False

    # РЕЖИМ РАБОТЫ БОТА:
    # 1. monitoring_only = true: Только мониторинг через публичные API (БЕЗ торговли, БЕЗ личных ключей)
    # 2. monitoring_only = false + mock_mode = true: Mock режим для разработки
    # 3. monitoring_only = false + dry_run_mode = true: Реальные данные, но без ордеров
    monitoring_only: bool = False  # Полный режим с реальными API

    # Mock режим
    mock_mode: bool = False  # Реальные API

    # DRY RUN режим
    # true = реальные данные, но НЕ размещает ордера
    # false = РЕАЛЬНАЯ ТОРГОВЛЯ
    dry_run_mode: bool = False  # Реальная торговля

    # Multi-Pair настройки
    min_opportunity_lifetime: int = 3  # Минимальное время жизни возможности (сек)
    update_interval: float = 0.5  # Интервал обновления цен (сек)
    min_spread: float = 0.05  # Минимальный интересный спред (%)
    spread_change_threshold: float = 0.3  # Порог для уведомления об изменении (%)
    renotify_interval: int = 60  # Повторное уведомление если возможность держится N сек

    # Стратегия: Spot Arbitrage
    min_spot_profit: float = 0.1    # Минимальная чистая прибыль после комиссий (%)

    # Стратегия: Funding Rate Arbitrage
    min_funding_diff: float = 0.02  # Минимальная разница ставок финансирования (%)
    funding_btc_threshold: float = 0.02  # BTC funding threshold (%)
    funding_eth_threshold: float = 0.03  # ETH funding threshold (%)
    funding_alt_threshold: float = 0.05  # ALT funding threshold (%)
    funding_target_profit: float = 0.10  # Target profit to exit (%)

    # Стратегия: Basis Arbitrage
    min_basis: float = 0.15         # Минимальный базис спот vs фьючерс (%)
    basis_close_threshold: float = 0.05  # Close when basis < this (%)

    # Стратегия: Statistical Arbitrage
    stat_arb_z_entry: float = 2.5   # Z-score entry threshold
    stat_arb_z_exit: float = 0.5    # Z-score exit threshold
    stat_arb_window: int = 500      # Rolling window size (samples)

    # Стратегия: Triangular Arbitrage
    min_triangular_profit: float = 0.05  # Минимальная чистая прибыль треугольника (%)

    # Включённые стратегии (через запятую в .env): funding,basis,stat_arb
    enabled_strategies: str = "funding,basis,stat_arb"

    # Position limits
    max_concurrent_positions: int = 3
    emergency_margin_ratio: float = 0.1  # Close all if margin ratio < this

    @classmethod
    def from_env(cls) -> "ArbitrageConfig":
        """Создать конфигурацию из переменных окружения"""
        return cls(
            # OKX
            okx_api_key=os.getenv("OKX_API_KEY", ""),
            okx_api_secret=os.getenv("OKX_SECRET", ""),
            okx_passphrase=os.getenv("OKX_PASSPHRASE", ""),
            okx_testnet=os.getenv("OKX_TESTNET", "false").lower() == "true",

            # HTX
            htx_api_key=os.getenv("HTX_API_KEY", ""),
            htx_api_secret=os.getenv("HTX_SECRET", ""),
            htx_testnet=os.getenv("HTX_TESTNET", "false").lower() == "true",

            # Bybit
            bybit_api_key=os.getenv("BYBIT_API_KEY", ""),
            bybit_api_secret=os.getenv("BYBIT_SECRET", ""),
            bybit_testnet=os.getenv("BYBIT_TESTNET", "false").lower() == "true",

            # Торговые параметры
            symbol=os.getenv("SYMBOL", "BTCUSDT"),
            position_size=float(os.getenv("POSITION_SIZE", "0.01")),
            leverage=int(os.getenv("LEVERAGE", "1")),

            # Пороги
            entry_threshold=float(os.getenv("ENTRY_THRESHOLD", "0.5")),
            exit_threshold=float(os.getenv("EXIT_THRESHOLD", "0.1")),

            # Риск
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.30")),
            max_risk_per_trade=float(os.getenv("MAX_RISK_PER_TRADE", "0.30")),
            max_delta_percent=float(os.getenv("MAX_DELTA_PERCENT", "0.01")),
            pair_cooldown_seconds=int(os.getenv("PAIR_COOLDOWN", "300")),

            # Исполнение
            order_timeout_ms=int(os.getenv("ORDER_TIMEOUT_MS", "200")),

            # WS
            ws_ping_interval=int(os.getenv("WS_PING_INTERVAL", "20")),
            ws_ping_timeout=int(os.getenv("WS_PING_TIMEOUT", "10")),

            # Прочее
            log_level=os.getenv("ARB_LOG_LEVEL", "INFO"),
            log_dir=os.getenv("ARB_LOG_DIR", "logs"),

            # Debug
            debug_mode=os.getenv("ARB_DEBUG_MODE", "false").lower() == "true",

            # Monitoring only mode
            monitoring_only=os.getenv("ARB_MONITORING_ONLY", "false").lower() == "true",

            # Mock mode
            mock_mode=os.getenv("ARB_MOCK_MODE", "false").lower() == "true",

            # DRY RUN mode
            dry_run_mode=os.getenv("ARB_DRY_RUN_MODE", "false").lower() == "true",

            # Multi-Pair настройки
            min_opportunity_lifetime=int(os.getenv("MIN_OPPORTUNITY_LIFETIME", "3")),
            update_interval=float(os.getenv("UPDATE_INTERVAL", "0.5")),
            min_spread=float(os.getenv("MIN_SPREAD", "0.05")),
            spread_change_threshold=float(os.getenv("SPREAD_CHANGE_THRESHOLD", "0.3")),
            renotify_interval=int(os.getenv("RENOTIFY_INTERVAL", "60")),

            # Стратегии
            min_spot_profit=float(os.getenv("MIN_SPOT_PROFIT", "0.1")),
            min_funding_diff=float(os.getenv("MIN_FUNDING_DIFF", "0.02")),
            funding_btc_threshold=float(os.getenv("FUNDING_BTC_THRESHOLD", "0.02")),
            funding_eth_threshold=float(os.getenv("FUNDING_ETH_THRESHOLD", "0.03")),
            funding_alt_threshold=float(os.getenv("FUNDING_ALT_THRESHOLD", "0.05")),
            funding_target_profit=float(os.getenv("FUNDING_TARGET_PROFIT", "0.10")),
            min_basis=float(os.getenv("MIN_BASIS", "0.15")),
            basis_close_threshold=float(os.getenv("BASIS_CLOSE_THRESHOLD", "0.05")),
            stat_arb_z_entry=float(os.getenv("STAT_ARB_Z_ENTRY", "2.5")),
            stat_arb_z_exit=float(os.getenv("STAT_ARB_Z_EXIT", "0.5")),
            stat_arb_window=int(os.getenv("STAT_ARB_WINDOW", "500")),
            min_triangular_profit=float(os.getenv("MIN_TRIANGULAR_PROFIT", "0.05")),
            enabled_strategies=os.getenv("ENABLED_STRATEGIES", "funding,basis,stat_arb"),
            max_concurrent_positions=int(os.getenv("MAX_CONCURRENT_POSITIONS", "3")),
            emergency_margin_ratio=float(os.getenv("EMERGENCY_MARGIN_RATIO", "0.1")),
        )

    def get_okx_config(self) -> ExchangeConfig:
        """Получить конфигурацию OKX"""
        return ExchangeConfig(
            api_key=self.okx_api_key,
            api_secret=self.okx_api_secret,
            passphrase=self.okx_passphrase,
            testnet=self.okx_testnet
        )

    def get_htx_config(self) -> ExchangeConfig:
        """Получить конфигурацию HTX"""
        return ExchangeConfig(
            api_key=self.htx_api_key,
            api_secret=self.htx_api_secret,
            testnet=self.htx_testnet
        )

    def get_bybit_config(self) -> ExchangeConfig:
        """Получить конфигурацию Bybit"""
        return ExchangeConfig(
            api_key=self.bybit_api_key,
            api_secret=self.bybit_api_secret,
            testnet=self.bybit_testnet
        )

    def validate(self) -> bool:
        """Проверить валидность конфигурации"""
        import logging
        _logger = logging.getLogger(__name__)
        errors = []

        # OKX ключи нужны всегда (для данных и торговли)
        if not self.okx_api_key or not self.okx_api_secret or not self.okx_passphrase:
            errors.append("OKX credentials required (set in .env)")

        # HTX ключи нужны только для реальной торговли
        if not self.dry_run_mode and not self.monitoring_only and not self.mock_mode:
            if not self.htx_api_key or not self.htx_api_secret:
                _logger.warning("HTX API keys not configured - trading disabled, only monitoring")
                # НЕ добавляем в errors - бот запустится в режиме мониторинга

        if self.position_size <= 0:
            errors.append("Position size must be positive")

        if self.leverage < 1 or self.leverage > 100:
            errors.append("Leverage must be between 1 and 100")

        if self.entry_threshold <= self.exit_threshold:
            errors.append("Entry threshold must be greater than exit threshold")

        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")

        return True
