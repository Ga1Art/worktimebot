from calendar import monthrange
from datetime import date

from bot.shared_text import format_expenses, format_period_stats, format_work_logs
from services.db import get_worker_expenses, get_worker_period_stats, get_worker_work_logs


def get_current_month_logs_text(worker_id: int) -> str:
    today = date.today()
    rows = get_worker_work_logs(worker_id, today.year, today.month)
    period_label = f"{today.month:02d}.{today.year}"
    return format_work_logs(rows, period_label)


def get_current_month_expenses_text(worker_id: int) -> str:
    today = date.today()
    rows = get_worker_expenses(worker_id, today.year, today.month)
    period_label = f"{today.month:02d}.{today.year}"
    return format_expenses(rows, period_label)


def get_half_month_stats_text(worker_id: int, half: str) -> str:
    today = date.today()
    days_in_month = monthrange(today.year, today.month)[1]

    if half == "first":
        day_from, day_to = 1, min(15, days_in_month)
        period_label = f"1-я половина {today.month:02d}.{today.year}"
    else:
        day_from, day_to = 16, days_in_month
        period_label = f"2-я половина {today.month:02d}.{today.year}"

    stats = get_worker_period_stats(worker_id, today.year, today.month, day_from, day_to)
    return format_period_stats(period_label, stats)
