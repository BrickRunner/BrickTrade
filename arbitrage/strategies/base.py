"""
Базовые классы и модели данных для всех арбитражных стратегий
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from enum import Enum


class StrategyType(Enum):
    SPOT_ARB = "spot_arb"
    FUTURES_ARB = "futures_arb"
    FUNDING_ARB = "funding_arb"
    BASIS_ARB = "basis_arb"
    TRIANGULAR = "triangular"

    @property
    def display_name(self) -> str:
        names = {
            "spot_arb": "Спот-арбитраж",
            "futures_arb": "Фьючерсный арбитраж",
            "funding_arb": "Funding Rate арбитраж",
            "basis_arb": "Basis арбитраж",
            "triangular": "Треугольный арбитраж",
        }
        return names.get(self.value, self.value)

    @property
    def emoji(self) -> str:
        emojis = {
            "spot_arb": "🔄",
            "futures_arb": "📊",
            "funding_arb": "💸",
            "basis_arb": "⚖️",
            "triangular": "🔺",
        }
        return emojis.get(self.value, "📈")


@dataclass
class BaseOpportunity:
    """Базовая модель арбитражной возможности"""
    strategy: StrategyType
    symbol: str
    profit_pct: float          # Ожидаемая прибыль в %
    timestamp: datetime = field(default_factory=datetime.utcnow)

    # Для отслеживания уведомлений (не поле датакласса — устанавливается динамически)
    # _last_notified: Optional[float] = None

    def is_profitable(self, min_profit: float = 0.05) -> bool:
        return self.profit_pct >= min_profit

    def age_seconds(self) -> float:
        return (datetime.utcnow() - self.timestamp).total_seconds()


@dataclass
class SpotArbitrageOpportunity(BaseOpportunity):
    """Возможность для классического спот-арбитража"""
    buy_exchange: str = ""
    sell_exchange: str = ""
    buy_price: float = 0.0        # Ask цена на бирже покупки
    sell_price: float = 0.0       # Bid цена на бирже продажи
    spread_raw: float = 0.0       # Сырой спред без комиссий
    estimated_fees: float = 0.002  # ~0.2% суммарные комиссии

    def net_profit(self) -> float:
        """Чистая прибыль после комиссий"""
        return self.spread_raw - self.estimated_fees * 100


@dataclass
class FuturesArbitrageOpportunity(BaseOpportunity):
    """Возможность для фьючерсного межбиржевого арбитража"""
    long_exchange: str = ""
    short_exchange: str = ""
    long_price: float = 0.0       # Ask на бирже лонга
    short_price: float = 0.0      # Bid на бирже шорта
    direction: str = ""            # "okx_long" или "bybit_long"


@dataclass
class FundingArbitrageOpportunity(BaseOpportunity):
    """Возможность для Funding Rate арбитража"""
    long_exchange: str = ""        # Биржа лонга (отрицательный funding — вам платят)
    short_exchange: str = ""       # Биржа шорта (положительный funding — вам платят)
    long_funding_rate: float = 0.0  # Ставка финансирования для лонга (8ч)
    short_funding_rate: float = 0.0 # Ставка финансирования для шорта (8ч)
    net_funding_8h: float = 0.0    # Чистый доход за 8 часов (%)
    next_funding_time: Optional[str] = None  # Время следующего начисления

    def annualized_return(self) -> float:
        """Годовая доходность (3 начисления в день × 365 дней)"""
        return self.net_funding_8h * 3 * 365


@dataclass
class BasisArbitrageOpportunity(BaseOpportunity):
    """Возможность для Basis арбитража (фьючерс vs спот)"""
    spot_exchange: str = ""
    futures_exchange: str = ""
    spot_price: float = 0.0
    futures_price: float = 0.0
    basis_pct: float = 0.0         # (futures - spot) / spot × 100
    direction: str = ""             # "cash_and_carry" или "reverse_cash_carry"

    def annualized_if_known_expiry(self, days_to_expiry: float) -> float:
        """Приблизительная годовая доходность если известна экспирация"""
        if days_to_expiry <= 0:
            return 0.0
        return self.basis_pct * (365 / days_to_expiry)


@dataclass
class TriangularArbitrageOpportunity(BaseOpportunity):
    """Возможность для треугольного арбитража"""
    exchange: str = ""
    path: list = field(default_factory=list)  # ["USDT", "BTC", "ETH", "USDT"]
    rates: list = field(default_factory=list)  # Курсы обмена на каждом шаге
    gross_profit_pct: float = 0.0  # До вычета комиссий
    estimated_fees: float = 0.003  # ~0.3% суммарные комиссии (3 трейда)

    def net_profit(self) -> float:
        return self.gross_profit_pct - self.estimated_fees * 100

    def path_str(self) -> str:
        return " → ".join(self.path)
