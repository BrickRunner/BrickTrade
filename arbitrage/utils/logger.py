"""
Модуль логирования для арбитражного бота
"""
import logging
import sys
from pathlib import Path
from datetime import datetime


class ArbitrageLogger:
    """Класс для настройки и управления логированием арбитражного бота"""

    def __init__(self, log_dir: str = "logs", log_level: int = logging.INFO):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(exist_ok=True)
        self.log_level = log_level
        self.loggers = {}

    def get_logger(self, name: str) -> logging.Logger:
        """Получить или создать логгер с указанным именем"""
        if name in self.loggers:
            return self.loggers[name]

        logger = logging.getLogger(f"arbitrage.{name}")
        logger.setLevel(self.log_level)
        logger.propagate = False

        # Очистка существующих хендлеров
        if logger.handlers:
            logger.handlers.clear()

        # Формат логов
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )

        # Консольный хендлер
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(self.log_level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # Файловый хендлер (общий)
        file_handler = logging.FileHandler(
            self.log_dir / f"arbitrage_{datetime.now().strftime('%Y%m%d')}.log",
            encoding='utf-8'
        )
        file_handler.setLevel(self.log_level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Файловый хендлер для трейдов
        if 'execution' in name or 'arbitrage' in name:
            trade_handler = logging.FileHandler(
                self.log_dir / f"trades_{datetime.now().strftime('%Y%m%d')}.log",
                encoding='utf-8'
            )
            trade_handler.setLevel(logging.INFO)
            trade_handler.setFormatter(formatter)
            logger.addHandler(trade_handler)

        # Файловый хендлер для ошибок
        error_handler = logging.FileHandler(
            self.log_dir / f"errors_{datetime.now().strftime('%Y%m%d')}.log",
            encoding='utf-8'
        )
        error_handler.setLevel(logging.ERROR)
        error_handler.setFormatter(formatter)
        logger.addHandler(error_handler)

        self.loggers[name] = logger
        return logger


# Глобальный экземпляр логгера
_arbitrage_logger = None


def init_arbitrage_logger(log_dir: str = "logs", log_level: int = logging.INFO) -> ArbitrageLogger:
    """Инициализация глобального логгера арбитража"""
    global _arbitrage_logger
    _arbitrage_logger = ArbitrageLogger(log_dir, log_level)
    return _arbitrage_logger


def get_arbitrage_logger(name: str) -> logging.Logger:
    """Получить логгер по имени"""
    global _arbitrage_logger
    if _arbitrage_logger is None:
        _arbitrage_logger = ArbitrageLogger()
    return _arbitrage_logger.get_logger(name)
