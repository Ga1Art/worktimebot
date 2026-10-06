from datetime import date
import json


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

    if entry_type in {"install", "expense"} and project_name:
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
            "Одобрите или отклоните заявку. После одобрения запись попадёт в базу и архивный отчёт.",
        ]
    )
    return "\n".join(lines)


def format_saved_request(request):
    return f"Заявка №{request['id']}\n" + format_past_month_request(
        full_name=request['full_name'], worker_id=request['worker_id'],
        request_date=request['request_date'], entry_type=request['entry_type'],
        source_label='Telegram' if request['source_platform'] == 'telegram' else 'VK',
        project_name=request['project_name'], hours=request['hours'],
        expense_description=request['description'], expense_amount=request['amount'],
    )


def telegram_request_keyboard(request_id):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text='Одобрить', callback_data=f'past_approve_{request_id}'),
        InlineKeyboardButton(text='Отклонить', callback_data=f'past_reject_{request_id}'),
    ]])


def vk_request_keyboard(request_id):
    return json.dumps({'inline': True, 'buttons': [[
        {'action': {'type': 'text', 'label': f'Одобрить заявку #{request_id}'}, 'color': 'positive'},
        {'action': {'type': 'text', 'label': f'Отклонить заявку #{request_id}'}, 'color': 'negative'},
    ]]}, ensure_ascii=False)


async def process_request_decision(request_id, approve, decided_by):
    import logging
    from bot.async_utils import run_sync
    from bot.core.notifications import notify_expense_origin
    from services.db import get_worker_contacts_by_id
    from services.past_month_requests import decide_request, sync_request_archive
    result = await run_sync(decide_request, request_id, approve, decided_by)
    request = result['request']
    archive_synced = not request['archive_sync_pending']
    if request['status'] == 'approved' and request['archive_sync_pending']:
        try:
            archive_synced = await run_sync(sync_request_archive, request['request_date'].year, request['request_date'].month)
        except Exception:
            logging.exception('Historical entry saved, archive update queued')
            archive_synced = False
    outcome = 'одобрена' if request['status'] == 'approved' else 'отклонена'
    if result['changed']:
        worker = await run_sync(get_worker_contacts_by_id, request['worker_id'])
        await notify_expense_origin(request['source_platform'], request['source_peer_id'],
                                    worker[2] if worker else None, worker[3] if worker else None,
                                    f"Ваша заявка №{request_id} за {request['request_date']:%d.%m.%Y} {outcome}.")
    text = f"Заявка №{request_id} {'уже ' if not result['changed'] else ''}{outcome}."
    if request['status'] == 'approved':
        text += '\nЗапись сохранена в базе. '
        text += 'Архивный отчёт обновлён.' if archive_synced else 'Обновление архива ожидает автоматического повтора.'
    return text
