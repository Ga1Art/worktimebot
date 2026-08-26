from calendar import monthrange
from collections import defaultdict
from decimal import Decimal

from services.db import get_connection


def _to_decimal(value):
    if value is None:
        return Decimal("0")
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def _format_decimal(value):
    normalized = _to_decimal(value).quantize(Decimal("0.01"))
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _format_day_entry(work_type: str, hours: Decimal, project_name: str | None = None):
    prefix = "М" if work_type == "install" else "С"
    part = f"{prefix}:{_format_decimal(hours)}"
    if project_name:
        part += f" {project_name}"
    return part


def _build_day_cell(entries):
    return "\n".join(
        _format_day_entry(work_type, hours, project_name)
        for work_type, hours, project_name in entries
    )


def _build_combined_day_cell(day_to_entries: dict[int, list], days: list[int]):
    parts = []
    for day in days:
        entries = day_to_entries.get(day, [])
        if not entries:
            continue
        rendered = _build_day_cell(entries)
        if len(days) == 1:
            parts.append(rendered)
        else:
            parts.append(f"{day}: {rendered}")
    return "\n".join(parts)


def _empty_half_stats():
    return {
        "shift_hours": Decimal("0"),
        "install_hours": Decimal("0"),
        "expenses": Decimal("0"),
        "bonuses": Decimal("0"),
        "penalties": Decimal("0"),
    }


def _half_key_for_day(day: int) -> str:
    return "first_half" if day <= 15 else "second_half"


def _build_half_summary(half_stats: dict, shift_rate: Decimal, install_rate: Decimal):
    shift_income = half_stats["shift_hours"] * shift_rate
    install_income = half_stats["install_hours"] * install_rate
    total_income = (
        shift_income
        + install_income
        + half_stats["expenses"]
        + half_stats["bonuses"]
        - half_stats["penalties"]
    )
    return {
        "shift_hours": half_stats["shift_hours"],
        "install_hours": half_stats["install_hours"],
        "expenses": half_stats["expenses"],
        "bonuses": half_stats["bonuses"],
        "penalties": half_stats["penalties"],
        "total_income": total_income,
    }


def _build_compact_day_columns(days_in_month: int):
    columns = []
    top_row_days = list(range(1, 16))
    for first_half_day in top_row_days:
        if first_half_day <= 14:
            second_half_day = first_half_day + 15
            label = f"{first_half_day} / {second_half_day}"
            mapped_days = [first_half_day, second_half_day]
        else:
            tail_days = [first_half_day]
            if days_in_month >= 30:
                tail_days.append(30)
            if days_in_month == 31:
                tail_days.append(31)
            label = "15 / 30-31" if days_in_month >= 30 else "15"
            mapped_days = tail_days
        columns.append({"label": label, "days": mapped_days})
    return columns


