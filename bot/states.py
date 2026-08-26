from aiogram.fsm.state import StatesGroup, State


class WorkState(StatesGroup):
    choosing_action = State()
    waiting_date = State()
    waiting_place = State()

    waiting_project = State()
    waiting_hours = State()

    waiting_expense_type = State()
    waiting_expense_amount = State()
    waiting_expense_receipt = State()
    choosing_stats_period = State()

    admin_waiting_worker_id = State()
    admin_waiting_work_type = State()
    admin_waiting_rate = State()
    admin_waiting_amount = State()
    admin_waiting_description = State()
    admin_waiting_second_worker_id = State()
    admin_waiting_month = State()
    admin_waiting_project_id = State()
    admin_waiting_project_name = State()
    admin_confirm = State()

    waiting_past_month_date = State()
    waiting_past_month_type = State()
    waiting_past_month_project = State()
    waiting_past_month_hours = State()
    waiting_past_month_expense_description = State()
    waiting_past_month_expense_amount = State()
    confirm_past_month = State()

    waiting_standflow_task_comment = State()
    waiting_standflow_link_code = State()
    waiting_standflow_issue_description = State()
    waiting_standflow_issue_photo = State()

    confirm = State()
