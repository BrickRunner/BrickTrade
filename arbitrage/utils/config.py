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

    # Опциональные параметры
    okx_testnet: bool = False
    htx_testnet: bool = False

    # Торговые параметры
    symbol: str = "BTCUSDT"
    position_size: float = 0.01
    leverage: int = 3

    # Пороги входа/выхода
    entry_threshold: float = 0.25  # % спред для входа
    exit_threshold: float = 0.05   # % спред для выхода

    # Риск-менеджмент
    max_risk_per_trade: float = 0.01  # 1% депозита
    max_delta_percent: float = 0.001  # 0.1% баланса

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
    monitoring_only: bool = True  # По умолчанию только мониторинг!

    # Mock режим (безопасный режим без реальных денег)
    mock_mode: bool = True  # По умолчанию TRUE для безопасности!

    # DRY RUN режим (реальные API, но БЕЗ совершения сделок)
    # true = использует реальные биржи для данных, но НЕ размещает ордера
    # false = размещает реальные ордера (ОПАСНО!)
    dry_run_mode: bool = True  # По умолчанию TRUE для безопасности!

    # Multi-Pair настройки
    min_opportunity_lifetime: int = 3  # Минимальное время жизни возможности (сек)
    update_interval: int = 1  # Интервал обновления цен (сек)
    min_spread: float = 0.05  # Минимальный интересный спред (%)
    spread_change_threshold: float = 0.3  # Порог для уведомления об изменении (%)
    renotify_interval: int = 60  # Повторное уведомление если возможность держится N сек

    # Стратегия: Spot Arbitrage
    min_spot_profit: float = 0.1    # Минимальная чистая прибыль после комиссий (%)

    # Стратегия: Funding Rate Arbitrage
    min_funding_diff: float = 0.02  # Минимальная разница ставок финансирования (%)

    # Стратегия: Basis Arbitrage
    min_basis: float = 0.15         # Минимальный базис спот vs фьючерс (%)

    # Стратегия: Triangular Arbitrage
    min_triangular_profit: float = 0.05  # Минимальная чистая прибыль треугольника (%)

    # Включённые стратегии (через запятую в .env): spot,futures,funding,basis,triangular
    enabled_strategies: str = "futures,funding,basis"

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

            # Торговые параметры
            symbol=os.getenv("SYMBOL", "BTCUSDT"),
            position_size=float(os.getenv("POSITION_SIZE", "0.01")),
            leverage=int(os.getenv("LEVERAGE", "3")),

            # Пороги
            entry_threshold=float(os.getenv("ENTRY_THRESHOLD", "0.25")),
            exit_threshold=float(os.getenv("EXIT_THRESHOLD", "0.05")),

            # Риск
            max_risk_per_trade=float(os.getenv("MAX_RISK_PER_TRADE", "0.01")),
            max_delta_percent=float(os.getenv("MAX_DELTA_PERCENT", "0.001")),

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

            # Monitoring only mode (по умолчанию TRUE - только мониторинг!)
            monitoring_only=os.getenv("ARB_MONITORING_ONLY", "true").lower() == "true",

            # Mock mode (по умолчанию TRUE для безопасности!)
            mock_mode=os.getenv("ARB_MOCK_MODE", "true").lower() == "true",

            # DRY RUN mode (реальные API, но без сделок)
            dry_run_mode=os.getenv("ARB_DRY_RUN_MODE", "true").lower() == "true",

            # Multi-Pair настройки
            min_opportunity_lifetime=int(os.getenv("MIN_OPPORTUNITY_LIFETIME", "3")),
            update_interval=int(os.getenv("UPDATE_INTERVAL", "1")),
            min_spread=float(os.getenv("MIN_SPREAD", "0.05")),
            spread_change_threshold=float(os.getenv("SPREAD_CHANGE_THRESHOLD", "0.3")),
            renotify_interval=int(os.getenv("RENOTIFY_INTERVAL", "60")),

            # Стратегии
            min_spot_profit=float(os.getenv("MIN_SPOT_PROFIT", "0.1")),
            min_funding_diff=float(os.getenv("MIN_FUNDING_DIFF", "0.02")),
            min_basis=float(os.getenv("MIN_BASIS", "0.15")),
            min_triangular_profit=float(os.getenv("MIN_TRIANGULAR_PROFIT", "0.05")),
            enabled_strategies=os.getenv("ENABLED_STRATEGIES", "futures,funding,basis"),
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

    def validate(self) -> bool:
        """Проверить валидность конфигурации"""
        errors = []

        # В режиме monitoring_only требуются только OKX ключи
        if self.monitoring_only:
            # OKX ключи нужны для получения данных
            if not self.okx_api_key or not self.okx_api_secret or not self.okx_passphrase:
                errors.append("OKX credentials required in monitoring mode (set in .env)")
            # HTX ключи НЕ нужны в monitoring режиме - используется только публичное API
        # В mock/debug режиме API ключи не требуются
        elif not self.debug_mode and not self.mock_mode:
            # В режиме реальной торговли или dry_run нужны API ключи
            if not self.dry_run_mode:
                # Реальная торговля - нужны полные ключи
                if not self.okx_api_key or not self.okx_api_secret or not self.okx_passphrase:
                    errors.append("OKX credentials are missing (set ARB_MONITORING_ONLY=true or ARB_DRY_RUN_MODE=true)")

                if not self.htx_api_key or not self.htx_api_secret:
                    errors.append("HTX credentials are missing (set ARB_MONITORING_ONLY=true or ARB_DRY_RUN_MODE=true)")

        if self.position_size <= 0:
            errors.append("Position size must be positive")

        if self.leverage < 1 or self.leverage > 100:
            errors.append("Leverage must be between 1 and 100")

        if self.entry_threshold <= self.exit_threshold:
            errors.append("Entry threshold must be greater than exit threshold")

        if errors:
            raise ValueError(f"Configuration validation failed: {'; '.join(errors)}")

        return True
