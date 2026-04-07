"""
Модуль логирования для арбитражного бота.
Логи сохраняются в структуре: logs/YYYY-MM-DD/HH/filename.log

FIX M4: Now uses datetime.utcnow() instead of datetime.now() to avoid
DST transition issues during log rotation.
"""
import logging
import os
import sys
from pathlib import Path
from datetime import datetime, timezone


class HourlyRotatingFileHandler(logging.FileHandler):
    """Файловый хендлер, автоматически переключающийся на новую папку каждый час.

    Структура: {base_dir}/YYYY-MM-DD/HH/{filename}
    """

    def __init__(self, base_dir: str, filename: str, level=logging.NOTSET, encoding="utf-8"):
        self._base_dir = Path(base_dir)
        self._filename = filename
        self._encoding = encoding
        self._current_hour_key = None

        # Инициализируем с текущим путём
        path = self._resolve_path()
        super().__init__(path, mode="a", encoding=encoding)
        self.setLevel(level)

    def _resolve_path(self) -> str:
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%d/%H")

        target_dir = self._base_dir / now.strftime("%Y-%m-%d") / now.strftime("%H")
        target_dir.mkdir(parents=True, exist_ok=True)

        self._current_hour_key = hour_key
        return str(target_dir / self._filename)

    def emit(self, record):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%Y-%m-%d/%H")

        if hour_key != self._current_hour_key:
            # Час изменился — переключаемся на новый файл
            self.close()
            new_path = self._resolve_path()
            self.baseFilename = os.path.abspath(new_path)
            self.stream = self._open()

        super().emit(record)


class ArbitrageLogger:
    """Класс для настройки и управления логированием арбитражного бота"""

    def __init__(self, log_dir: str = "logs", log_level: int = logging.INFO):
        self.log_dir = log_dir
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

        # Файловый хендлер (общий) → logs/YYYY-MM-DD/HH/arbitrage.log
        file_handler = HourlyRotatingFileHandler(
            self.log_dir, "arbitrage.log", level=self.log_level
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # Файловый хендлер для трейдов → logs/YYYY-MM-DD/HH/trades.log
        if 'execution' in name or 'arbitrage' in name:
            trade_handler = HourlyRotatingFileHandler(
                self.log_dir, "trades.log", level=logging.INFO
            )
            trade_handler.setFormatter(formatter)
            logger.addHandler(trade_handler)

        # Файловый хендлер для ошибок → logs/YYYY-MM-DD/HH/errors.log
        error_handler = HourlyRotatingFileHandler(
            self.log_dir, "errors.log", level=logging.ERROR
        )
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
