def format_pending_users(rows, missing_chat_text=None):
    if not rows:
        return "Сейчас новых заявок на регистрацию нет."

    lines = ["Заявки на регистрацию:"]
    for worker_id, full_name, chat_id in rows:
        chat_text = chat_id
        if chat_text is None and missing_chat_text is not None:
            chat_text = missing_chat_text
        lines.append(f"{worker_id}. {full_name} (chat_id: {chat_text})")
    return "\n".join(lines)


def format_active_workers(rows):
    if not rows:
        return "Сейчас нет активных подтвержденных пользователей."

    lines = ["Активные пользователи:"]
    for worker_id, full_name in rows:
        lines.append(f"{worker_id}. {full_name}")
    return "\n".join(lines)


def format_pending_expenses(rows):
    if not rows:
        return "Сейчас нет расходов, ожидающих подтверждения."

    lines = ["Расходы на подтверждение:"]
    for expense_id, full_name, amount, description, expense_date in rows:
        lines.append(f"{expense_id}. {full_name} | {amount} | {description} | {expense_date}")
    return "\n".join(lines)


def format_adjustments(rows, period_label, title, empty_text):
    if not rows:
        return empty_text

    lines = [f"{title} за {period_label}:"]
    for adjustment_date, amount, description in rows:
        description_text = f" | {description}" if description else ""
        lines.append(f"{adjustment_date} | {amount}{description_text}")
    return "\n".join(lines)


def format_work_logs(rows, period_label):
    if not rows:
        return f"За {period_label} у вас пока нет рабочих записей."

    lines = [f"Ваши записи за {period_label}:"]
    for work_date, work_type, project_name, hours in rows:
        type_label = "Монтаж" if work_type == "install" else "Смена"
        line = f"{work_date} | {type_label} | {hours} ч."
        if project_name:
            line += f" | {project_name}"
        lines.append(line)
    return "\n".join(lines)


def format_expenses(rows, period_label):
    if not rows:
        return f"За {period_label} у вас пока нет расходов."

    status_map = {
        "pending": "ожидает подтверждения",
        "approved": "подтвержден",
        "rejected": "отклонен",
    }
    lines = [f"Ваши расходы за {period_label}:"]
    for expense_date, amount, description, status, receipt_path in rows:
        receipt_text = " | чек сохранен" if receipt_path else ""
        lines.append(
            f"{expense_date} | {amount} | {description} | {status_map.get(status, status)}{receipt_text}"
        )
    return "\n".join(lines)


def format_period_stats(period_label, stats):
    bonus_rows = stats.get("bonus_rows") or []
    penalty_rows = stats.get("penalty_rows") or []

    lines = [
        f"Статистика за {period_label}:",
        "",
        f"Смены: {stats['shift_count']} ({stats['shift_hours']} ч.) - {stats['shift_income']}",
        f"Монтажи: {stats['install_count']} ({stats['install_hours']} ч.) - {stats['install_income']}",
        f"Премии: {stats['bonuses']}",
    ]

    if bonus_rows:
        lines.append("Премии по датам:")
        for bonus_date, amount, description in bonus_rows:
            lines.append(f"- {bonus_date}: {amount} - {description}")

    lines.append(f"Штрафы: {stats['penalties']}")

    if penalty_rows:
        lines.append("Штрафы по датам:")
        for penalty_date, amount, description in penalty_rows:
            lines.append(f"- {penalty_date}: {amount} - {description}")

    lines.extend([
        f"Подтверждённые расходы: {stats['approved_expenses']}",
        f"Начислено за работу: {stats['work_income']}",
        f"Итого к выплате: {stats['total_income']}",
    ])
    return "\n".join(lines)


def format_worker_rates(worker, rates):
    worker_id, full_name, _, _, _, _, _ = worker
    shift_rate = rates.get("shift")
    install_rate = rates.get("install")

    def render(rate):
        if rate is None:
            return "не задана"
        return str(int(rate)) if isinstance(rate, float) and rate.is_integer() else str(rate)

    return (
        f"Ставки сотрудника {full_name} (ID: {worker_id}):\n\n"
        f"Смена (`shift`): {render(shift_rate)}\n"
        f"Монтаж (`install`): {render(install_rate)}"
    )


def format_expense_admin_text(full_name: str, expense_date, amount, description: str, has_receipt: bool, *, source_label=None, project_name=None):
    receipt_text = "приложен" if has_receipt else "без чека"
    project_text = f"Проект: {project_name}\n" if project_name else ""
    if source_label:
        return (
            f"Новый расход из {source_label}:\n"
            f"Сотрудник: {full_name}\n"
            f"Дата: {expense_date}\n"
            f"Сумма: {amount}\n"
            f"Описание: {description}\n"
            f"{project_text}"
            f"Чек: {receipt_text}"
        )
    return (
        f"Новый расход от {full_name}\n"
        f"Дата: {expense_date}\n"
        f"Сумма: {amount}\n"
        f"Описание: {description}\n"
        f"{project_text}"
        f"Чек: {receipt_text}"
    )


