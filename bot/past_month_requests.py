from datetime import date


def format_past_month_request(
    *,
    full_name: str,
    worker_id: int,
    request_date: date | str,
    entry_type: str,
    source_label: str,
    project_name: str | None = None,
    hours: float | None = None,
    expense_description: str | None = None,
    expense_amount: float | None = None,
) -> str:
    if isinstance(request_date, str):
        request_date = date.fromisoformat(request_date)

    type_labels = {
        "shift": "Смена",
        "install": "Монтаж",
        "expense": "Расход",
    }
    lines = [
        f"Запрос на внесение данных за прошлый месяц ({source_label}):",
        f"Сотрудник: {full_name} (ID: {worker_id})",
        f"Дата: {request_date:%d.%m.%Y}",
        f"Тип записи: {type_labels.get(entry_type, entry_type)}",
    ]

    if entry_type == "install" and project_name:
        lines.append(f"Проект: {project_name}")

    if entry_type in {"shift", "install"} and hours is not None:
        lines.append(f"Часы: {hours}")

    if entry_type == "expense":
        if expense_description:
            lines.append(f"Описание расхода: {expense_description}")
        if expense_amount is not None:
            lines.append(f"Сумма расхода: {expense_amount}")

    lines.extend(
        [
            "",
            "Нужно вручную перенести эту запись в архивный лист прошлого месяца.",
        ]
    )
    return "\n".join(lines)
