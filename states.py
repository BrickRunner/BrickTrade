from aiogram.fsm.state import State, StatesGroup


class DateForm(StatesGroup):
    """Конечный автомат для ввода даты"""
    waiting_for_date = State()


class InlineThresholdForm(StatesGroup):
    """Конечный автомат для ввода пороговых значений"""
    choosing_currency = State()
    entering_value = State()
    entering_comment_manual = State()
