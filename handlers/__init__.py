"""
Модуль с обработчиками команд и callback-запросов бота
"""

from . import basic
from . import settings
from . import thresholds
from . import stats_handlers
from . import arbitrage_handlers_simple

__all__ = ['basic', 'settings', 'thresholds', 'stats_handlers', 'arbitrage_handlers_simple']
