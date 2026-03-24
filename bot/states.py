from aiogram.fsm.state import StatesGroup, State


class WorkState(StatesGroup):
    waiting_date = State()
    waiting_place = State()

    waiting_project = State()
    waiting_hours = State()

    waiting_expense_type = State()
    waiting_expense_amount = State()

    confirm = State()