def build_monthly_report(year: int, month: int):
    days_in_month = monthrange(year, month)[1]
    compact_day_columns = _build_compact_day_columns(days_in_month)

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, full_name
        FROM workers
        WHERE is_approved = true
        ORDER BY full_name, id
        """
    )
    workers = cursor.fetchall()

    cursor.execute(
        """
        SELECT worker_id, work_date, work_type, project_name, hours
        FROM work_logs
        WHERE EXTRACT(YEAR FROM work_date) = %s
          AND EXTRACT(MONTH FROM work_date) = %s
        ORDER BY work_date, id
        """,
        (year, month),
    )
    work_logs = cursor.fetchall()

    cursor.execute(
        """
        SELECT worker_id, work_type, MAX(rate_per_hour)
        FROM rates
        GROUP BY worker_id, work_type
        """
    )
    rates = cursor.fetchall()

    cursor.execute(
        """
        SELECT worker_id, expense_date, amount
        FROM expenses
        WHERE EXTRACT(YEAR FROM expense_date) = %s
          AND EXTRACT(MONTH FROM expense_date) = %s
          AND status = 'approved'
        ORDER BY expense_date, id
        """,
        (year, month),
    )
    expenses = cursor.fetchall()

    cursor.execute(
        """
        SELECT worker_id, bonus_date, amount
        FROM bonuses
        WHERE EXTRACT(YEAR FROM bonus_date) = %s
          AND EXTRACT(MONTH FROM bonus_date) = %s
        ORDER BY bonus_date, id
        """,
        (year, month),
    )
    bonuses = cursor.fetchall()

    cursor.execute(
        """
        SELECT worker_id, penalty_date, amount
        FROM penalties
        WHERE EXTRACT(YEAR FROM penalty_date) = %s
          AND EXTRACT(MONTH FROM penalty_date) = %s
        ORDER BY penalty_date, id
        """,
        (year, month),
    )
    penalties = cursor.fetchall()

    cursor.close()
    conn.close()

    rates_map = {(worker_id, work_type): _to_decimal(rate) for worker_id, work_type, rate in rates}
    day_entries = defaultdict(lambda: defaultdict(list))
    worker_totals = defaultdict(_empty_half_stats)
    half_stats = defaultdict(
        lambda: {
            "first_half": _empty_half_stats(),
            "second_half": _empty_half_stats(),
        }
    )

    for worker_id, work_date, work_type, project_name, hours in work_logs:
        hour_value = _to_decimal(hours)
        day_entries[worker_id][work_date.day].append((work_type, hour_value, project_name))
        half_key = _half_key_for_day(work_date.day)
        if work_type == "shift":
            worker_totals[worker_id]["shift_hours"] += hour_value
            half_stats[worker_id][half_key]["shift_hours"] += hour_value
        elif work_type == "install":
            worker_totals[worker_id]["install_hours"] += hour_value
            half_stats[worker_id][half_key]["install_hours"] += hour_value

    for worker_id, expense_date, amount in expenses:
        amount_value = _to_decimal(amount)
        worker_totals[worker_id]["expenses"] += amount_value
        half_stats[worker_id][_half_key_for_day(expense_date.day)]["expenses"] += amount_value

    for worker_id, bonus_date, amount in bonuses:
        amount_value = _to_decimal(amount)
        worker_totals[worker_id]["bonuses"] += amount_value
        half_stats[worker_id][_half_key_for_day(bonus_date.day)]["bonuses"] += amount_value

    for worker_id, penalty_date, amount in penalties:
        amount_value = _to_decimal(amount)
        worker_totals[worker_id]["penalties"] += amount_value
        half_stats[worker_id][_half_key_for_day(penalty_date.day)]["penalties"] += amount_value

    columns = [
        "ID сотрудника",
        "Сотрудник",
        "Ставка за смену",
        "Ставка за монтаж",
        *[column["label"] for column in compact_day_columns],
        "Смены, ч",
        "Монтажи, ч",
        "Расходы",
        "Премии",
        "Штрафы",
        "Итого",
    ]

    rows = []
    sheet_rows = [columns]

    for worker_id, full_name in workers:
        shift_rate = rates_map.get((worker_id, "shift"), Decimal("0"))
        install_rate = rates_map.get((worker_id, "install"), Decimal("0"))
        first_half_summary = _build_half_summary(half_stats[worker_id]["first_half"], shift_rate, install_rate)
        second_half_summary = _build_half_summary(half_stats[worker_id]["second_half"], shift_rate, install_rate)

        top_day_cells = []
        bottom_day_cells = []
        for index, column in enumerate(compact_day_columns):
            if index <= 13:
                top_day_cells.append(_build_combined_day_cell(day_entries[worker_id], [column["days"][0]]))
                bottom_day_cells.append(
                    _build_combined_day_cell(day_entries[worker_id], column["days"][1:])
                )
            else:
                top_day_cells.append(_build_combined_day_cell(day_entries[worker_id], [15]))
                bottom_day_cells.append(
                    _build_combined_day_cell(day_entries[worker_id], [day for day in column["days"] if day != 15])
                )

        sheet_rows.append(
            [
                worker_id,
                full_name,
                _format_decimal(shift_rate),
                _format_decimal(install_rate),
                *top_day_cells,
                _format_decimal(first_half_summary["shift_hours"]),
                _format_decimal(first_half_summary["install_hours"]),
                _format_decimal(first_half_summary["expenses"]),
                _format_decimal(first_half_summary["bonuses"]),
                _format_decimal(first_half_summary["penalties"]),
                _format_decimal(first_half_summary["total_income"]),
            ]
        )
        sheet_rows.append(
            [
                "",
                "",
                "",
                "",
                *bottom_day_cells,
                _format_decimal(second_half_summary["shift_hours"]),
                _format_decimal(second_half_summary["install_hours"]),
                _format_decimal(second_half_summary["expenses"]),
                _format_decimal(second_half_summary["bonuses"]),
                _format_decimal(second_half_summary["penalties"]),
                _format_decimal(second_half_summary["total_income"]),
            ]
        )

        rows.append(
            {
                "worker_id": worker_id,
                "full_name": full_name,
                "shift_rate": float(shift_rate),
                "install_rate": float(install_rate),
                "first_half": {key: float(value) for key, value in first_half_summary.items()},
                "second_half": {key: float(value) for key, value in second_half_summary.items()},
                "top_row_days": {column["label"]: top_day_cells[idx] for idx, column in enumerate(compact_day_columns)},
                "bottom_row_days": {column["label"]: bottom_day_cells[idx] for idx, column in enumerate(compact_day_columns)},
            }
        )

    return {
        "year": year,
        "month": month,
        "sheet_title": f"{year}-{month:02d}",
        "columns": columns,
        "rows": rows,
        "sheet_rows": sheet_rows,
        "day_columns_count": len(compact_day_columns),
        "assumptions": [
            "Каждый сотрудник занимает две строки: верхняя для 1-15 числа, нижняя для 16-конца месяца.",
            "Последняя дневная колонка объединяет 30 и 31 число в одной ячейке, чтобы таблица оставалась компактной.",
            "Доход по половине месяца включает часы по ставкам, подтвержденные расходы и премии за вычетом штрафов.",
        ],
    }
