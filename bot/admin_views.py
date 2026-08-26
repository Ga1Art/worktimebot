from bot.pending_views import paginate_rows


ADMIN_WORKER_PROMPTS = {
    "show_rates": "Выберите сотрудника, чтобы посмотреть его ставки.",
    "set_rate": "Выберите сотрудника, которому нужно задать ставку.",
    "add_bonus": "Выберите сотрудника, которому нужно назначить премию.",
    "add_penalty": "Выберите сотрудника, которому нужно назначить штраф.",
    "show_bonuses": "Выберите сотрудника, чтобы показать его премии за текущий месяц.",
    "show_penalties": "Выберите сотрудника, чтобы показать его штрафы за текущий месяц.",
    "merge_workers": "Выберите основную запись сотрудника, которую нужно оставить.",
    "merge_source": "Выберите запись-дубль, которую нужно влить в основную.",
    "make_admin": "Выберите сотрудника, которому нужно выдать права администратора.",
    "remove_admin": "Выберите сотрудника, у которого нужно снять права администратора.",
    "delete_worker": "Выберите сотрудника, которого нужно полностью удалить.",
}

ADMIN_WORKER_CALLBACK_ACTIONS = {
    "admin_rates": "show_rates",
    "admin_set_rate": "set_rate",
    "admin_add_bonus": "add_bonus",
    "admin_add_penalty": "add_penalty",
    "admin_show_bonuses": "show_bonuses",
    "admin_show_penalties": "show_penalties",
    "admin_merge": "merge_workers",
    "admin_make_admin": "make_admin",
    "admin_remove_admin": "remove_admin",
    "admin_delete_worker": "delete_worker",
}


def get_admin_worker_prompt(action: str) -> str:
    return ADMIN_WORKER_PROMPTS.get(action, "Выберите сотрудника.")


def get_admin_worker_action_from_callback(callback_data: str):
    return ADMIN_WORKER_CALLBACK_ACTIONS.get(callback_data)


def build_worker_picker_page(rows, page: int = 0, page_size: int = 8):
    return paginate_rows(rows, page=page, page_size=page_size)


def get_admin_work_type_label(work_type: str) -> str:
    return "Смена" if work_type == "shift" else "Монтаж"


def get_admin_adjustment_amount_label(action: str) -> str:
    return "премии" if action == "add_bonus" else "штрафа"


def get_admin_adjustment_confirmation_label(action: str) -> str:
    return "премию" if action == "add_bonus" else "штраф"


def get_admin_adjustment_confirmation_title(action: str) -> str:
    if action == "add_bonus":
        return "Проверьте данные перед назначением премии:"
    return "Проверьте данные перед назначением штрафа:"


def build_admin_confirmation_text(
    data: dict,
    title: str,
    worker_name: str | None = None,
    footer: str | None = None,
) -> str:
    worker_id = data["worker_id"]
    if worker_name:
        text_lines = [title, f"Сотрудник: {worker_name} (ID: {worker_id})"]
    else:
        text_lines = [title, f"Сотрудник ID: {worker_id}"]

    if data.get("work_type"):
        text_lines.append(f"Тип работ: {get_admin_work_type_label(data['work_type'])}")
    if data.get("rate_value") is not None:
        text_lines.append(f"Ставка: {data['rate_value']}")
    if data.get("amount") is not None:
        text_lines.append(f"Сумма: {data['amount']}")
    if data.get("description"):
        text_lines.append(f"Описание: {data['description']}")
    if footer:
        text_lines.extend(["", footer])
    return "\n".join(text_lines)
