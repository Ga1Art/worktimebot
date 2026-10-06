from datetime import date

from bot.shared_text import format_adjustments, format_worker_rates
from services.db import (
    add_bonus,
    add_penalty,
    add_project,
    approve_worker,
    deactivate_project,
    delete_worker,
    get_active_projects,
    get_active_projects_with_stats,
    get_worker_bonuses,
    get_worker_by_id,
    get_worker_contacts_by_id,
    get_expense_by_id,
    get_project_by_id,
    get_worker_penalties,
    get_worker_rates,
    merge_workers,
    reject_worker,
    set_worker_admin,
    set_worker_rate,
    update_expense_status,
)


def merge_workers_result(target_worker_id: int, source_worker_id: int) -> str:
    result = merge_workers(target_worker_id, source_worker_id)
    return (
        'Записи сотрудника объединены.\n'
        f"Основная запись: {result['target_worker_id']}\n"
        f"Удалённый дубль: {result['source_worker_id']}\n"
        f"Имя: {result['full_name']}\n"
        f"Статус: {result['registration_status']}"
    )


def get_worker_rates_result(worker_id: int):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None
    rates = get_worker_rates(worker_id)
    return format_worker_rates(worker, rates)


def set_worker_rate_result(worker_id: int, work_type: str, rate_value: float):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None

    set_worker_rate(worker_id, work_type, rate_value)
    rates = get_worker_rates(worker_id)
    return 'Ставка сохранена.\n\n' + format_worker_rates(worker, rates)


def add_bonus_result(worker_id: int, amount: float, description: str):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None

    bonus_id = add_bonus(worker_id, amount, description)
    _, full_name, _, _, _, _, _ = worker
    worker_contacts = get_worker_contacts_by_id(worker_id)
    _, _, chat_id, vk_id, _, _, _, _ = worker_contacts if worker_contacts else (None, None, None, None, None, None, None, None)
    return {
        "message_text": f'Премия добавлена.\n\nID записи: {bonus_id}\nСотрудник: {full_name}\nСумма: {amount}\nОписание: {description}',
        "notify_chat_id": chat_id,
        "notify_vk_id": vk_id,
        "notify_text": f'Вам начислена премия.\nСумма: {amount}\nОписание: {description}',
        "sync_year": date.today().year,
        "sync_month": date.today().month,
    }


def add_penalty_result(worker_id: int, amount: float, description: str):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None

    penalty_id = add_penalty(worker_id, amount, description)
    _, full_name, _, _, _, _, _ = worker
    worker_contacts = get_worker_contacts_by_id(worker_id)
    _, _, chat_id, vk_id, _, _, _, _ = worker_contacts if worker_contacts else (None, None, None, None, None, None, None, None)
    return {
        "message_text": f'Штраф добавлен.\n\nID записи: {penalty_id}\nСотрудник: {full_name}\nСумма: {amount}\nОписание: {description}',
        "notify_chat_id": chat_id,
        "notify_vk_id": vk_id,
        "notify_text": f'Вам назначен штраф.\nСумма: {amount}\nОписание: {description}',
        "sync_year": date.today().year,
        "sync_month": date.today().month,
    }


def get_worker_bonuses_result(worker_id: int):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None

    rows = get_worker_bonuses(worker_id)
    today = date.today()
    _, full_name, _, _, _, _, _ = worker
    return format_adjustments(
        rows,
        f"{today.month:02d}.{today.year}",
        f'Премии сотрудника {full_name} (ID: {worker_id})',
        f'За {today.month:02d}.{today.year} у сотрудника {full_name} нет премий.',
    )


def get_worker_penalties_result(worker_id: int):
    worker = get_worker_by_id(worker_id)
    if not worker:
        return None

    rows = get_worker_penalties(worker_id)
    today = date.today()
    _, full_name, _, _, _, _, _ = worker
    return format_adjustments(
        rows,
        f"{today.month:02d}.{today.year}",
        f'Штрафы сотрудника {full_name} (ID: {worker_id})',
        f'За {today.month:02d}.{today.year} у сотрудника {full_name} нет штрафов.',
    )


def set_admin_result(worker_id: int, is_admin_value: bool):
    result = set_worker_admin(worker_id, is_admin_value)
    if not result:
        return None

    _, full_name, chat_id, vk_id, current_admin = result
    action_text = 'назначен администратором' if current_admin else 'снят с роли администратора'
    notify_text = (
        'Вам выданы права администратора в боте.'
        if current_admin
        else 'Ваши права администратора в боте были сняты.'
    )
    return {
        "message_text": f'Пользователь {full_name} (ID: {worker_id}) {action_text}.',
        "notify_chat_id": chat_id,
        "notify_vk_id": vk_id,
        "notify_text": notify_text,
    }


def delete_worker_result(worker_id: int):
    result = delete_worker(worker_id)
    if not result:
        return None

    _, full_name, chat_id, vk_id, was_admin = result
    admin_text = ' У него были права администратора.' if was_admin else ""
    return {
        "message_text": f'Пользователь {full_name} (ID: {worker_id}) полностью удалён.{admin_text}',
        "notify_chat_id": chat_id,
        "notify_vk_id": vk_id,
        "notify_text": 'Ваш аккаунт в системе удалён. Если это ошибка, свяжитесь с администратором.',
    }