def admin_help_text():
    return (
        "Доступные команды администратора:\n\n"
        "/help — показать эту памятку\n"
        "/users — показать активных пользователей с ID\n"
        "/pending_users — показать заявки на регистрацию\n"
        "/past_requests — заявки на внесение данных за прошлый месяц\n"
        "/approve_user ID — одобрить заявку по ID\n"
        "/reject_user ID — отклонить заявку по ID\n"
        "/pending_expenses — показать расходы на подтверждение\n"
        "/approve_expense ID — подтвердить расход по ID\n"
        "/reject_expense ID — отклонить расход по ID\n"
        "/rates WORKER_ID — показать ставки сотрудника\n"
        "/set_rate WORKER_ID shift|install RATE — задать ставку в час\n"
        "/add_bonus WORKER_ID AMOUNT DESCRIPTION — добавить премию\n"
        "/add_penalty WORKER_ID AMOUNT DESCRIPTION — добавить штраф\n"
        "/bonuses WORKER_ID — показать премии за текущий месяц\n"
        "/penalties WORKER_ID — показать штрафы за текущий месяц\n"
        "/make_admin WORKER_ID — выдать права администратора\n"
        "/remove_admin WORKER_ID — снять права администратора\n"
        "/delete_worker WORKER_ID — полностью удалить сотрудника\n"
        "/merge_workers KEEP_ID DUPLICATE_ID — объединить дубли сотрудника\n"
        "/close_month — закрыть текущий месяц и обновить рабочий лист\n"
        "/close_month MM.YYYY — закрыть выбранный месяц\n"
        "/close_month YYYY-MM — тот же сценарий в альтернативном формате"
    )


def user_help_text():
    return (
        "Доступные команды:\n\n"
        "/start — открыть главное меню\n"
        "/help — показать эту справку\n"
        "/my_id — показать ваш ID\n"
        "/link_vk — получить код для привязки VK-аккаунта\n"
        "/my_logs — показать ваши рабочие записи за текущий месяц\n"
        "/my_expenses — показать ваши расходы за текущий месяц\n\n"
        "В главном меню можно:\n"
        "1. Внести новую информацию о смене, монтаже или расходе.\n"
        "2. Посмотреть сводную статистику за первую или вторую половину текущего месяца."
    )


def vk_help_text(is_admin_user: bool):
    lines = [
        "Доступные команды:",
        "",
        "/start — открыть бота и проверить доступ",
        "/help — показать эту справку",
        "/my_id — показать ваш ID",
        "/add — внести смену, монтаж или расход",
        "/stats — посмотреть статистику за половину месяца",
        "/my_logs — показать ваши рабочие записи за текущий месяц",
        "/my_expenses — показать ваши расходы за текущий месяц",
        "/cancel — отменить текущий ввод",
        "",
        "Основные действия можно делать кнопками.",
        "Если у вас уже есть Telegram-аккаунт, получите код через /link_vk в Telegram и отправьте его здесь после /start.",
    ]

    if is_admin_user:
        lines.extend(
            [
                "",
                "Команды администратора:",
                "/pending_users — показать заявки на регистрацию",
                "/past_requests — заявки на внесение данных за прошлый месяц",
                "/approve_user ID — одобрить заявку",
                "/reject_user ID — отклонить заявку",
                "/pending_expenses — показать расходы на подтверждение",
                "/approve_expense ID — подтвердить расход",
                "/reject_expense ID — отклонить расход",
                "/users — показать активных пользователей с ID",
                "/rates WORKER_ID — показать ставки сотрудника",
                "/set_rate WORKER_ID shift|install RATE — задать ставку в час",
                "/add_bonus WORKER_ID AMOUNT DESCRIPTION — добавить премию",
                "/add_penalty WORKER_ID AMOUNT DESCRIPTION — добавить штраф",
                "/bonuses WORKER_ID — показать премии за текущий месяц",
                "/penalties WORKER_ID — показать штрафы за текущий месяц",
                "/make_admin WORKER_ID — выдать права администратора",
                "/remove_admin WORKER_ID — снять права администратора",
                "/delete_worker WORKER_ID — полностью удалить сотрудника",
                "/merge_workers KEEP_ID DUPLICATE_ID — объединить дубли",
                "/close_month — закрыть текущий месяц",
                "/close_month MM.YYYY — закрыть выбранный месяц",
                "/close_month YYYY-MM — тот же сценарий в альтернативном формате",
            ]
        )

    return "\n".join(lines)
