"""
Модуль управления рисками
"""
from typing import Optional, Tuple
from arbitrage.utils import get_arbitrage_logger, ArbitrageConfig
from arbitrage.core.state import BotState, Position

logger = get_arbitrage_logger("risk")


class RiskManager:
    """Менеджер рисков для арбитражного бота"""

    def __init__(self, config: ArbitrageConfig, state: BotState):
        self.config = config
        self.state = state

        # Лимиты риска
        self.max_risk_per_trade = config.max_risk_per_trade
        self.max_delta_percent = config.max_delta_percent
        self.max_position_size = config.position_size

    def can_enter_position(self, size: float, price: float) -> Tuple[bool, str]:
        """
        Проверить возможность входа в позицию

        Returns:
            (bool, str): (разрешено, причина отказа)
        """
        # Проверка: не в позиции ли мы уже
        if self.state.is_in_position:
            return False, "Already in position"

        # Проверка: достаточно ли баланса
        position_value = size * price
        required_balance = position_value * 2  # Нужно открыть 2 позиции

        if self.state.total_balance < required_balance:
            return False, f"Insufficient balance: need {required_balance:.2f}, have {self.state.total_balance:.2f}"

        # Проверка: размер позиции не превышает лимит
        if size > self.max_position_size:
            return False, f"Position size {size} exceeds max {self.max_position_size}"

        # Проверка: риск на трейд
        max_risk_value = self.state.total_balance * self.max_risk_per_trade
        if position_value > max_risk_value:
            return False, f"Position value {position_value:.2f} exceeds max risk {max_risk_value:.2f}"

        return True, "OK"

    def can_exit_position(self) -> Tuple[bool, str]:
        """
        Проверить возможность выхода из позиции

        Returns:
            (bool, str): (разрешено, причина отказа)
        """
        if not self.state.is_in_position:
            return False, "Not in position"

        if len(self.state.positions) == 0:
            return False, "No positions to close"

        return True, "OK"

    def check_delta(self) -> Tuple[bool, float]:
        """
        Проверить дельту позиций (hedge)

        Returns:
            (bool, float): (в пределах нормы, текущая дельта в %)
        """
        if not self.state.positions:
            return True, 0.0

        # Расчет суммарной позиции
        total_position = 0.0

        for pos in self.state.positions.values():
            if pos.side == "LONG":
                total_position += pos.size * pos.entry_price
            else:  # SHORT
                total_position -= pos.size * pos.entry_price

        # Дельта в процентах от баланса
        if self.state.total_balance > 0:
            delta_percent = abs(total_position) / self.state.total_balance
        else:
            delta_percent = 0.0

        is_ok = delta_percent < self.max_delta_percent

        if not is_ok:
            logger.warning(
                f"Delta check failed: {delta_percent*100:.3f}% > {self.max_delta_percent*100:.3f}%"
            )

        return is_ok, delta_percent

    def calculate_position_size(self, price: float) -> float:
        """
        Рассчитать размер позиции с учетом баланса и рисков

        Args:
            price: Текущая цена

        Returns:
            Размер позиции
        """
        # Используем фиксированный размер из конфига
        size = self.config.position_size

        # Проверяем, что размер не превышает допустимый риск
        position_value = size * price
        max_risk_value = self.state.total_balance * self.max_risk_per_trade

        if position_value > max_risk_value:
            # Уменьшаем размер до допустимого
            size = max_risk_value / price
            logger.info(f"Position size adjusted to {size} due to risk limits")

        return size

    def should_emergency_close(self) -> Tuple[bool, str]:
        """
        Проверить необходимость аварийного закрытия позиций

        Returns:
            (bool, str): (нужно закрывать, причина)
        """
        # Проверка дельты
        delta_ok, delta = self.check_delta()
        if not delta_ok:
            return True, f"Delta exceeded: {delta*100:.3f}%"

        # Проверка подключения к биржам
        if not self.state.is_both_connected():
            return True, "Connection lost to one or both exchanges"

        # Проверка баланса (если баланс упал слишком сильно)
        if self.state.total_balance < 10:  # Минимальный баланс
            return True, "Balance too low"

        return False, "OK"

    def validate_spread(self, spread: float, is_entry: bool) -> bool:
        """
        Проверить валидность спреда

        Args:
            spread: Спред в процентах
            is_entry: True для входа, False для выхода

        Returns:
            True если спред валидный
        """
        if is_entry:
            threshold = self.config.entry_threshold
            if spread >= threshold:
                logger.info(f"Entry spread {spread:.3f}% >= threshold {threshold:.3f}%")
                return True
            return False
        else:
            threshold = self.config.exit_threshold
            if abs(spread) <= threshold:
                logger.info(f"Exit spread {abs(spread):.3f}% <= threshold {threshold:.3f}%")
                return True
            return False

    def log_risk_status(self) -> None:
        """Логировать текущее состояние рисков"""
        delta_ok, delta = self.check_delta()

        logger.info(
            f"Risk Status: "
            f"in_position={self.state.is_in_position}, "
            f"positions={len(self.state.positions)}, "
            f"delta={delta*100:.3f}%, "
            f"balance={self.state.total_balance:.2f}, "
            f"connected={'both' if self.state.is_both_connected() else 'partial'}"
        )