def get_active_projects_result():
    return get_active_projects()


def get_active_projects_text_result():
    rows = get_active_projects_with_stats()
    if not rows:
        return 'Сейчас нет активных проектов.'

    lines = ['Активные проекты:']
    for project_id, project_name, participants, hours, approved, pending, rejected in rows:
        lines.append(f"{project_id}. {project_name} | Монтажи: {hours} ч. | Участников: {participants} | Расходы: {approved} | Ожидают: {pending} | Отклонены: {rejected}")
    return "\n".join(lines)


def add_project_result(name: str, project_id: int | None = None):
    result = add_project(name, project_id)
    if not result:
        return None
    project_id, project_name = result
    return {
        "project_id": project_id,
        "project_name": project_name,
        "message_text": f'Проект «{project_name}» добавлен в активный список.',
    }


def delete_project_result(project_id: int):
    project = get_project_by_id(project_id)
    if not project:
        return None

    deleted = deactivate_project(project_id)
    if not deleted:
        return {"already_inactive": True, "project_name": project[1]}

    _, project_name = deleted
    return {
        "project_id": project_id,
        "project_name": project_name,
        "message_text": f'Проект «{project_name}» удалён из активного списка.',
    }


def approve_user_result(worker_id: int):
    worker = get_worker_contacts_by_id(worker_id)
    if not worker:
        return {"ok": False, "error": 'Пользователь не найден.'}

    _, full_name, chat_id, vk_id, registration_status, _, _, _ = worker
    if registration_status != "pending":
        return {"ok": False, "error": 'Эта заявка уже обработана.'}

    approve_worker(worker_id)
    return {
        "ok": True,
        "full_name": full_name,
        "chat_id": chat_id,
        "vk_id": vk_id,
        "notify_text": (
            f'{full_name}, ваша регистрация подтверждена.\n'
            'Теперь вы можете открыть бота и начать работу через /start.'
        ),
    }


def reject_user_result(worker_id: int):
    worker = get_worker_contacts_by_id(worker_id)
    if not worker:
        return {"ok": False, "error": 'Пользователь не найден.'}

    _, full_name, chat_id, vk_id, registration_status, _, _, _ = worker
    if registration_status != "pending":
        return {"ok": False, "error": 'Эта заявка уже обработана.'}

    reject_worker(worker_id)
    return {
        "ok": True,
        "full_name": full_name,
        "chat_id": chat_id,
        "vk_id": vk_id,
        "notify_text": (
            f'{full_name}, ваша заявка на регистрацию отклонена.\n'
            'Если это ошибка, свяжитесь с администратором.'
        ),
    }


def approve_expense_result(expense_id: int):
    expense = get_expense_by_id(expense_id)
    if not expense:
        return {"ok": False, "error": 'Расход не найден.'}

    worker = get_worker_contacts_by_id(expense[1])
    _, _, chat_id, vk_id, _, _, _, _ = worker if worker else (None, None, None, None, None, None, None, None)
    (
        _expense_id,
        _worker_id,
        _full_name,
        _worker_chat_id,
        amount,
        description,
        expense_date,
        status,
        _receipt_path,
        _receipt_original_name,
        _receipt_media_type,
        _receipt_source_file_id,
        _receipt_source_url,
        source_platform,
        source_peer_id,
    ) = expense
    if status != "pending":
        return {"ok": False, "error": 'Этот расход уже обработан.'}

    update_expense_status(expense_id, "approved")
    return {
        "ok": True,
        "chat_id": chat_id,
        "vk_id": vk_id,
        "source_platform": source_platform,
        "source_peer_id": source_peer_id,
        "expense_date": expense_date,
        "sync_year": expense_date.year,
        "sync_month": expense_date.month,
        "notify_text": (
            f'Ваш расход подтвержден.\n'
            f'Дата: {expense_date}\n'
            f'Сумма: {amount}\n'
            f'Описание: {description}'
        ),
    }


def reject_expense_result(expense_id: int):
    expense = get_expense_by_id(expense_id)
    if not expense:
        return {"ok": False, "error": 'Расход не найден.'}

    worker = get_worker_contacts_by_id(expense[1])
    _, _, chat_id, vk_id, _, _, _, _ = worker if worker else (None, None, None, None, None, None, None, None)
    (
        _expense_id,
        _worker_id,
        _full_name,
        _worker_chat_id,
        amount,
        description,
        expense_date,
        status,
        _receipt_path,
        _receipt_original_name,
        _receipt_media_type,
        _receipt_source_file_id,
        _receipt_source_url,
        source_platform,
        source_peer_id,
    ) = expense
    if status != "pending":
        return {"ok": False, "error": 'Этот расход уже обработан.'}

    update_expense_status(expense_id, "rejected")
    return {
        "ok": True,
        "chat_id": chat_id,
        "vk_id": vk_id,
        "source_platform": source_platform,
        "source_peer_id": source_peer_id,
        "notify_text": (
            f'Ваш расход отклонен.\n'
            f'Дата: {expense_date}\n'
            f'Сумма: {amount}\n'
            f'Описание: {description}'
        ),
    }


