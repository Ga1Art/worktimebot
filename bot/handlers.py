import asyncio
import logging
import uuid
from calendar import monthrange
from datetime import date
from io import BytesIO
from types import SimpleNamespace

from aiogram import Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import FSInputFile
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.admin_service import (
    add_bonus_result,
    add_penalty_result,
    add_project_result,
    approve_expense_result,
    approve_user_result,
    delete_project_result,
    delete_worker_result,
    get_active_projects_result,
    get_active_projects_text_result,
    get_worker_bonuses_result,
    get_worker_penalties_result,
    get_worker_rates_result,
    merge_workers_result,
    reject_expense_result,
    reject_user_result,
    set_admin_result,
    set_worker_rate_result,
)
from bot.async_utils import run_sync
from bot.admin_views import (
    build_worker_picker_page,
    get_admin_worker_action_from_callback,
    get_admin_worker_prompt,
)
from bot.pending_views import build_pending_expenses_overview_text, build_pending_users_overview_text
from bot.past_month_requests import (
    format_saved_request, telegram_request_keyboard, vk_request_keyboard, process_request_decision,
)
from services.past_month_requests import create_request, list_pending_requests
from bot.shared_text import (
    admin_help_text,
    format_active_workers,
    format_expense_admin_text,
    format_pending_expenses,
    format_pending_users,
    user_help_text,
)
from bot.standflow_client import (
    StandFlowError,
    add_task_comment,
    complete_task,
    confirm_telegram_link,
    create_telegram_link_code,
    get_daily_summary,
    get_reminders,
    get_task_cards,
    import_work_log,
    list_active_projects,
    list_telegram_link_candidates,
    report_issue,
)
from bot.states import WorkState
from bot.user_service import (
    get_current_month_expenses_text,
    get_current_month_logs_text,
    get_half_month_stats_text,
)
from config import ADMIN_CHAT_ID
from services.db import (
    clear_conversation_state,
    create_worker,
    generate_account_link_code,
    get_active_projects,
    get_admin_vk_ids,
    clear_pending_inbound_message,
    get_expense_by_id,
    get_active_workers,
    list_active_conversation_states,
    list_conversations_needing_reply,
    load_conversation_state,
    get_pending_inbound_message,
    get_pending_expenses,
    get_pending_workers,
    get_project_by_id,
    get_worker,
    get_worker_by_id,
    get_worker_contacts_by_id,
    is_admin,
    save_to_db,
    save_conversation_state,
    update_expense_receipt,
)
from services.yougile import sync_projects
from bot.core.notifications import (
    notify_expense_origin,
    notify_worker,
    send_telegram_media,
    send_telegram_message,
    send_vk_media,
    send_vk_message,
)
from services.google_sheets import google_error_message, sync_monthly_report_to_current_sheet, sync_projects_reference_sheet
from services.monthly_closing import close_month_tracked, parse_previous_month_date
from services.receipts import save_remote_receipt, save_telegram_receipt

router = Router()
logger = logging.getLogger(__name__)
_sheet_sync_tasks: dict[tuple[int, int], asyncio.Task] = {}


def is_private(message: Message):
    return message.chat.type == "private"


def is_admin_chat(message: Message):
    return message.chat.id == ADMIN_CHAT_ID


def is_admin_message(message: Message):
    return (is_admin_chat(message) or is_private(message)) and is_admin(message.from_user.id)


def is_admin_callback(callback: CallbackQuery):
    return (callback.message.chat.id == ADMIN_CHAT_ID or callback.message.chat.type == "private") and is_admin(callback.from_user.id)


def confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Подтвердить', callback_data="confirm_yes"),
            InlineKeyboardButton(text='Отменить', callback_data="confirm_no"),
        ]
    ])


def expense_receipt_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Продолжить без чека', callback_data="expense_skip_receipt"),
        ]
    ])


def register_keyboard(worker_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Одобрить', callback_data=f"approve_user_{worker_id}"),
            InlineKeyboardButton(text='Отклонить', callback_data=f"reject_user_{worker_id}"),
        ]
    ])


def expense_keyboard(expense_id):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Одобрить', callback_data=f"approve_expense_{expense_id}"),
            InlineKeyboardButton(text='Отклонить', callback_data=f"reject_expense_{expense_id}"),
        ]
        ,
        [
            InlineKeyboardButton(text='Показать чек', callback_data=f"show_expense_receipt_{expense_id}"),
        ]
    ])


def main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Внести информацию", callback_data="menu_add"),
            InlineKeyboardButton(text="Посмотреть статистику", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton(text="Мои записи", callback_data="menu_logs"),
            InlineKeyboardButton(text="Мои расходы", callback_data="menu_expenses"),
        ],
        [
            InlineKeyboardButton(text="Мой ID", callback_data="menu_my_id"),
            InlineKeyboardButton(text="Привязать VK", callback_data="menu_link_vk"),
        ],
        [
            InlineKeyboardButton(text="За прошлый месяц", callback_data="menu_past_month"),
        ],
        [
            InlineKeyboardButton(text="StandFlow", callback_data="menu_standflow"),
        ],
    ])

def admin_main_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Внести информацию", callback_data="menu_add"),
            InlineKeyboardButton(text="Посмотреть статистику", callback_data="menu_stats"),
        ],
        [
            InlineKeyboardButton(text="Мои записи", callback_data="menu_logs"),
            InlineKeyboardButton(text="Мои расходы", callback_data="menu_expenses"),
        ],
        [
            InlineKeyboardButton(text="Мой ID", callback_data="menu_my_id"),
            InlineKeyboardButton(text="Привязать VK", callback_data="menu_link_vk"),
        ],
        [
            InlineKeyboardButton(text="За прошлый месяц", callback_data="menu_past_month"),
        ],
        [
            InlineKeyboardButton(text="StandFlow", callback_data="menu_standflow"),
        ],
        [
            InlineKeyboardButton(text="Админ-панель", callback_data="menu_admin"),
        ],
    ])

def admin_panel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Пользователи', callback_data="admin_users"),
            InlineKeyboardButton(text='Заявки', callback_data="admin_pending_users"),
        ],
        [
            InlineKeyboardButton(text='Расходы на проверку', callback_data="admin_pending_expenses"),
            InlineKeyboardButton(text='Ставки', callback_data="admin_rates"),
        ],
        [
            InlineKeyboardButton(text='Задать ставку', callback_data="admin_set_rate"),
            InlineKeyboardButton(text='Премия', callback_data="admin_add_bonus"),
        ],
        [
            InlineKeyboardButton(text='Штраф', callback_data="admin_add_penalty"),
            InlineKeyboardButton(text='Премии / штрафы', callback_data="admin_adjustments"),
        ],
        [
            InlineKeyboardButton(text='Сделать админом', callback_data="admin_make_admin"),
            InlineKeyboardButton(text='Снять админа', callback_data="admin_remove_admin"),
        ],
        [
            InlineKeyboardButton(text='Удалить сотрудника', callback_data="admin_delete_worker"),
            InlineKeyboardButton(text='Объединить дубли', callback_data="admin_merge"),
        ],
        [
            InlineKeyboardButton(text='Показать проекты', callback_data="admin_projects"),
            InlineKeyboardButton(text='Добавить проект', callback_data="admin_add_project"),
        ],
        [InlineKeyboardButton(text='Обновить проекты Yougile', callback_data="admin_sync_projects")],
        [InlineKeyboardButton(text='Заявки за прошлый месяц', callback_data="admin_past_requests")],
        [
            InlineKeyboardButton(text='Удалить проект', callback_data="admin_delete_project"),
            InlineKeyboardButton(text='Закрыть месяц', callback_data="admin_close_month"),
        ],
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ],
    ])


def admin_work_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Смена', callback_data="admin_work_type_shift"),
            InlineKeyboardButton(text='Монтаж', callback_data="admin_work_type_install"),
        ],
        [
            InlineKeyboardButton(text='Отмена', callback_data="admin_cancel"),
        ],
    ])


def project_picker_keyboard(rows, page: int = 0, page_size: int = 8, *, mode: str = "pick"):
    keyboard_rows = []
    start = page * page_size
    page_rows = rows[start:start + page_size]

    for project_id, project_name in page_rows:
        callback_prefix = "project_pick" if mode == "pick" else "admin_project_delete"
        label = project_name if mode == "pick" else f"{project_name} ({project_id})"
        keyboard_rows.append(
            [InlineKeyboardButton(text=label, callback_data=f"{callback_prefix}_{project_id}_page_{page}")]
        )

    nav_row = []
    if page > 0:
        callback_name = "project_page" if mode == "pick" else "admin_project_page"
        nav_row.append(InlineKeyboardButton(text='◀', callback_data=f"{callback_name}_{page - 1}"))
    if start + page_size < len(rows):
        callback_name = "project_page" if mode == "pick" else "admin_project_page"
        nav_row.append(InlineKeyboardButton(text='▶', callback_data=f"{callback_name}_{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)

    if mode == "pick":
        keyboard_rows.append([InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home")])
    else:
        keyboard_rows.append([InlineKeyboardButton(text='Отмена', callback_data="admin_cancel")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def pending_state_hint(state_name: str | None) -> str:
    leaf = (state_name or "").rsplit(":", 1)[-1]
    hints = {
        "waiting_date": 'Ожидаю число месяца от 1 до 31, например: 14.',
        "waiting_project": 'Выберите проект кнопкой из справочника.',
        "waiting_hours": 'Ожидаю количество часов числом, например: 8 или 7.5.',
        "waiting_expense_type": 'Ожидаю короткое описание расхода.',
        "waiting_expense_amount": 'Ожидаю сумму расхода числом, например: 1500 или 1500.50.',
        "waiting_expense_receipt": 'Ожидаю фото или файл чека. Если чека нет, используйте кнопку продолжения без чека.',
        "waiting_past_month_date": 'Ожидаю полную дату из прошлого месяца в формате ДД.ММ.ГГГГ.',
        "waiting_past_month_type": 'Выберите тип записи кнопкой ниже.',
        "waiting_past_month_project": 'Выберите проект кнопкой из справочника.',
        "waiting_past_month_hours": 'Ожидаю количество часов числом, например: 8 или 7.5.',
        "waiting_past_month_expense_description": 'Ожидаю краткое описание расхода.',
        "waiting_past_month_expense_amount": 'Ожидаю сумму расхода числом, например: 1500 или 1500.50.',
        "choosing_stats_period": 'Выберите период кнопками ниже.',
        "admin_waiting_worker_id": 'Выберите сотрудника кнопкой или введите его числовой ID.',
        "admin_waiting_work_type": 'Выберите тип работ кнопкой или введите shift / install.',
        "admin_waiting_rate": 'Ожидаю ставку числом, например: 500 или 650.50.',
        "admin_waiting_amount": 'Ожидаю сумму числом без лишних символов.',
        "admin_waiting_description": 'Ожидаю описание в свободной форме.',
        "admin_waiting_second_worker_id": 'Ожидаю ID записи-дубля числом.',
        "admin_waiting_month": 'Ожидаю месяц в формате MM.YYYY или YYYY-MM.',
        "admin_waiting_project_id": 'Введите ID проекта цифрами или отправьте auto, если ID можно назначить автоматически.',
        "admin_waiting_project_name": 'Ожидаю название проекта.',
        "admin_confirm": 'Используйте кнопки Подтвердить или Отменить.',
        "waiting_standflow_link_code": 'Ожидаю код привязки StandFlow, например: A1B2C3D4.',
        "waiting_standflow_task_comment": 'Ожидаю текст комментария к задаче StandFlow.',
        "waiting_standflow_issue_description": 'Ожидаю короткое описание проблемы для StandFlow.',
        "waiting_standflow_issue_photo": 'Ожидаю фото/файл проблемы или кнопку Без фото.',
        "confirm_past_month": 'Используйте кнопки Подтвердить или Отменить.',
        "confirm": 'Используйте кнопки Подтвердить или Отменить.',
    }
    return hints.get(leaf, 'Используйте кнопки меню или введите значение в ожидаемом формате.')


class ReplayTelegramState:
    def __init__(self, user_id: int):
        self.user_id = int(user_id)

    async def get_state(self):
        current = await run_sync(load_conversation_state, "telegram", str(self.user_id))
        return current["state"] if current else None

    async def set_state(self, state):
        current = await run_sync(load_conversation_state, "telegram", str(self.user_id))
        current_data = current.get("data", {}) if current else {}
        state_name = getattr(state, "state", state)
        await run_sync(save_conversation_state, "telegram", str(self.user_id), state_name, current_data)

    async def get_data(self):
        current = await run_sync(load_conversation_state, "telegram", str(self.user_id))
        return dict(current.get("data", {})) if current else {}

    async def set_data(self, data):
        current = await run_sync(load_conversation_state, "telegram", str(self.user_id))
        current_state = current.get("state") if current else None
        await run_sync(save_conversation_state, "telegram", str(self.user_id), current_state, dict(data or {}))

    async def update_data(self, **kwargs):
        current = await self.get_data()
        current.update(kwargs)
        await self.set_data(current)

    async def clear(self):
        await run_sync(clear_conversation_state, "telegram", str(self.user_id))


class ReplayTelegramMessage:
    def __init__(self, bot_instance, user_id: int, text: str | None):
        self.bot = bot_instance
        self.text = text
        self.caption = text
        self.photo = []
        self.document = None
        self.from_user = SimpleNamespace(id=int(user_id), full_name=f"User {user_id}")
        self.chat = SimpleNamespace(id=int(user_id), type="private")

    async def answer(self, text: str, **kwargs):
        return await self.bot.send_message(self.chat.id, text, **kwargs)


def telegram_user_id_from_state_key(user_key) -> int:
    raw_key = str(user_key or "").strip()
    if raw_key.isdigit():
        return int(raw_key)

    parts = raw_key.split(":")
    if len(parts) >= 3 and parts[2].isdigit():
        return int(parts[2])

    raise ValueError(f"Unsupported Telegram state user key: {raw_key!r}")


async def auto_replay_pending_telegram_message(bot_instance, row: dict):
    if not row:
        logger.info("Telegram startup replay: no replay row")
        return

    user_id = telegram_user_id_from_state_key(row["user_key"])

    if row.get("state") == "waiting_expense_receipt" and row.get("has_media"):
        logger.info(
            "Telegram startup replay: pending media receipt for user %s in state %s, requesting resend",
            user_id,
            row.get("state"),
        )
        replay_message = ReplayTelegramMessage(bot_instance, user_id, None)
        await replay_message.answer(
            "Последним сообщением был чек с вложением. После перезапуска его нужно отправить заново.",
            reply_markup=expense_receipt_keyboard(),
        )
        return

    if not row.get("message_text"):
        logger.info(
            "Telegram startup replay: skipping empty replay row for user %s (state=%s)",
            user_id,
            row.get("state"),
        )
        return

    logger.info(
        "Telegram startup replay: reprocessing pending message for user %s (state=%s, updated_at=%s)",
        user_id,
        row.get("state"),
        row.get("last_inbound_at") or row.get("updated_at"),
    )
    replay_message = ReplayTelegramMessage(bot_instance, user_id, row["message_text"])
    replay_state = ReplayTelegramState(user_id)
    try:
        if row.get("state"):
            await replay_state.set_state(row.get("state"))
        await replay_state.set_data(row.get("state_data") or {})
        await replay_pending_telegram_message(replay_message, replay_state)
    except Exception:
        logger.exception("Failed to auto-replay pending Telegram message for user %s", user_id)
        return

    logger.info("Telegram startup replay: successfully reprocessed message for user %s", user_id)


async def send_telegram_state_reminder(bot_instance, row: dict):
    user_id = telegram_user_id_from_state_key(row["user_key"])
    state_name = row.get("state")
    if not state_name:
        return
    leaf = state_name.rsplit(":", 1)[-1]
    reply_markup = back_to_menu_keyboard()
    if leaf == "waiting_expense_receipt":
        reply_markup = expense_receipt_keyboard()
    elif leaf == "waiting_place":
        reply_markup = work_type_keyboard()
    elif leaf == "waiting_past_month_type":
        reply_markup = past_month_type_keyboard()
    elif leaf == "admin_waiting_work_type":
        reply_markup = admin_work_type_keyboard()

    message = (
        "Бот перезапустился, но ваш сценарий сохранён.\n"
        f"{pending_state_hint(state_name)}"
    )
    await bot_instance.send_message(user_id, message, reply_markup=reply_markup)
    logger.info("Telegram startup reminder: sent continuation hint to user %s for state %s", user_id, state_name)


async def auto_replay_pending_telegram_messages(bot_instance):
    rows = await run_sync(list_conversations_needing_reply, "telegram")
    logger.info("Telegram startup replay: found %d conversations needing reply", len(rows))
    replayed_user_ids = set()
    for row in rows:
        try:
            await auto_replay_pending_telegram_message(bot_instance, row)
            replayed_user_ids.add(str(row["user_key"]))
        except Exception:
            logger.exception("Telegram startup replay failed for pending row %s", row)

    active_states = await run_sync(list_active_conversation_states, "telegram")
    logger.info("Telegram startup replay: found %d active conversation states", len(active_states))
    for row in active_states:
        if str(row["user_key"]) in replayed_user_ids:
            continue
        try:
            await send_telegram_state_reminder(bot_instance, row)
        except Exception:
            logger.exception("Telegram startup reminder failed for state row %s", row)


async def replay_pending_telegram_message(message: Message, state: FSMContext):
    pending = await run_sync(get_pending_inbound_message, "telegram", str(message.from_user.id))
    if not pending or not pending.get("message_text"):
        await message.answer(
            'Не нашёл последнего сообщения, которое осталось без ответа.',
            reply_markup=back_to_menu_keyboard(),
        )
        return

    state_name = pending.get("state")
    state_data = pending.get("state_data") or {}
    leaf = (state_name or "").rsplit(":", 1)[-1]

    if leaf == "waiting_expense_receipt" and pending.get("has_media"):
        await message.answer(
            'Последним сообщением был чек с вложением. Автоматически повторить его нельзя — пожалуйста, отправьте файл ещё раз.',
            reply_markup=expense_receipt_keyboard(),
        )
        return

    handler_map = {
        "waiting_date": handle_date,
        "waiting_project": handle_project,
        "waiting_hours": handle_hours,
        "waiting_expense_type": handle_expense_type,
        "waiting_expense_amount": handle_expense_amount,
        "waiting_expense_receipt": handle_expense_receipt,
        "waiting_past_month_date": handle_past_month_date,
        "waiting_past_month_type": handle_past_month_type,
        "waiting_past_month_project": handle_past_month_project,
        "waiting_past_month_hours": handle_past_month_hours,
        "waiting_past_month_expense_description": handle_past_month_expense_description,
        "waiting_past_month_expense_amount": handle_past_month_expense_amount,
        "admin_waiting_worker_id": handle_admin_worker_id,
        "admin_waiting_work_type": handle_admin_work_type,
        "admin_waiting_rate": handle_admin_rate,
        "admin_waiting_amount": handle_admin_amount,
        "admin_waiting_description": handle_admin_description,
        "admin_waiting_second_worker_id": handle_admin_second_worker_id,
        "admin_waiting_month": handle_admin_month,
        "admin_waiting_project_id": handle_admin_project_id,
        "admin_waiting_project_name": handle_admin_project_name,
        "admin_confirm": handle_admin_confirm_text,
        "confirm_past_month": handle_confirm_past_month,
    }
    handler = handler_map.get(leaf)
    if handler is None:
        await message.answer(
            'Это сообщение осталось без ответа, но автоматически повторить именно этот шаг я пока не могу.\n'
            f"{pending_state_hint(state_name)}\n\n"
            f"Последнее сообщение: {pending['message_text']}",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if state_name:
        await state.set_state(state_name)
    await state.set_data(state_data)
    replay_message = message.model_copy(update={"text": pending["message_text"], "caption": pending["message_text"]})
    stateful_handlers = {
        handle_date,
        handle_project,
        handle_hours,
        handle_expense_type,
        handle_expense_amount,
        handle_expense_receipt,
        handle_admin_worker_id,
        handle_admin_work_type,
        handle_admin_rate,
        handle_admin_amount,
        handle_admin_description,
        handle_admin_second_worker_id,
        handle_admin_month,
        handle_admin_project_id,
        handle_admin_project_name,
    }
    if handler in stateful_handlers:
        await handler(replay_message, state)
    else:
        await handler(replay_message)


def admin_adjustments_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Показать премии', callback_data="admin_show_bonuses"),
            InlineKeyboardButton(text='Показать штрафы', callback_data="admin_show_penalties"),
        ],
        [
            InlineKeyboardButton(text='В админ-панель', callback_data="menu_admin"),
        ],
    ])


def admin_month_keyboard():
    today = date.today()
    current = f"{today.month:02d}.{today.year}"
    previous_year = today.year if today.month > 1 else today.year - 1
    previous_month = today.month - 1 if today.month > 1 else 12
    previous = f"{previous_month:02d}.{previous_year}"
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f'Текущий: {current}', callback_data=f"admin_month_{current}"),
        ],
        [
            InlineKeyboardButton(text=f'Предыдущий: {previous}', callback_data=f"admin_month_{previous}"),
        ],
        [
            InlineKeyboardButton(text='Ввести другой месяц', callback_data="admin_month_manual"),
            InlineKeyboardButton(text='Отмена', callback_data="admin_cancel"),
        ],
    ])


def admin_confirm_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Подтвердить', callback_data="admin_confirm_yes"),
            InlineKeyboardButton(text='Отменить', callback_data="admin_confirm_no"),
        ]
    ])


def admin_worker_picker_keyboard(action: str, rows, page: int = 0, page_size: int = 8):
    keyboard_rows = []
    view = build_worker_picker_page(rows, page=page, page_size=page_size)
    for worker_id, full_name in view["page_rows"]:
        keyboard_rows.append([
            InlineKeyboardButton(text=f"{full_name} ({worker_id})", callback_data=f"admin_pick_{action}_{worker_id}"),
        ])
    nav_row = []
    if view["page"] > 0:
        nav_row.append(InlineKeyboardButton(text='◀', callback_data=f"admin_page_{action}_{view['page'] - 1}"))
    if view["page"] + 1 < view["total_pages"]:
        nav_row.append(InlineKeyboardButton(text='▶', callback_data=f"admin_page_{action}_{view['page'] + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([InlineKeyboardButton(text='Отмена', callback_data="admin_cancel")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def pending_users_page_keyboard(rows, page: int = 0, page_size: int = 5):
    keyboard_rows = []
    start = page * page_size
    page_rows = rows[start:start + page_size]
    for worker_id, full_name, _chat_id in page_rows:
        keyboard_rows.append([
            InlineKeyboardButton(text=f'✅ {full_name}', callback_data=f"approve_user_{worker_id}_page_{page}"),
            InlineKeyboardButton(text='❌', callback_data=f"reject_user_{worker_id}_page_{page}"),
        ])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text='◀', callback_data=f"pending_users_page_{page - 1}"))
    if start + page_size < len(rows):
        nav_row.append(InlineKeyboardButton(text='▶', callback_data=f"pending_users_page_{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([InlineKeyboardButton(text='В админ-панель', callback_data="menu_admin")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def pending_expenses_page_keyboard(rows, page: int = 0, page_size: int = 5):
    keyboard_rows = []
    start = page * page_size
    page_rows = rows[start:start + page_size]
    for expense_id, full_name, amount, _description, _expense_date in page_rows:
        keyboard_rows.append([
            InlineKeyboardButton(text=f'✅ {full_name} • {amount}', callback_data=f"approve_expense_{expense_id}_page_{page}"),
            InlineKeyboardButton(text='❌', callback_data=f"reject_expense_{expense_id}_page_{page}"),
        ])
        keyboard_rows.append([
            InlineKeyboardButton(text='🧾 Показать чек', callback_data=f"show_expense_receipt_{expense_id}_page_{page}")
        ])
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text='◀', callback_data=f"pending_expenses_page_{page - 1}"))
    if start + page_size < len(rows):
        nav_row.append(InlineKeyboardButton(text='▶', callback_data=f"pending_expenses_page_{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([InlineKeyboardButton(text='В админ-панель', callback_data="menu_admin")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def work_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Монтаж', callback_data="place_install"),
            InlineKeyboardButton(text='Смена', callback_data="place_shift"),
        ],
        [
            InlineKeyboardButton(text='Расходы', callback_data="place_expense"),
        ],
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ],
    ])


def past_month_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Монтаж', callback_data="past_place_install"),
            InlineKeyboardButton(text='Смена', callback_data="past_place_shift"),
        ],
        [
            InlineKeyboardButton(text='Расходы', callback_data="past_place_expense"),
        ],
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ],
    ])


def stats_period_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='1-я половина месяца', callback_data="stats_first_half"),
            InlineKeyboardButton(text='2-я половина месяца', callback_data="stats_second_half"),
        ],
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ],
    ])


def back_to_menu_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ]
    ])


def standflow_keyboard(*, show_admin: bool = False):
    keyboard_rows = [
        [
            InlineKeyboardButton(text="Мои задачи", callback_data="sf_tasks"),
            InlineKeyboardButton(text="На сегодня", callback_data="sf_today"),
        ],
        [
            InlineKeyboardButton(text="Напоминания", callback_data="sf_reminders"),
            InlineKeyboardButton(text="Сводка дня", callback_data="sf_summary"),
        ],
        [
            InlineKeyboardButton(text="Привязать StandFlow", callback_data="sf_link"),
        ],
        [
            InlineKeyboardButton(text="Сообщить о проблеме", callback_data="sf_issue"),
        ],
    ]
    if show_admin:
        keyboard_rows.append(
            [InlineKeyboardButton(text="Код для сотрудника", callback_data="sf_link_admin")]
        )
    keyboard_rows.append([InlineKeyboardButton(text="Вернуться в главное меню", callback_data="menu_home")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def standflow_task_keyboard(task_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Готово", callback_data=f"sf_done_{task_id}"),
            InlineKeyboardButton(text="Комментарий", callback_data=f"sf_comment_{task_id}"),
        ],
        [
            InlineKeyboardButton(text="К списку задач", callback_data="sf_tasks"),
        ],
    ])


def standflow_link_candidates_keyboard(users: list[dict], page: int = 0, page_size: int = 8):
    keyboard_rows = []
    start = page * page_size
    page_rows = users[start:start + page_size]
    for user in page_rows:
        linked = " *" if user.get("telegram_chat_id") else ""
        label = f"{user['full_name']} ({user['role']}){linked}"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"sf_link_user_{user['id']}_p_{page}",
                )
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="Назад", callback_data=f"sf_link_page_{page - 1}"))
    if start + page_size < len(users):
        nav_row.append(InlineKeyboardButton(text="Еще", callback_data=f"sf_link_page_{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard_rows.append([InlineKeyboardButton(text="В StandFlow", callback_data="menu_standflow")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def standflow_issue_projects_keyboard(projects: list[dict], page: int = 0, page_size: int = 8):
    keyboard_rows = []
    start = page * page_size
    page_rows = projects[start:start + page_size]
    for project in page_rows:
        label = f"{project['number']} - {project['title']}"
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    text=label[:64],
                    callback_data=f"sf_issue_project_{project['id']}_p_{page}",
                )
            ]
        )

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="Назад", callback_data=f"sf_issue_page_{page - 1}"))
    if start + page_size < len(projects):
        nav_row.append(InlineKeyboardButton(text="Еще", callback_data=f"sf_issue_page_{page + 1}"))
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard_rows.append([InlineKeyboardButton(text="В StandFlow", callback_data="menu_standflow")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def standflow_issue_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Монтаж", callback_data="sf_issue_type_installation"),
            InlineKeyboardButton(text="Производство", callback_data="sf_issue_type_production"),
        ],
        [
            InlineKeyboardButton(text="Закупки", callback_data="sf_issue_type_purchasing"),
            InlineKeyboardButton(text="Другое", callback_data="sf_issue_type_other"),
        ],
        [
            InlineKeyboardButton(text="В StandFlow", callback_data="menu_standflow"),
        ],
    ])


def standflow_issue_severity_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Обычная", callback_data="sf_issue_severity_medium"),
            InlineKeyboardButton(text="Важная", callback_data="sf_issue_severity_high"),
        ],
        [
            InlineKeyboardButton(text="Критичная", callback_data="sf_issue_severity_critical"),
        ],
        [
            InlineKeyboardButton(text="В StandFlow", callback_data="menu_standflow"),
        ],
    ])


def standflow_issue_photo_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="Без фото", callback_data="sf_issue_no_photo"),
        ],
        [
            InlineKeyboardButton(text="В StandFlow", callback_data="menu_standflow"),
        ],
    ])


def _standflow_error_text(exc: Exception) -> str:
    return (
        "Не получилось получить данные из StandFlow.\n\n"
        "Проверьте, что StandFlow запущен, общий токен ботов совпадает, "
        "а ваш Telegram ID привязан к пользователю StandFlow.\n\n"
        f"Техническая деталь: {exc}"
    )


def standflow_intro_text() -> str:
    return (
        "Раздел StandFlow.\n\n"
        "Здесь можно работать без команд: просто нажмите нужную кнопку.\n"
        "Рекомендуемый порядок для менеджера: сначала \"Сводка дня\", затем \"На сегодня\".\n\n"
        "Если бот напишет, что Telegram ID не найден, нажмите в главном меню \"Мой ID\" "
        "или используйте кнопку \"Привязать StandFlow\", если администратор выдал вам короткий код."
    )


def _standflow_work_log_payload(form_data: dict) -> dict | None:
    place = form_data.get("place")
    if place not in {"Монтаж", "Смена"}:
        return None

    hours = float(form_data.get("hours") or 0)
    if hours <= 0:
        return None

    work_type = "installation" if place == "Монтаж" else "other"
    description_parts = [f"Imported from worktimebot: {place}"]
    if form_data.get("project"):
        description_parts.append(f"Project: {form_data['project']}")

    project_id = form_data.get("standflow_project_id")
    return {
        "duration_minutes": int(round(hours * 60)),
        "work_type": work_type,
        "description": "\n".join(description_parts),
        "project_id": int(project_id) if project_id is not None else None,
    }


async def _sync_work_log_to_standflow(message: Message, telegram_user_id: int, form_data: dict) -> bool:
    payload = _standflow_work_log_payload(form_data)
    if payload is None:
        return True

    try:
        await run_sync(import_work_log, telegram_user_id, **payload)
    except StandFlowError as exc:
        logger.warning("Failed to sync work log to StandFlow: %s", exc)
        await message.answer(
            "Запись сохранена в боте, но не попала в StandFlow.\n"
            f"{_standflow_error_text(exc)}",
            reply_markup=back_to_menu_keyboard(),
        )
        return False
    return True


async def _send_standflow_task_cards(message: Message, *, telegram_user_id: int | None = None, due_today: bool = False):
    effective_user_id = telegram_user_id or message.from_user.id
    try:
        cards = await run_sync(get_task_cards, effective_user_id, due_today=due_today)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    if not cards:
        empty_text = (
            "На сегодня задач StandFlow нет."
            if due_today
            else "Активных задач StandFlow пока нет."
        )
        await message.answer(empty_text, reply_markup=standflow_keyboard())
        return

    for card in cards:
        await message.answer(
            card["text"],
            reply_markup=standflow_task_keyboard(card["task_id"]),
        )


async def _send_standflow_link_candidates(message: Message, page: int = 0):
    try:
        users = await run_sync(list_telegram_link_candidates)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard(show_admin=True))
        return

    if not users:
        await message.answer("В StandFlow пока нет активных пользователей.", reply_markup=standflow_keyboard(show_admin=True))
        return

    await message.answer(
        "Выберите сотрудника StandFlow, для которого нужно создать код привязки.\n\n"
        "Звездочка * означает, что Telegram уже привязан.",
        reply_markup=standflow_link_candidates_keyboard(users, page=page),
    )


async def _send_standflow_issue_projects(message: Message, page: int = 0):
    try:
        projects = await run_sync(list_active_projects)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    if not projects:
        await message.answer(
            "В StandFlow пока нет активных проектов, к которым можно привязать проблему.",
            reply_markup=standflow_keyboard(),
        )
        return

    await message.answer(
        "Выберите проект, по которому нужно сообщить о проблеме.",
        reply_markup=standflow_issue_projects_keyboard(projects, page=page),
    )


def _standflow_issue_type_label(issue_type: str | None) -> str:
    labels = {
        "installation": "Монтаж",
        "production": "Производство",
        "purchasing": "Закупки",
        "other": "Другое",
    }
    return labels.get(issue_type or "other", "Другое")


def _build_standflow_issue_title(issue_type: str | None, description: str) -> str:
    prefix = _standflow_issue_type_label(issue_type)
    first_line = description.splitlines()[0].strip()
    short_description = first_line[:110] if first_line else "без описания"
    return f"{prefix}: {short_description}"


async def _submit_standflow_issue(
    message: Message,
    state: FSMContext,
    *,
    upload_filename: str | None = None,
    upload_content: bytes | None = None,
    upload_content_type: str | None = None,
):
    state_data = await state.get_data()
    description = (state_data.get("standflow_issue_description") or "").strip()
    project_id = state_data.get("standflow_issue_project_id")
    if not project_id or not description:
        await state.clear()
        await message.answer(
            "Не хватило данных для создания проблемы. Откройте StandFlow и начните сценарий заново.",
            reply_markup=standflow_keyboard(),
        )
        return

    issue_type = state_data.get("standflow_issue_type")
    severity = state_data.get("standflow_issue_severity") or "medium"
    title = _build_standflow_issue_title(issue_type, description)
    project_label = state_data.get("standflow_issue_project_label")
    full_description = description
    if project_label:
        full_description = f"Проект: {project_label}\nТип: {_standflow_issue_type_label(issue_type)}\n\n{description}"

    try:
        result = await run_sync(
            report_issue,
            message.from_user.id,
            project_id=int(project_id),
            title=title,
            description=full_description,
            severity=severity,
            upload_filename=upload_filename,
            upload_content=upload_content,
            upload_content_type=upload_content_type,
        )
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    await state.clear()
    issue = result.get("issue") or {}
    issue_id = issue.get("id")
    await message.answer(
        f"Проблема создана в StandFlow{f' #{issue_id}' if issue_id else ''}.\n"
        "Ответственный увидит ее в списке issues.",
        reply_markup=standflow_keyboard(show_admin=is_admin(message.from_user.id)),
    )


def future_date_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text='За прошлый месяц', callback_data="menu_past_month"),
        ],
        [
            InlineKeyboardButton(text='Вернуться в главное меню', callback_data="menu_home"),
        ],
    ])


def validate_current_month_day(work_day: int):
    today = date.today()
    days_in_month = monthrange(today.year, today.month)[1]
    if not (1 <= work_day <= days_in_month):
        return False, f'Дата должна быть в диапазоне от 1 до {days_in_month} для текущего месяца.'
    if work_day > today.day:
        return False, (
            'Эта дата ещё не наступила.\n\n'
            'Введите другое число текущего месяца или нажмите "За прошлый месяц".'
        )
    return True, None


def parse_month_argument(text: str):
    parts = text.split(maxsplit=1)
    if len(parts) == 1:
        today = date.today()
        return today.year, today.month

    raw_value = parts[1].strip()
    for separator in (".", "-", "/"):
        if separator in raw_value:
            left, right = raw_value.split(separator, maxsplit=1)
            if len(left) == 4:
                year = int(left)
                month = int(right)
            else:
                month = int(left)
                year = int(right)

            if not 1 <= month <= 12:
                raise ValueError
            return year, month
    raise ValueError


def parse_id_argument(text: str):
    parts = text.split(maxsplit=1)
    if len(parts) != 2:
        raise ValueError
    return int(parts[1].strip())


def parse_two_ids_argument(text: str):
    parts = text.split()
    if len(parts) != 3:
        raise ValueError
    return int(parts[1]), int(parts[2])


def parse_rate_argument(text: str):
    parts = text.split()
    if len(parts) != 4:
        raise ValueError

    worker_id = int(parts[1])
    work_type = parts[2].strip().lower()
    if work_type not in {"shift", "install"}:
        raise ValueError

    rate_value = float(parts[3].replace(",", "."))
    if rate_value <= 0:
        raise ValueError

    return worker_id, work_type, rate_value


def parse_adjustment_argument(text: str):
    parts = (text or "").split(maxsplit=3)
    if len(parts) < 4:
        raise ValueError

    worker_id = int(parts[1])
    amount = float(parts[2].replace(",", "."))
    description = parts[3].strip()
    if amount <= 0 or not description:
        raise ValueError

    return worker_id, amount, description


def parse_past_entry_type(text: str):
    normalized = (text or "").strip().lower()
    mapping = {
        "смена": "shift",
        "монтаж": "install",
        "расходы": "expense",
        "расход": "expense",
        "shift": "shift",
        "install": "install",
        "expense": "expense",
    }
    return mapping.get(normalized)


def get_worker_access(chat_id: int):
    worker = get_worker(chat_id)
    if not worker:
        return None, (
            'Вы еще не зарегистрированы в системе.\n'
            'Нажмите /start, чтобы отправить заявку на регистрацию.'
        )

    worker_id, registration_status, is_approved, active = worker
    if registration_status == "rejected":
        return None, 'Ваша заявка на регистрацию была отклонена. Если это ошибка, свяжитесь с администратором.'
    if not is_approved:
        return None, 'Ваша заявка уже отправлена и сейчас находится на рассмотрении администратора.'
    if not active:
        return None, 'Ваш аккаунт временно деактивирован. Обратитесь к администратору.'
    return worker_id, None


def get_worker_for_link(chat_id: int):
    worker = get_worker(chat_id)
    if not worker:
        return None, (
            'Вы еще не зарегистрированы в системе.\n'
            'Используйте /start, чтобы создать аккаунт.'
        )

    worker_id, registration_status, _, active = worker
    if registration_status == "rejected":
        return None, 'Ваша заявка на регистрацию была отклонена. Если это ошибка, свяжитесь с администратором.'
    if not active:
        return None, 'Ваш аккаунт временно деактивирован. Обратитесь к администратору.'
    return worker_id, None


async def notify_user(bot, chat_id, text):
    if not chat_id:
        return
    try:
        await bot.send_message(chat_id, text)
    except Exception:
        pass


async def finalize_admin_action(callback: CallbackQuery, text: str):
    await callback.answer()
    await callback.message.answer(text)
    try:
        await callback.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass


async def show_main_menu(target_message: Message, state: FSMContext):
    await state.clear()
    await state.set_state(WorkState.choosing_action)
    keyboard = admin_main_menu_keyboard() if is_admin(target_message.from_user.id) else main_menu_keyboard()
    await target_message.answer(
        'Главное меню.\n\n'
        'Здесь вы можете внести новую информацию о работе и расходах, '
        'посмотреть статистику, открыть свои записи и расходы.'
        + ('\nДля администратора также доступна отдельная админ-панель.' if is_admin(target_message.from_user.id) else "")
        + "\n"
        'Выберите нужный вариант кнопками ниже.',
        reply_markup=keyboard,
    )


async def send_past_month_request_to_admins(message: Message, state: FSMContext, worker_id: int):
    worker = await run_sync(get_worker_by_id, worker_id)
    full_name = worker[1] if worker else message.from_user.full_name
    state_data = await state.get_data()
    submission_key = state_data.get('past_request_key') or str(uuid.uuid4())
    await state.update_data(past_request_key=submission_key)
    request = await run_sync(create_request, state_data, worker_id, full_name, submission_key, 'telegram', message.chat.id)
    request_text = format_saved_request(request)
    await send_telegram_message(ADMIN_CHAT_ID, request_text, reply_markup=telegram_request_keyboard(request['id']))
    for admin_vk_id in await run_sync(get_admin_vk_ids):
        await send_vk_message(admin_vk_id, request_text, keyboard=vk_request_keyboard(request['id']))


async def show_past_requests(message: Message):
    requests = await run_sync(list_pending_requests)
    if not requests:
        await message.answer('Нет заявок за прошлый месяц, ожидающих решения.')
        return
    for request in requests:
        await message.answer(format_saved_request(request), reply_markup=telegram_request_keyboard(request['id']))


async def send_main_menu_from_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await show_main_menu(callback.message, state)


async def get_accessible_worker(message: Message):
    if not is_private(message):
        return None

    worker_id, error_text = await run_sync(get_worker_access, message.from_user.id)
    if error_text:
        await message.answer(error_text)
        return None
    return worker_id


async def approve_user_by_id(message: Message, worker_id: int):
    result = await run_sync(approve_user_result, worker_id)
    if not result["ok"]:
        await message.answer(result["error"])
        return
    await message.answer('Регистрация пользователя подтверждена.')
    await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])


async def reject_user_by_id(message: Message, worker_id: int):
    result = await run_sync(reject_user_result, worker_id)
    if not result["ok"]:
        await message.answer(result["error"])
        return
    await message.answer('Заявка на регистрацию отклонена.')
    await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])


async def approve_expense_by_id(message: Message, expense_id: int):
    result = await run_sync(approve_expense_result, expense_id)
    if not result["ok"]:
        await message.answer(result["error"])
        return
    await message.answer('Расход подтвержден.')
    schedule_sheet_sync(result["sync_year"], result["sync_month"])
    await notify_expense_origin(
        result["source_platform"],
        result["source_peer_id"],
        result["chat_id"],
        result["vk_id"],
        result["notify_text"],
    )


async def reject_expense_by_id(message: Message, expense_id: int):
    result = await run_sync(reject_expense_result, expense_id)
    if not result["ok"]:
        await message.answer(result["error"])
        return
    await message.answer('Расход отклонен.')
    await notify_expense_origin(
        result["source_platform"],
        result["source_peer_id"],
        result["chat_id"],
        result["vk_id"],
        result["notify_text"],
    )


async def show_expense_receipt_by_id(message: Message, expense_id: int):
    expense = await run_sync(get_expense_by_id, expense_id)
    if not expense:
        await message.answer('Расход не найден.')
        return

    (
        _id,
        _worker_id,
        full_name,
        _chat_id,
        amount,
        description,
        expense_date,
        _status,
        receipt_path,
        receipt_original_name,
        receipt_media_type,
        receipt_source_file_id,
        receipt_source_url,
        *_rest,
    ) = expense

    caption = (
        f'Чек по расходу #{expense_id}\n'
        f'Сотрудник: {full_name}\n'
        f'Дата: {expense_date}\n'
        f'Сумма: {amount}\n'
        f'Описание: {description}'
    )
    media_kind = "photo" if receipt_media_type == "photo" else "document"
    sent = False
    if receipt_path:
        sent = await send_telegram_media(message.chat.id, media_kind, receipt_path, caption)
    if not sent and receipt_source_file_id:
        sent = await send_telegram_media(message.chat.id, media_kind, receipt_source_file_id, caption)
    if not sent and receipt_source_url:
        sent = await send_telegram_media(message.chat.id, media_kind, receipt_source_url, caption)
    if not sent and receipt_source_url:
        try:
            restored_receipt = await save_remote_receipt(
                receipt_source_url,
                expense_id,
                "restored",
                media_kind,
                receipt_original_name,
            )
            await run_sync(
                update_expense_receipt,
                expense_id,
                restored_receipt["path"],
                restored_receipt["original_name"],
                restored_receipt["media_type"],
                receipt_source_file_id,
                receipt_source_url,
            )
            sent = await send_telegram_media(message.chat.id, media_kind, restored_receipt["path"], caption)
        except Exception:
            sent = False
    if not any([receipt_path, receipt_source_file_id, receipt_source_url]):
        await message.answer(f'У расхода #{expense_id} чек не приложен.')
        return
    if not sent:
        await message.answer(f'Не удалось отправить чек по расходу #{expense_id}.')


async def merge_workers_by_id(message: Message, target_worker_id: int, source_worker_id: int):
    await message.answer(await run_sync(merge_workers_result, target_worker_id, source_worker_id))


async def worker_rates_by_id(message: Message, worker_id: int):
    text = await run_sync(get_worker_rates_result, worker_id)
    if text is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(text, parse_mode=None)


async def save_rate_by_id(message: Message, worker_id: int, work_type: str, rate_value: float):
    text = await run_sync(set_worker_rate_result, worker_id, work_type, rate_value)
    if text is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(text, parse_mode=None)
    schedule_sheet_sync(date.today().year, date.today().month)


async def add_bonus_by_id(message: Message, worker_id: int, amount: float, description: str):
    result = await run_sync(add_bonus_result, worker_id, amount, description)
    if result is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(result["message_text"], parse_mode=None)
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])
    schedule_sheet_sync(result["sync_year"], result["sync_month"])


async def add_penalty_by_id(message: Message, worker_id: int, amount: float, description: str):
    result = await run_sync(add_penalty_result, worker_id, amount, description)
    if result is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(result["message_text"], parse_mode=None)
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])
    schedule_sheet_sync(result["sync_year"], result["sync_month"])


async def bonuses_by_worker_id(message: Message, worker_id: int):
    text = await run_sync(get_worker_bonuses_result, worker_id)
    if text is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(text, parse_mode=None)


async def penalties_by_worker_id(message: Message, worker_id: int):
    text = await run_sync(get_worker_penalties_result, worker_id)
    if text is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(text, parse_mode=None)


async def set_admin_by_id(message: Message, worker_id: int, is_admin_value: bool):
    result = await run_sync(set_admin_result, worker_id, is_admin_value)
    if result is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(result["message_text"], parse_mode=None)
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])


async def delete_worker_by_id(message: Message, worker_id: int):
    result = await run_sync(delete_worker_result, worker_id)
    if result is None:
        await message.answer('Пользователь не найден.')
        return
    await message.answer(result["message_text"], parse_mode=None)
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])


async def send_my_id_info(message: Message, user_id: int):
    if not is_private(message):
        return

    user = await run_sync(get_worker, user_id)
    if not user:
        await message.answer(
            'Вы еще не зарегистрированы в системе.\n'
            'Используйте /start для регистрации.'
        )
        return

    worker_id, registration_status, is_approved, active = user
    await message.answer(
        f'Ваш ID: {worker_id}\n\n'
        'Используйте этот номер, если захотите подключить к боту другой мессенджер (VK, Telegram и т.д.).',
        reply_markup=back_to_menu_keyboard(),
    )


async def send_link_vk_code(message: Message, user_id: int):
    if not is_private(message):
        return

    worker_id, error_text = get_worker_for_link(user_id)
    if error_text:
        await message.answer(error_text)
        return

    result = await run_sync(generate_account_link_code, worker_id, "vk")
    await message.answer(
        'Код для привязки VK создан.\n'
        f"Код: {result['code']}\n"
        f"Он действует {result['ttl_minutes']} минут.\n\n"
        'Откройте VK-бот, нажмите /start, ответьте что аккаунт уже есть, и отправьте этот код.',
        reply_markup=back_to_menu_keyboard(),
    )


async def sync_current_sheet_for_month(year: int, month: int):
    try:
        await asyncio.to_thread(sync_monthly_report_to_current_sheet, year, month)
    except Exception as exc:
        logger.exception("Failed to sync Google Sheets for %04d-%02d: %s", year, month, exc)


def schedule_sheet_sync(year: int, month: int):
    key = (year, month)
    existing_task = _sheet_sync_tasks.get(key)
    if existing_task and not existing_task.done():
        return existing_task

    task = asyncio.create_task(sync_current_sheet_for_month(year, month))
    _sheet_sync_tasks[key] = task

    def _log_task_result(done_task: asyncio.Task):
        try:
            done_task.result()
        except Exception as exc:
            logger.exception("Background Google Sheets sync failed for %04d-%02d: %s", year, month, exc)
        finally:
            if _sheet_sync_tasks.get(key) is done_task:
                _sheet_sync_tasks.pop(key, None)

    task.add_done_callback(_log_task_result)
    return task


async def show_admin_worker_picker(message: Message, state: FSMContext, action: str, prompt: str, page: int = 0):
    rows = await run_sync(get_active_workers)
    if not rows:
        await message.answer('Сейчас нет активных подтвержденных пользователей.', reply_markup=admin_panel_keyboard())
        return

    await state.clear()
    await state.update_data(admin_action=action)
    await state.set_state(WorkState.admin_waiting_worker_id)
    await message.answer(prompt, reply_markup=admin_worker_picker_keyboard(action, rows, page=page))


async def start_admin_worker_picker(message: Message, state: FSMContext, action: str):
    await show_admin_worker_picker(message, state, action, get_admin_worker_prompt(action))


async def handle_admin_worker_pick_action(message: Message, state: FSMContext, action: str, worker_id: int):
    if action == "show_rates":
        await worker_rates_by_id(message, worker_id)
        await state.clear()
        return

    if action == "show_bonuses":
        await bonuses_by_worker_id(message, worker_id)
        await state.clear()
        return

    if action == "show_penalties":
        await penalties_by_worker_id(message, worker_id)
        await state.clear()
        return

    if action == "set_rate":
        await state.set_state(WorkState.admin_waiting_work_type)
        await message.answer('Выберите тип работ для ставки.', reply_markup=admin_work_type_keyboard())
        return

    if action in {"add_bonus", "add_penalty"}:
        await state.set_state(WorkState.admin_waiting_amount)
        label = 'премии' if action == "add_bonus" else 'штрафа'
        await message.answer(f'Введите сумму {label}.', reply_markup=back_to_menu_keyboard())
        return

    if action == "merge_workers":
        rows = [(wid, name) for wid, name in await run_sync(get_active_workers) if wid != worker_id]
        if not rows:
            await state.clear()
            await message.answer('Нет другой активной записи для объединения.', reply_markup=admin_panel_keyboard())
            return
        await state.set_state(WorkState.admin_waiting_second_worker_id)
        await message.answer(
            'Выберите запись-дубль, которую нужно влить в основную.',
            reply_markup=admin_worker_picker_keyboard("merge_source", rows),
        )
        return

    if action == "make_admin":
        await show_admin_confirmation(message, state, 'Подтвердите выдачу прав администратора:')
        return

    if action == "remove_admin":
        await show_admin_confirmation(message, state, 'Подтвердите снятие прав администратора:')
        return

    if action == "delete_worker":
        await show_admin_confirmation(
            message,
            state,
            'Подтвердите полное удаление сотрудника:',
            footer='Будут удалены профиль, записи, расходы, ставки, премии и штрафы.',
        )
        return


async def prompt_admin_worker_id(message: Message, state: FSMContext, action: str, prompt: str):
    await state.clear()
    await state.update_data(admin_action=action)
    await state.set_state(WorkState.admin_waiting_worker_id)
    await message.answer(prompt, reply_markup=back_to_menu_keyboard())


async def prompt_admin_worker_id_from_callback(callback: CallbackQuery, state: FSMContext, action: str, prompt: str):
    await callback.answer()
    await state.clear()
    await state.update_data(admin_action=action)
    await state.set_state(WorkState.admin_waiting_worker_id)
    await callback.message.answer(prompt, reply_markup=back_to_menu_keyboard())


async def show_admin_confirmation(message: Message, state: FSMContext, title: str, footer: str | None = None):
    data = await state.get_data()
    worker = await run_sync(get_worker_by_id, data["worker_id"])
    full_name = worker[1] if worker else f"ID {data['worker_id']}"
    text_lines = [title, f"Сотрудник: {full_name} (ID: {data['worker_id']})"]
    if data.get("work_type"):
        work_type_text = 'Смена' if data["work_type"] == "shift" else 'Монтаж'
        text_lines.append(f'Тип работ: {work_type_text}')
    if data.get("rate_value") is not None:
        text_lines.append(f"Ставка: {data['rate_value']}")
    if data.get("amount") is not None:
        text_lines.append(f"Сумма: {data['amount']}")
    if data.get("description"):
        text_lines.append(f"Описание: {data['description']}")
    if footer:
        text_lines.extend(["", footer])
    await state.set_state(WorkState.admin_confirm)
    await message.answer("\n".join(text_lines), reply_markup=admin_confirm_keyboard())


async def send_pending_users_overview(message: Message, page: int = 0):
    rows = await run_sync(get_pending_workers)
    if not rows:
        await message.answer(format_pending_users(rows), reply_markup=admin_panel_keyboard())
        return
    text, view = build_pending_users_overview_text(rows, page=page)
    await message.answer(text, reply_markup=pending_users_page_keyboard(rows, page=view["page"]))


async def send_pending_expenses_overview(message: Message, page: int = 0):
    rows = await run_sync(get_pending_expenses)
    if not rows:
        await message.answer(format_pending_expenses(rows), reply_markup=admin_panel_keyboard())
        return
    text, view = build_pending_expenses_overview_text(rows, page=page)
    await message.answer(text, reply_markup=pending_expenses_page_keyboard(rows, page=view["page"]))


def parse_paged_action(data: str, prefix: str):
    payload = data.removeprefix(prefix)
    if "_page_" in payload:
        item_id_raw, page_raw = payload.split("_page_", 1)
        return int(item_id_raw), int(page_raw)
    return int(payload), None


async def refresh_pending_users_message(callback: CallbackQuery, page: int):
    rows = await run_sync(get_pending_workers)
    if not rows:
        await callback.message.edit_text('Сейчас новых заявок на регистрацию нет.')
        await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
        return
    text, view = build_pending_users_overview_text(rows, page=page)
    await callback.message.edit_text(text)
    await callback.message.edit_reply_markup(reply_markup=pending_users_page_keyboard(rows, page=view["page"]))


async def refresh_pending_expenses_message(callback: CallbackQuery, page: int):
    rows = await run_sync(get_pending_expenses)
    if not rows:
        await callback.message.edit_text('Сейчас нет расходов, ожидающих подтверждения.')
        await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
        return
    text, view = build_pending_expenses_overview_text(rows, page=page)
    await callback.message.edit_text(text)
    await callback.message.edit_reply_markup(reply_markup=pending_expenses_page_keyboard(rows, page=view["page"]))


@router.message(Command("pending_users"))
async def pending_users_handler(message: Message):
    if not is_admin_message(message):
        return
    await send_pending_users_overview(message)


@router.message(Command("users"))
async def users_command_handler(message: Message):
    if not is_admin_message(message):
        return
    await message.answer(format_active_workers(await run_sync(get_active_workers)))


@router.message(Command("merge_workers"))
async def merge_workers_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        target_worker_id, source_worker_id = parse_two_ids_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /merge_workers KEEP_ID DUPLICATE_ID')
        return

    try:
        await merge_workers_by_id(message, target_worker_id, source_worker_id)
    except ValueError as exc:
        await message.answer(str(exc))


@router.message(Command("rates"))
async def rates_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /rates WORKER_ID')
        return

    await worker_rates_by_id(message, worker_id)


@router.message(Command("set_rate"))
async def set_rate_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id, work_type, rate_value = parse_rate_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /set_rate WORKER_ID shift|install RATE')
        return

    await save_rate_by_id(message, worker_id, work_type, rate_value)


@router.message(Command("add_bonus"))
async def add_bonus_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id, amount, description = parse_adjustment_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /add_bonus WORKER_ID AMOUNT DESCRIPTION')
        return

    await add_bonus_by_id(message, worker_id, amount, description)


@router.message(Command("add_penalty"))
async def add_penalty_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id, amount, description = parse_adjustment_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /add_penalty WORKER_ID AMOUNT DESCRIPTION')
        return

    await add_penalty_by_id(message, worker_id, amount, description)


@router.message(Command("bonuses"))
async def bonuses_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /bonuses WORKER_ID')
        return

    await bonuses_by_worker_id(message, worker_id)


@router.message(Command("penalties"))
async def penalties_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /penalties WORKER_ID')
        return

    await penalties_by_worker_id(message, worker_id)


@router.message(Command("make_admin"))
async def make_admin_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /make_admin WORKER_ID')
        return

    await set_admin_by_id(message, worker_id, True)


@router.message(Command("remove_admin"))
async def remove_admin_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /remove_admin WORKER_ID')
        return

    await set_admin_by_id(message, worker_id, False)


@router.message(Command("delete_worker"))
async def delete_worker_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /delete_worker WORKER_ID')
        return

    await delete_worker_by_id(message, worker_id)


@router.message(Command("link_vk"))
async def link_vk_command_handler(message: Message):
    await send_link_vk_code(message, message.from_user.id)


@router.message(Command("approve_user"))
async def approve_user_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /approve_user ID')
        return

    await approve_user_by_id(message, worker_id)


@router.message(Command("reject_user"))
async def reject_user_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        worker_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /reject_user ID')
        return

    await reject_user_by_id(message, worker_id)


@router.message(Command("help_admin"))
async def help_admin_handler(message: Message):
    if not is_admin_message(message):
        return
    await message.answer(admin_help_text())


@router.message(Command("help"))
async def help_handler(message: Message):
    if is_admin_message(message):
        await message.answer(admin_help_text())
        return
    if is_private(message):
        await message.answer(user_help_text(), reply_markup=back_to_menu_keyboard())


@router.message(Command("pending_expenses"))
async def pending_expenses_handler(message: Message):
    if not is_admin_message(message):
        return
    await send_pending_expenses_overview(message)


@router.message(Command("approve_expense"))
async def approve_expense_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        expense_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /approve_expense ID')
        return

    await approve_expense_by_id(message, expense_id)


@router.message(Command("reject_expense"))
async def reject_expense_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        expense_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /reject_expense ID')
        return

    await reject_expense_by_id(message, expense_id)


@router.message(Command("show_expense_receipt"))
async def show_expense_receipt_command_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        expense_id = parse_id_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /show_expense_receipt ID')
        return

    await show_expense_receipt_by_id(message, expense_id)


@router.message(Command("close_month"))
async def close_month_handler(message: Message):
    if not is_admin_message(message):
        return

    try:
        year, month = parse_month_argument(message.text or "")
    except ValueError:
        await message.answer('Формат команды: /close_month MM.YYYY или /close_month YYYY-MM')
        return

    await message.answer(
        f'Закрываю месяц {month:02d}.{year} и обновляю рабочий лист. '
        'Это может занять несколько секунд.'
    )
    try:
        result = await asyncio.to_thread(close_month_tracked, year, month, "telegram_manual")
    except ValueError as exc:
        await message.answer(str(exc))
        return
    except Exception as exc:
        await message.answer(google_error_message(exc))
        return

    if result["status"] == "busy":
        await message.answer('Этот месяц уже закрывается в другом процессе. Попробуйте ещё раз через минуту.')
        return
    if result["status"] == "already_closed":
        archived = result["archived_month"]
        current = result["current_month"]
        await message.answer(
            'Этот месяц уже был закрыт ранее.\n'
            f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
            f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})"
        )
        return

    archived = result["archived_month"]
    current = result["current_month"]
    await message.answer(
        'Месяц успешно закрыт.\n'
        f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
        f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})"
    )


@router.message(Command("start"))
async def start_handler(message: Message, state: FSMContext):
    if not is_private(message):
        return

    user = await run_sync(get_worker, message.from_user.id)
    if not user:
        worker_id = await run_sync(create_worker, message.from_user.full_name, message.from_user.id)
        await message.answer(
            f'Заявка на регистрацию отправлена администраторам.\n\n'
            f'**Ваш ID: {worker_id}**\n\n'
            f'Сохраните этот номер — он понадобится если вы захотите использовать бота в другом мессенджере.\n\n'
            f'Как только доступ будет подтвержден, вы сможете вносить рабочие данные, '
            f'расходы и смотреть статистику через это меню.'
        )
        await message.bot.send_message(
            ADMIN_CHAT_ID,
            f'Новая заявка на регистрацию:\n{message.from_user.full_name} (ID: {worker_id})',
            reply_markup=register_keyboard(worker_id),
        )
        return

    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return

    await show_main_menu(message, state)


@router.message(Command("sync_projects"))
async def sync_projects_handler(message: Message):
    if not is_admin_message(message):
        return
    try:
        result = await run_sync(sync_projects)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    except Exception:
        logging.exception("Yougile sync requested by administrator failed")
        await message.answer("Не удалось обновить проекты Yougile. Локальный справочник сохранён.")
        return
    await message.answer(f"Проекты Yougile обновлены: {result['synced']}.")


@router.message(Command("past_requests"))
async def past_requests_handler(message: Message):
    if is_admin_message(message):
        await show_past_requests(message)


@router.message(Command("my_logs"))
async def my_logs_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return

    await message.answer(await run_sync(get_current_month_logs_text, worker_id), reply_markup=back_to_menu_keyboard())


@router.message(Command("my_expenses"))
async def my_expenses_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return

    await message.answer(await run_sync(get_current_month_expenses_text, worker_id), reply_markup=back_to_menu_keyboard())


@router.message(Command("sf_tasks"))
async def standflow_tasks_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return
    await _send_standflow_task_cards(message, due_today=False)


@router.message(Command("sf_today"))
async def standflow_today_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return
    await _send_standflow_task_cards(message, due_today=True)


@router.message(Command("sf_reminders"))
async def standflow_reminders_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return

    try:
        result = await run_sync(get_reminders, message.from_user.id)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    cards = result.get("cards") or []
    if not cards:
        await message.answer("Сейчас нет срочных напоминаний StandFlow.", reply_markup=standflow_keyboard())
        return

    for card in cards:
        await message.answer(card["text"])
    await message.answer("Что смотрим дальше?", reply_markup=standflow_keyboard())


@router.message(Command("sf_summary"))
async def standflow_summary_handler(message: Message):
    worker_id = await get_accessible_worker(message)
    if not worker_id:
        return

    try:
        result = await run_sync(get_daily_summary, message.from_user.id)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    await message.answer(result["text"], reply_markup=standflow_keyboard())


@router.message(WorkState.waiting_standflow_task_comment)
async def handle_standflow_task_comment(message: Message, state: FSMContext):
    if not is_private(message):
        return

    content = (message.text or "").strip()
    if not content:
        await message.answer("Напишите текст комментария одним сообщением.", reply_markup=back_to_menu_keyboard())
        return

    state_data = await state.get_data()
    task_id = state_data.get("standflow_task_id")
    if not task_id:
        await state.clear()
        await message.answer(
            "Не удалось понять, к какой задаче добавить комментарий. "
            "Откройте список задач и нажмите \"Комментарий\" еще раз.",
            reply_markup=standflow_keyboard(),
        )
        return

    try:
        await run_sync(add_task_comment, message.from_user.id, int(task_id), content)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    await state.clear()
    await message.answer("Комментарий добавлен в задачу StandFlow.", reply_markup=standflow_keyboard())


@router.message(WorkState.waiting_standflow_link_code)
async def handle_standflow_link_code(message: Message, state: FSMContext):
    if not is_private(message):
        return

    code = (message.text or "").strip().upper().replace(" ", "")
    if not code:
        await message.answer(
            "Введите код привязки StandFlow одним сообщением.\n"
            "Он выглядит примерно так: A1B2C3D4",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    try:
        result = await run_sync(confirm_telegram_link, code, message.from_user.id)
    except StandFlowError as exc:
        await message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
        return

    await state.clear()
    user = result.get("user") or {}
    full_name = user.get("full_name") or "вашим пользователем"
    await message.answer(
        f"Готово, Telegram привязан к StandFlow-пользователю: {full_name}.\n\n"
        "Теперь можно открыть \"Сводка дня\" или \"Мои задачи\".",
        reply_markup=standflow_keyboard(),
    )


@router.message(WorkState.waiting_standflow_issue_description)
async def handle_standflow_issue_description(message: Message, state: FSMContext):
    if not is_private(message):
        return

    description = (message.text or "").strip()
    if len(description) < 5:
        await message.answer(
            "Опишите проблему чуть подробнее одним сообщением.\n"
            "Например: \"На объекте не хватает двух панелей, монтаж остановлен\".",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await state.update_data(standflow_issue_description=description)
    await state.set_state(WorkState.waiting_standflow_issue_photo)
    await message.answer(
        "Если есть фото проблемы, отправьте его следующим сообщением.\n"
        "Если фото нет, нажмите \"Без фото\".",
        reply_markup=standflow_issue_photo_keyboard(),
    )


@router.message(WorkState.waiting_standflow_issue_photo)
async def handle_standflow_issue_photo(message: Message, state: FSMContext):
    if not is_private(message):
        return

    file_id = None
    filename = "standflow-issue-photo.jpg"
    content_type = "image/jpeg"
    if message.photo:
        file_id = message.photo[-1].file_id
    elif message.document:
        file_id = message.document.file_id
        filename = message.document.file_name or "standflow-issue-attachment.bin"
        content_type = message.document.mime_type or "application/octet-stream"

    if not file_id:
        await message.answer(
            "Отправьте фото или файл проблемы. Если вложения нет, нажмите \"Без фото\".",
            reply_markup=standflow_issue_photo_keyboard(),
        )
        return

    buffer = BytesIO()
    await message.bot.download(file_id, destination=buffer)
    await _submit_standflow_issue(
        message,
        state,
        upload_filename=filename,
        upload_content=buffer.getvalue(),
        upload_content_type=content_type,
    )


@router.message(WorkState.waiting_date)
async def handle_date(message: Message, state: FSMContext):
    if not is_private(message):
        return

    if not message.text or not message.text.isdigit():
        await message.answer(
            'Введите, пожалуйста, число месяца от 1 до 31.\n'
            'Например: 14'
        )
        return

    work_day = int(message.text)
    is_valid, error_text = validate_current_month_day(work_day)
    if not is_valid:
        reply_markup = future_date_keyboard() if work_day > date.today().day else back_to_menu_keyboard()
        await message.answer(error_text, reply_markup=reply_markup)
        return

    await state.update_data(date=work_day)
    await state.set_state(WorkState.waiting_place)
    await message.answer(
        'Дата записана.\n\n'
        'Теперь выберите, что именно вы хотите внести:',
        reply_markup=work_type_keyboard(),
    )


@router.message(WorkState.waiting_place)
async def handle_waiting_place_text(message: Message):
    if not is_private(message):
        return
    await message.answer(
        'Чтобы продолжить, выберите тип записи кнопками ниже.',
        reply_markup=work_type_keyboard(),
    )


@router.message(WorkState.waiting_project)
async def handle_project(message: Message, state: FSMContext):
    if not is_private(message):
        return
    projects = await run_sync(get_active_projects)
    if not projects:
        await message.answer('Нет активных проектов. Обратитесь к администратору.', reply_markup=back_to_menu_keyboard())
        return
    await message.answer('Выберите проект кнопкой ниже.', reply_markup=project_picker_keyboard(projects))


@router.message(WorkState.waiting_hours)
async def handle_hours(message: Message, state: FSMContext):
    try:
        hours = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer(
            'Введите количество часов числом.\n'
            'Например: 8 или 7.5'
        )
        return

    if hours <= 0:
        await message.answer('Количество часов должно быть больше нуля.')
        return

    await state.update_data(hours=hours)
    await show_summary(message, state)


@router.message(WorkState.waiting_expense_type)
async def handle_expense_type(message: Message, state: FSMContext):
    expense_type = (message.text or "").strip()
    if not expense_type:
        await message.answer('Напишите, пожалуйста, краткое описание расхода.')
        return

    await state.update_data(expense_type=expense_type)
    await state.set_state(WorkState.waiting_expense_amount)
    await message.answer('Теперь укажите сумму расхода.')


@router.message(WorkState.waiting_expense_amount)
async def handle_expense_amount(message: Message, state: FSMContext):
    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer(
            'Введите сумму числом.\n'
            'Например: 1500 или 1500.50'
        )
        return

    if amount <= 0:
        await message.answer('Сумма должна быть больше нуля.')
        return

    await state.update_data(amount=amount)
    await state.set_state(WorkState.waiting_expense_receipt)
    await message.answer(
        'Теперь приложите чек фотографией или файлом.\n'
        'Если чека нет, нажмите кнопку ниже.',
        reply_markup=expense_receipt_keyboard(),
    )


@router.message(WorkState.waiting_expense_receipt)
async def handle_expense_receipt(message: Message, state: FSMContext):
    receipt_file_id = None
    receipt_kind = None

    if message.photo:
        receipt_kind = "photo"
        receipt_file_id = message.photo[-1].file_id
        receipt_original_name = None
    elif message.document:
        receipt_kind = "document"
        receipt_file_id = message.document.file_id
        receipt_original_name = message.document.file_name
    else:
        receipt_original_name = None

    if not receipt_file_id or not receipt_kind:
        await message.answer(
            'Отправьте фотографию или файл с чеком.\n'
            'Если чека нет, нажмите кнопку «Продолжить без чека».',
            reply_markup=expense_receipt_keyboard(),
        )
        return

    await state.update_data(
        receipt_kind=receipt_kind,
        receipt_file_id=receipt_file_id,
        receipt_original_name=receipt_original_name,
    )
    await show_summary(message, state)


@router.message(WorkState.choosing_action)
async def handle_menu_text(message: Message):
    if not is_private(message):
        return
    await message.answer(
        'Пожалуйста, воспользуйтесь кнопками главного меню.',
        reply_markup=admin_main_menu_keyboard() if is_admin(message.from_user.id) else main_menu_keyboard(),
    )


@router.message(WorkState.choosing_stats_period)
async def handle_stats_period_text(message: Message):
    if not is_private(message):
        return
    await message.answer(
        'Выберите период кнопками ниже.',
        reply_markup=stats_period_keyboard(),
    )


@router.message(WorkState.waiting_past_month_date)
async def handle_past_month_date(message: Message, state: FSMContext):
    if not is_private(message):
        return

    try:
        request_date = parse_previous_month_date(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(request_date=request_date.isoformat())
    await state.set_state(WorkState.waiting_past_month_type)
    await message.answer(
        'Теперь выберите, что нужно передать администратору за эту дату.',
        reply_markup=past_month_type_keyboard(),
    )


@router.message(WorkState.waiting_past_month_type)
async def handle_past_month_type(message: Message):
    if not is_private(message):
        return
    await message.answer(
        'Чтобы продолжить, выберите тип записи кнопками ниже.',
        reply_markup=past_month_type_keyboard(),
    )


@router.message(WorkState.waiting_past_month_project)
async def handle_past_month_project(message: Message, state: FSMContext):
    if not is_private(message):
        return
    projects = await run_sync(get_active_projects)
    if not projects:
        await message.answer('Нет активных проектов. Обратитесь к администратору.', reply_markup=back_to_menu_keyboard())
        return
    await message.answer('Выберите проект кнопкой ниже.', reply_markup=project_picker_keyboard(projects))


@router.message(WorkState.waiting_past_month_hours)
async def handle_past_month_hours(message: Message, state: FSMContext):
    if not is_private(message):
        return

    try:
        hours = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer('Введите количество часов числом. Например: 8 или 7.5', reply_markup=back_to_menu_keyboard())
        return

    if hours <= 0:
        await message.answer('Количество часов должно быть больше нуля.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(hours=hours)
    data = await state.get_data()
    request_date = date.fromisoformat(data["request_date"])
    type_label = 'Монтаж' if data.get("entry_type") == "install" else 'Смена'
    lines = [
        'Проверьте запрос перед отправкой администратору:',
        f"Дата: {request_date:%d.%m.%Y}",
        f"Тип записи: {type_label}",
    ]
    if data.get("project"):
        lines.append(f"Проект: {data['project']}")
    lines.append(f"Часы: {hours}")
    await state.set_state(WorkState.confirm_past_month)
    await message.answer("\n".join(lines), reply_markup=confirm_keyboard())


@router.message(WorkState.waiting_past_month_expense_description)
async def handle_past_month_expense_description(message: Message, state: FSMContext):
    if not is_private(message):
        return

    expense_type = (message.text or "").strip()
    if not expense_type:
        await message.answer('Напишите, пожалуйста, краткое описание расхода.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(expense_type=expense_type)
    await state.set_state(WorkState.waiting_past_month_expense_amount)
    await message.answer('Теперь укажите сумму расхода.', reply_markup=back_to_menu_keyboard())


@router.message(WorkState.waiting_past_month_expense_amount)
async def handle_past_month_expense_amount(message: Message, state: FSMContext):
    if not is_private(message):
        return

    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer('Введите сумму числом. Например: 1500 или 1500.50', reply_markup=back_to_menu_keyboard())
        return

    if amount <= 0:
        await message.answer('Сумма должна быть больше нуля.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(amount=amount)
    data = await state.get_data()
    request_date = date.fromisoformat(data["request_date"])
    lines = [
        'Проверьте запрос перед отправкой администратору:',
        f"Дата: {request_date:%d.%m.%Y}",
        'Тип записи: Расход',
        f"Проект: {data.get('project')}",
        f"Описание расхода: {data['expense_type']}",
        f"Сумма: {amount}",
    ]
    await state.set_state(WorkState.confirm_past_month)
    await message.answer("\n".join(lines), reply_markup=confirm_keyboard())


@router.message(WorkState.admin_waiting_worker_id)
async def handle_admin_worker_id(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer('Введите числовой ID сотрудника.', reply_markup=back_to_menu_keyboard())
        return

    worker_id = int(text)
    data = await state.get_data()
    action = data.get("admin_action")
    await state.update_data(worker_id=worker_id)

    if action == "set_rate":
        await state.set_state(WorkState.admin_waiting_work_type)
        await message.answer('Выберите тип работ для ставки.', reply_markup=admin_work_type_keyboard())
        return

    if action in {"add_bonus", "add_penalty"}:
        await state.set_state(WorkState.admin_waiting_amount)
        label = 'премии' if action == "add_bonus" else 'штрафа'
        await message.answer(f'Введите сумму {label}.', reply_markup=back_to_menu_keyboard())
        return

    if action == "show_rates":
        await worker_rates_by_id(message, worker_id)
        await state.clear()
        return

    if action == "show_bonuses":
        await bonuses_by_worker_id(message, worker_id)
        await state.clear()
        return

    if action == "show_penalties":
        await penalties_by_worker_id(message, worker_id)
        await state.clear()
        return

    if action == "merge_workers":
        await state.set_state(WorkState.admin_waiting_second_worker_id)
        await message.answer('Введите ID записи-дубля, которую нужно влить в основную.', reply_markup=back_to_menu_keyboard())
        return


@router.message(WorkState.admin_waiting_work_type)
async def handle_admin_work_type(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    normalized = (message.text or "").strip().lower()
    mapping = {
        'смена': "shift",
        'монтаж': "install",
        "shift": "shift",
        "install": "install",
    }
    work_type = mapping.get(normalized)
    if not work_type:
        await message.answer('Выберите тип работ кнопкой или введите: shift / install.', reply_markup=admin_work_type_keyboard())
        return

    await state.update_data(work_type=work_type)
    await state.set_state(WorkState.admin_waiting_rate)
    await message.answer('Введите ставку в час.', reply_markup=back_to_menu_keyboard())


@router.message(WorkState.admin_waiting_rate)
async def handle_admin_rate(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    try:
        rate_value = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer('Введите ставку числом. Например: 500 или 650.50', reply_markup=back_to_menu_keyboard())
        return

    if rate_value <= 0:
        await message.answer('Ставка должна быть больше нуля.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(rate_value=rate_value)
    await show_admin_confirmation(message, state, 'Проверьте данные перед сохранением ставки:')


@router.message(WorkState.admin_waiting_amount)
async def handle_admin_amount(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    try:
        amount = float((message.text or "").replace(",", "."))
    except ValueError:
        await message.answer('Введите сумму числом. Например: 1500 или 2500.50', reply_markup=back_to_menu_keyboard())
        return

    if amount <= 0:
        await message.answer('Сумма должна быть больше нуля.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(amount=amount)
    await state.set_state(WorkState.admin_waiting_description)
    await message.answer('Введите описание.', reply_markup=back_to_menu_keyboard())


@router.message(WorkState.admin_waiting_description)
async def handle_admin_description(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    description = (message.text or "").strip()
    if not description:
        await message.answer('Описание не должно быть пустым.', reply_markup=back_to_menu_keyboard())
        return

    await state.update_data(description=description)
    action = (await state.get_data()).get("admin_action")
    title = 'Проверьте данные перед назначением премии:' if action == "add_bonus" else 'Проверьте данные перед назначением штрафа:'
    await show_admin_confirmation(message, state, title)


@router.message(WorkState.admin_waiting_second_worker_id)
async def handle_admin_second_worker_id(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    text = (message.text or "").strip()
    if not text.isdigit():
        await message.answer('Введите числовой ID записи-дубля.', reply_markup=back_to_menu_keyboard())
        return

    data = await state.get_data()
    try:
        await merge_workers_by_id(message, data["worker_id"], int(text))
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=back_to_menu_keyboard())
    await state.clear()


@router.message(WorkState.admin_waiting_month)
async def handle_admin_month(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    raw_value = (message.text or "").strip()
    command_text = "/close_month" if not raw_value else f"/close_month {raw_value}"
    try:
        year, month = parse_month_argument(command_text)
    except ValueError:
        await message.answer('Введите месяц в формате MM.YYYY или YYYY-MM.', reply_markup=back_to_menu_keyboard())
        return

    await state.clear()
    await message.answer(f'Закрываю месяц {month:02d}.{year} и обновляю рабочий лист. Это может занять несколько секунд.')
    try:
        result = await asyncio.to_thread(close_month_tracked, year, month, "telegram_manual")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=back_to_menu_keyboard())
        return
    except Exception as exc:
        await message.answer(google_error_message(exc), reply_markup=back_to_menu_keyboard())
        return

    if result["status"] == "busy":
        await message.answer('Этот месяц уже закрывается в другом процессе. Попробуйте ещё раз через минуту.', reply_markup=admin_panel_keyboard())
        return
    if result["status"] == "already_closed":
        archived = result["archived_month"]
        current = result["current_month"]
        await message.answer(
            'Этот месяц уже был закрыт ранее.\n'
            f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
            f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
            reply_markup=admin_panel_keyboard(),
        )
        return

    archived = result["archived_month"]
    current = result["current_month"]
    await message.answer(
        'Месяц успешно закрыт.\n'
        f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
        f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
        reply_markup=admin_panel_keyboard(),
    )


@router.message(WorkState.admin_confirm)
async def handle_admin_confirm_text(message: Message):
    if not is_admin_message(message):
        return
    await message.answer('Используйте кнопки Подтвердить или Отменить.', reply_markup=admin_confirm_keyboard())


@router.message(WorkState.confirm_past_month)
async def handle_confirm_past_month(message: Message):
    if not is_private(message):
        return
    await message.answer('Используйте кнопки Подтвердить или Отменить.', reply_markup=confirm_keyboard())


async def show_summary(message: Message, state: FSMContext):
    data = await state.get_data()

    text = [
        'Проверьте данные перед сохранением:',
        f"Дата: {data.get('date')}",
        f"Тип записи: {data.get('place')}",
    ]

    if data.get("project"):
        text.append(f"Проект: {data.get('project')}")
    if data.get("hours") is not None:
        text.append(f"Часы: {data.get('hours')}")
    if data.get("expense_type"):
        text.append(f"Описание расхода: {data.get('expense_type')}")
    if data.get("amount") is not None:
        text.append(f"Сумма: {data.get('amount')}")
    if data.get("place") == 'Расходы':
        text.append(f"Чек: {('приложен' if data.get('receipt_file_id') else 'без чека')}")

    await state.set_state(WorkState.confirm)
    await message.answer("\n".join(text), reply_markup=confirm_keyboard())


@router.callback_query()
async def callbacks(callback: CallbackQuery, state: FSMContext):
    data = callback.data

    if data == 'admin_past_requests' or data.startswith(('past_approve_', 'past_reject_')):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        if data == 'admin_past_requests':
            await show_past_requests(callback.message)
            return
        try:
            request_id = int(data.rsplit('_', 1)[1])
            result_text = await process_request_decision(request_id, data.startswith('past_approve_'), f'telegram:{callback.from_user.id}')
        except ValueError as exc:
            await callback.message.answer(str(exc))
            return
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(result_text)
        return

    if data == "admin_sync_projects":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        try:
            result = await run_sync(sync_projects)
        except ValueError as exc:
            await callback.message.answer(str(exc))
            return
        except Exception:
            logging.exception("Yougile sync requested by administrator failed")
            await callback.message.answer("Не удалось обновить проекты Yougile. Локальный справочник сохранён.")
            return
        await callback.message.answer(f"Проекты Yougile обновлены: {result['synced']}.")
        return

    if data.startswith(("approve_user_", "reject_user_", "approve_expense_", "reject_expense_")):
        if callback.message.chat.id != ADMIN_CHAT_ID or not is_admin(callback.from_user.id):
            await callback.answer('Недостаточно прав для выполнения этого действия.', show_alert=True)
            return

    if data == "menu_home":
        await send_main_menu_from_callback(callback, state)
        return

    if data == "menu_help":
        await callback.answer()
        help_text = admin_help_text() if is_admin_callback(callback) else user_help_text()
        await callback.message.answer(help_text, reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_my_id":
        await callback.answer()
        await send_my_id_info(callback.message, callback.from_user.id)
        return

    if data == "menu_link_vk":
        await callback.answer()
        await send_link_vk_code(callback.message, callback.from_user.id)
        return

    if data == "menu_standflow":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            standflow_intro_text(),
            reply_markup=standflow_keyboard(show_admin=is_admin(callback.from_user.id)),
        )
        return

    if data == "sf_tasks":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        await _send_standflow_task_cards(callback.message, telegram_user_id=callback.from_user.id, due_today=False)
        return

    if data == "sf_today":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        await _send_standflow_task_cards(callback.message, telegram_user_id=callback.from_user.id, due_today=True)
        return

    if data == "sf_reminders":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        try:
            result = await run_sync(get_reminders, callback.from_user.id)
        except StandFlowError as exc:
            await callback.message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
            return
        cards = result.get("cards") or []
        if not cards:
            await callback.message.answer("Сейчас нет срочных напоминаний StandFlow.", reply_markup=standflow_keyboard())
            return
        for card in cards:
            await callback.message.answer(card["text"])
        await callback.message.answer("Что смотрим дальше?", reply_markup=standflow_keyboard())
        return

    if data == "sf_summary":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        try:
            result = await run_sync(get_daily_summary, callback.from_user.id)
        except StandFlowError as exc:
            await callback.message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
            return
        await callback.message.answer(result["text"], reply_markup=standflow_keyboard())
        return

    if data == "sf_link":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        await state.set_state(WorkState.waiting_standflow_link_code)
        await callback.message.answer(
            "Введите код привязки StandFlow.\n\n"
            "Код выдает администратор StandFlow. Он действует ограниченное время "
            "и нужен только один раз.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "sf_link_admin":
        if not is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        await callback.answer()
        await _send_standflow_link_candidates(callback.message)
        return

    if data.startswith("sf_link_page_"):
        if not is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        page = int(data.removeprefix("sf_link_page_"))
        await callback.answer()
        await _send_standflow_link_candidates(callback.message, page=page)
        return

    if data.startswith("sf_link_user_"):
        if not is_admin(callback.from_user.id):
            await callback.answer("Недостаточно прав.", show_alert=True)
            return
        raw_value = data.removeprefix("sf_link_user_")
        user_id = int(raw_value.split("_p_", 1)[0])
        try:
            result = await run_sync(create_telegram_link_code, user_id)
        except StandFlowError as exc:
            await callback.answer("Ошибка StandFlow.", show_alert=True)
            await callback.message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard(show_admin=True))
            return

        await callback.answer("Код создан.")
        await callback.message.answer(
            "Код привязки StandFlow создан.\n\n"
            f"Код: {result['code']}\n"
            "Передайте его сотруднику. Он должен открыть в боте: "
            "StandFlow -> Привязать StandFlow и отправить этот код.\n\n"
            "Код действует 30 минут.",
            reply_markup=standflow_keyboard(show_admin=True),
        )
        return

    if data == "sf_issue":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        await callback.answer()
        await _send_standflow_issue_projects(callback.message)
        return

    if data.startswith("sf_issue_page_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        page = int(data.removeprefix("sf_issue_page_"))
        await callback.answer()
        await _send_standflow_issue_projects(callback.message, page=page)
        return

    if data.startswith("sf_issue_project_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        raw_value = data.removeprefix("sf_issue_project_")
        project_id = int(raw_value.split("_p_", 1)[0])
        try:
            projects = await run_sync(list_active_projects)
        except StandFlowError as exc:
            await callback.answer("Ошибка StandFlow.", show_alert=True)
            await callback.message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
            return
        selected_project = next((project for project in projects if int(project["id"]) == project_id), None)
        project_label = (
            f"{selected_project['number']} - {selected_project['title']}"
            if selected_project
            else f"Проект #{project_id}"
        )
        await callback.answer()
        await state.update_data(
            standflow_issue_project_id=project_id,
            standflow_issue_project_label=project_label,
        )
        await callback.message.answer(
            f"Проект: {project_label}\n\nВыберите тип проблемы.",
            reply_markup=standflow_issue_type_keyboard(),
        )
        return

    if data.startswith("sf_issue_type_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        issue_type = data.removeprefix("sf_issue_type_")
        await callback.answer()
        await state.update_data(standflow_issue_type=issue_type)
        await callback.message.answer(
            "Насколько проблема срочная?",
            reply_markup=standflow_issue_severity_keyboard(),
        )
        return

    if data.startswith("sf_issue_severity_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        severity = data.removeprefix("sf_issue_severity_")
        await callback.answer()
        await state.update_data(standflow_issue_severity=severity)
        await state.set_state(WorkState.waiting_standflow_issue_description)
        await callback.message.answer(
            "Опишите проблему одним сообщением.\n\n"
            "Например: \"На объекте не хватает двух панелей, монтаж остановлен\".",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "sf_issue_no_photo":
        current_state = await state.get_state()
        if current_state != WorkState.waiting_standflow_issue_photo.state:
            await callback.answer()
            return
        await callback.answer()
        await _submit_standflow_issue(callback.message, state)
        return

    if data.startswith("sf_done_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        task_id = int(data.removeprefix("sf_done_"))
        try:
            await run_sync(complete_task, callback.from_user.id, task_id)
        except StandFlowError as exc:
            await callback.answer("Ошибка StandFlow.", show_alert=True)
            await callback.message.answer(_standflow_error_text(exc), reply_markup=standflow_keyboard())
            return
        await callback.answer("Задача закрыта.")
        await callback.message.answer(
            f"Задача StandFlow #{task_id} отмечена как выполненная.",
            reply_markup=standflow_keyboard(),
        )
        return

    if data.startswith("sf_comment_"):
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return
        task_id = int(data.removeprefix("sf_comment_"))
        await callback.answer()
        await state.update_data(standflow_task_id=task_id)
        await state.set_state(WorkState.waiting_standflow_task_comment)
        await callback.message.answer(
            f"Напишите комментарий к задаче StandFlow #{task_id} одним сообщением.\n\n"
            "Например: \"Созвонился с клиентом, ждем подтверждение макета\".",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "menu_logs":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        await callback.answer()
        await callback.message.answer(await run_sync(get_current_month_logs_text, worker_id), reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_expenses":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        await callback.answer()
        await callback.message.answer(await run_sync(get_current_month_expenses_text, worker_id), reply_markup=back_to_menu_keyboard())
        return

    if data == "menu_admin":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await callback.message.answer('Админ-панель. Выберите нужное действие.', reply_markup=admin_panel_keyboard())
        return

    if data == "admin_users":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(format_active_workers(await run_sync(get_active_workers)), reply_markup=admin_panel_keyboard())
        return

    if data == "admin_pending_users":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await send_pending_users_overview(callback.message)
        return

    if data == "admin_pending_expenses":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await send_pending_expenses_overview(callback.message)
        return

    if data == "admin_adjustments":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await callback.message.answer('Что хотите посмотреть?', reply_markup=admin_adjustments_keyboard())
        return

    worker_picker_action = get_admin_worker_action_from_callback(data)
    if worker_picker_action:
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await start_admin_worker_picker(callback.message, state, worker_picker_action)
        return

    if data == "admin_add_project":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(WorkState.admin_waiting_project_id)
        await callback.message.answer(
            "Введите ID проекта цифрами без пробелов.\nЕсли ID можно назначить автоматически, отправьте auto.",
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "admin_projects":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await callback.message.answer(
            await run_sync(get_active_projects_text_result),
            reply_markup=admin_panel_keyboard(),
        )
        return

    if data == "admin_delete_project":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        rows = await run_sync(get_active_projects_result)
        if not rows:
            await callback.answer()
            await callback.message.answer(
                'Сейчас нет активных проектов для удаления.',
                reply_markup=admin_panel_keyboard(),
            )
            return
        await callback.answer()
        await callback.message.answer(
            'Выберите проект, который нужно удалить из активного списка.',
            reply_markup=project_picker_keyboard(rows, page=0, mode="delete"),
        )
        return

    if data.startswith("admin_project_page_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        page = int(data.removeprefix("admin_project_page_"))
        rows = await run_sync(get_active_projects_result)
        if not rows:
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
            return
        await callback.answer()
        await callback.message.edit_reply_markup(
            reply_markup=project_picker_keyboard(rows, page=page, mode="delete")
        )
        return

    if data.startswith("admin_project_delete_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        raw = data.removeprefix("admin_project_delete_")
        project_id = int(raw.rsplit("_page_", 1)[0])
        result = await run_sync(delete_project_result, project_id)
        if result is None:
            await callback.answer('Проект не найден.', show_alert=True)
            return
        if result.get("already_inactive"):
            await callback.answer('Проект уже не активен.', show_alert=True)
            return
        try:
            await run_sync(sync_projects_reference_sheet)
        except Exception:
            logger.exception("Failed to sync projects reference sheet after deleting a project.")
        await callback.answer('Проект удалён.')
        await callback.message.answer(result["message_text"], reply_markup=admin_panel_keyboard())
        return

    if data == "admin_close_month":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await callback.message.answer(
            'Выберите месяц для закрытия или введите его вручную.',
            reply_markup=admin_month_keyboard(),
        )
        return

    if data == "admin_cancel":
        await callback.answer()
        await state.clear()
        await callback.message.answer('Действие администратора отменено.', reply_markup=admin_panel_keyboard())
        return

    if data == "admin_work_type_shift":
        await callback.answer()
        await state.update_data(work_type="shift")
        await state.set_state(WorkState.admin_waiting_rate)
        await callback.message.answer('Введите ставку в час.', reply_markup=back_to_menu_keyboard())
        return

    if data == "admin_work_type_install":
        await callback.answer()
        await state.update_data(work_type="install")
        await state.set_state(WorkState.admin_waiting_rate)
        await callback.message.answer('Введите ставку в час.', reply_markup=back_to_menu_keyboard())
        return

    if data == "admin_month_manual":
        await callback.answer()
        await state.set_state(WorkState.admin_waiting_month)
        await callback.message.answer('Введите месяц в формате MM.YYYY или YYYY-MM.', reply_markup=back_to_menu_keyboard())
        return

    if data.startswith("admin_month_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        raw_value = data.removeprefix("admin_month_")
        await callback.answer()
        try:
            year, month = parse_month_argument(f"/close_month {raw_value}")
            result = await asyncio.to_thread(close_month_tracked, year, month, "telegram_manual")
        except ValueError as exc:
            await callback.message.answer(str(exc), reply_markup=admin_panel_keyboard())
            return
        except Exception as exc:
            await callback.message.answer(google_error_message(exc), reply_markup=admin_panel_keyboard())
            return

        if result["status"] == "busy":
            await callback.message.answer('Этот месяц уже закрывается в другом процессе. Попробуйте ещё раз через минуту.', reply_markup=admin_panel_keyboard())
            return
        if result["status"] == "already_closed":
            archived = result["archived_month"]
            current = result["current_month"]
            await callback.message.answer(
                'Этот месяц уже был закрыт ранее.\n'
                f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
                f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
                reply_markup=admin_panel_keyboard(),
            )
            return

        archived = result["archived_month"]
        current = result["current_month"]
        await callback.message.answer(
            'Месяц успешно закрыт.\n'
            f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
            f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
            reply_markup=admin_panel_keyboard(),
        )
        return

    if data.startswith("admin_pick_merge_source_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        source_worker_id = int(data.rsplit("_", 1)[1])
        state_data = await state.get_data()
        await callback.answer()
        try:
            await merge_workers_by_id(callback.message, state_data["worker_id"], source_worker_id)
        except ValueError as exc:
            await callback.message.answer(str(exc), reply_markup=admin_panel_keyboard())
        await state.clear()
        return

    if data.startswith("admin_pick_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        payload = data.removeprefix("admin_pick_")
        action, worker_id_raw = payload.rsplit("_", 1)
        worker_id = int(worker_id_raw)
        await callback.answer()
        await state.update_data(admin_action=action, worker_id=worker_id)

        await handle_admin_worker_pick_action(callback.message, state, action, worker_id)
        return

    if data.startswith("admin_page_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        payload = data.removeprefix("admin_page_")
        action, page_raw = payload.rsplit("_", 1)
        page = max(0, int(page_raw))
        rows = await run_sync(get_active_workers)
        if not rows:
            await callback.answer()
            await callback.message.edit_text('Сейчас нет активных подтвержденных пользователей.')
            await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
            return
        await callback.answer()
        await callback.message.edit_text(get_admin_worker_prompt(action))
        await callback.message.edit_reply_markup(reply_markup=admin_worker_picker_keyboard(action, rows, page=page))
        return

    if data.startswith("pending_users_page_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        page = max(0, int(data.removeprefix("pending_users_page_")))
        rows = await run_sync(get_pending_workers)
        await callback.answer()
        if not rows:
            await callback.message.edit_text('Сейчас новых заявок на регистрацию нет.')
            await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
            return
        start = page * 5
        page_rows = rows[start:start + 5]
        total_pages = (len(rows) - 1) // 5 + 1
        lines = [f'Заявки на регистрацию, страница {page + 1}/{total_pages}:']
        for worker_id, full_name, chat_id in page_rows:
            lines.append(f"{worker_id}. {full_name} (chat_id: {chat_id})")
        await callback.message.edit_text("\n".join(lines))
        await callback.message.edit_reply_markup(reply_markup=pending_users_page_keyboard(rows, page=page))
        return

    if data.startswith("pending_expenses_page_"):
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        page = max(0, int(data.removeprefix("pending_expenses_page_")))
        rows = await run_sync(get_pending_expenses)
        await callback.answer()
        if not rows:
            await callback.message.edit_text('Сейчас нет расходов, ожидающих подтверждения.')
            await callback.message.edit_reply_markup(reply_markup=admin_panel_keyboard())
            return
        start = page * 5
        page_rows = rows[start:start + 5]
        total_pages = (len(rows) - 1) // 5 + 1
        lines = [f'Расходы на подтверждение, страница {page + 1}/{total_pages}:']
        for expense_id, full_name, amount, description, expense_date in page_rows:
            lines.append(f"{expense_id}. {full_name} | {amount} | {description} | {expense_date}")
        await callback.message.edit_text("\n".join(lines))
        await callback.message.edit_reply_markup(reply_markup=pending_expenses_page_keyboard(rows, page=page))
        return

    if data == "admin_confirm_yes":
        if not is_admin_callback(callback):
            await callback.answer('Недостаточно прав.', show_alert=True)
            return
        state_data = await state.get_data()
        await callback.answer()
        if state_data.get("admin_action") == "set_rate":
            await save_rate_by_id(callback.message, state_data["worker_id"], state_data["work_type"], state_data["rate_value"])
        elif state_data.get("admin_action") == "add_bonus":
            await add_bonus_by_id(callback.message, state_data["worker_id"], state_data["amount"], state_data["description"])
        elif state_data.get("admin_action") == "add_penalty":
            await add_penalty_by_id(callback.message, state_data["worker_id"], state_data["amount"], state_data["description"])
        elif state_data.get("admin_action") == "make_admin":
            await set_admin_by_id(callback.message, state_data["worker_id"], True)
        elif state_data.get("admin_action") == "remove_admin":
            await set_admin_by_id(callback.message, state_data["worker_id"], False)
        elif state_data.get("admin_action") == "delete_worker":
            await delete_worker_by_id(callback.message, state_data["worker_id"])
        await state.clear()
        return

    if data == "admin_confirm_no":
        await callback.answer()
        await state.clear()
        await callback.message.answer('Действие отменено.', reply_markup=admin_panel_keyboard())
        return

    if data == "menu_add":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        await callback.answer()
        await state.clear()
        await state.set_state(WorkState.waiting_date)
        await callback.message.answer(
            'Давайте внесем новую запись.\n\n'
            'Введите число месяца, к которому относится запись.\n'
            'Например: 14'
        )
        return

    if data == "menu_past_month":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        await callback.answer()
        await state.clear()
        await state.update_data(worker_id=worker_id, past_request_key=str(uuid.uuid4()))
        await state.set_state(WorkState.waiting_past_month_date)
        await callback.message.answer(
            'Введите полную дату из прошлого месяца в формате ДД.ММ.ГГГГ.\n'
            'Например: 28.06.2026',
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data == "menu_stats":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        await callback.answer()
        await state.set_state(WorkState.choosing_stats_period)
        await callback.message.answer(
            'Выберите, за какой период текущего месяца показать статистику.',
            reply_markup=stats_period_keyboard(),
        )
        return

    if data in {"stats_first_half", "stats_second_half"}:
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        half = "first" if data == "stats_first_half" else "second"
        await callback.answer()
        await callback.message.answer(
            await run_sync(get_half_month_stats_text, worker_id, half),
            reply_markup=back_to_menu_keyboard(),
        )
        return

    if data.startswith("project_page_"):
        page = int(data.removeprefix("project_page_"))
        projects = await run_sync(get_active_projects)
        current_state = await state.get_state()
        if not projects:
            await callback.answer('Список проектов пуст.', show_alert=True)
            if current_state == WorkState.waiting_past_month_project.state:
                await state.update_data(manual_project_entry=False, project_id=None, standflow_project_id=None)
                await callback.message.answer(
                    'Нет активных проектов. Обратитесь к администратору.',
                    reply_markup=back_to_menu_keyboard(),
                )
            else:
                await state.set_state(WorkState.waiting_place)
                await callback.message.answer(
                    'Сейчас нет активных проектов для монтажа.\n'
                    'Обратитесь к администратору или выберите другой тип записи.',
                    reply_markup=work_type_keyboard(),
                )
            return
        await callback.answer()
        await callback.message.edit_reply_markup(reply_markup=project_picker_keyboard(projects, page=page, mode="pick"))
        return

    if data.startswith("project_other_page_"):
        await callback.answer("Выберите проект из справочника. Новый проект добавляет администратор.", show_alert=True)
        return

    if data.startswith("project_pick_"):
        raw = data.removeprefix("project_pick_")
        project_id = int(raw.rsplit("_page_", 1)[0])
        page = int(raw.rsplit("_page_", 1)[1]) if "_page_" in raw else 0
        current_state = await state.get_state()
        if current_state not in {WorkState.waiting_project.state, WorkState.waiting_past_month_project.state}:
            await callback.answer("Выбор проекта уже завершён.")
            return
        project = await run_sync(get_project_by_id, project_id)
        if not project or not project[2]:
            await callback.answer('Проект больше не активен.', show_alert=True)
            projects = await run_sync(get_active_projects)
            if projects:
                await callback.message.edit_reply_markup(
                    reply_markup=project_picker_keyboard(projects, page=page, mode="pick")
                )
            else:
                if current_state == WorkState.waiting_past_month_project.state:
                    await state.update_data(manual_project_entry=False, project_id=None, standflow_project_id=None)
                    await callback.message.answer(
                        'Нет активных проектов. Обратитесь к администратору.',
                        reply_markup=back_to_menu_keyboard(),
                    )
                else:
                    await state.set_state(WorkState.waiting_place)
                    await callback.message.answer(
                        'Сейчас нет активных проектов для монтажа.\n'
                        'Обратитесь к администратору или выберите другой тип записи.',
                        reply_markup=work_type_keyboard(),
                    )
            return

        await callback.answer()
        await state.update_data(project=project[1], project_id=project[0], standflow_project_id=project[0], manual_project_entry=False)
        form = await state.get_data()
        if form.get("place") == "Расходы" or form.get("entry_type") == "expense":
            target = WorkState.waiting_past_month_expense_description if current_state == WorkState.waiting_past_month_project.state else WorkState.waiting_expense_type
            await state.set_state(target)
            await callback.message.answer("Напишите краткое описание расхода.", reply_markup=back_to_menu_keyboard())
            return
        if current_state == WorkState.waiting_past_month_project.state:
            await state.set_state(WorkState.waiting_past_month_hours)
            await callback.message.answer('Укажите, пожалуйста, количество часов по этому монтажу.', reply_markup=back_to_menu_keyboard())
        else:
            await state.set_state(WorkState.waiting_hours)
            await callback.message.answer('Укажите, пожалуйста, количество часов по этому монтажу.')
        return

    if data == "place_install":
        await callback.answer()
        projects = await run_sync(get_active_projects)
        await state.update_data(place='Монтаж', project_id=None, standflow_project_id=None, manual_project_entry=False)
        if not projects:
            await state.update_data(manual_project_entry=False, project_id=None, standflow_project_id=None)
            await state.set_state(WorkState.waiting_project)
            await callback.message.answer(
                'Сейчас нет активных проектов для монтажа.\n'
                'Выберите проект из справочника. Обратитесь к администратору.',
                reply_markup=back_to_menu_keyboard(),
            )
            return
        await state.set_state(WorkState.waiting_project)
        await callback.message.answer(
            'Выберите проект кнопкой ниже.',
            reply_markup=project_picker_keyboard(projects, page=0, mode="pick"),
        )
        return

    if data == "place_shift":
        await callback.answer()
        await state.update_data(place='Смена', project=None, project_id=None, standflow_project_id=None)
        await state.set_state(WorkState.waiting_hours)
        await callback.message.answer('Укажите количество часов по смене.')
        return

    if data == "place_expense":
        await callback.answer()
        await state.update_data(
            place='Расходы',
            project=None,
            project_id=None,
            manual_project_entry=False,
            hours=None,
            receipt_kind=None,
            receipt_file_id=None,
            receipt_original_name=None,
            source_platform="telegram",
            source_peer_id=callback.message.chat.id,
        )
        projects = await run_sync(get_active_projects)
        if not projects:
            await callback.message.answer('Нет активных проектов. Обратитесь к администратору.', reply_markup=work_type_keyboard())
            return
        await state.set_state(WorkState.waiting_project)
        await callback.message.answer('Выберите проект для расхода.', reply_markup=project_picker_keyboard(projects))
        return

    if data == "past_place_install":
        await callback.answer()
        projects = await run_sync(get_active_projects)
        await state.update_data(entry_type="install", project_id=None, manual_project_entry=False)
        if not projects:
            await state.update_data(manual_project_entry=False, project_id=None, standflow_project_id=None)
            await state.set_state(WorkState.waiting_past_month_project)
            await callback.message.answer(
                'Нет активных проектов. Обратитесь к администратору.',
                reply_markup=back_to_menu_keyboard(),
            )
            return
        await state.set_state(WorkState.waiting_past_month_project)
        await callback.message.answer(
            'Выберите проект кнопкой ниже.',
            reply_markup=project_picker_keyboard(projects, page=0, mode="pick"),
        )
        return

    if data == "past_place_shift":
        await callback.answer()
        await state.update_data(entry_type="shift", project=None, project_id=None, manual_project_entry=False)
        await state.set_state(WorkState.waiting_past_month_hours)
        await callback.message.answer('Укажите количество часов по смене.', reply_markup=back_to_menu_keyboard())
        return

    if data == "past_place_expense":
        await callback.answer()
        projects = await run_sync(get_active_projects)
        if not projects:
            await callback.message.answer('Нет активных проектов. Обратитесь к администратору.', reply_markup=back_to_menu_keyboard())
            return
        await state.update_data(entry_type="expense", project=None, project_id=None, hours=None, manual_project_entry=False)
        await state.set_state(WorkState.waiting_past_month_project)
        await callback.message.answer('Выберите проект для расхода.', reply_markup=project_picker_keyboard(projects))
        return

    if data == "expense_skip_receipt":
        current_state = await state.get_state()
        if current_state != WorkState.waiting_expense_receipt.state:
            await callback.answer()
            return

        await callback.answer()
        await state.update_data(receipt_kind=None, receipt_file_id=None, receipt_original_name=None)
        await show_summary(callback.message, state)
        return

    if data.startswith("approve_user_"):
        worker_id, page = parse_paged_action(data, "approve_user_")
        result = await run_sync(approve_user_result, worker_id)
        if not result["ok"]:
            await callback.answer(result["error"], show_alert=True)
            return
        if page is None:
            await finalize_admin_action(callback, 'Регистрация пользователя подтверждена.')
        else:
            await callback.answer('Заявка одобрена.')
        await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])
        if page is not None:
            await refresh_pending_users_message(callback, page)
        return

    if data.startswith("reject_user_"):
        worker_id, page = parse_paged_action(data, "reject_user_")
        result = await run_sync(reject_user_result, worker_id)
        if not result["ok"]:
            await callback.answer(result["error"], show_alert=True)
            return
        if page is None:
            await finalize_admin_action(callback, 'Заявка на регистрацию отклонена.')
        else:
            await callback.answer('Заявка отклонена.')
        await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])
        if page is not None:
            await refresh_pending_users_message(callback, page)
        return

    if data.startswith("approve_expense_"):
        expense_id, page = parse_paged_action(data, "approve_expense_")
        result = await run_sync(approve_expense_result, expense_id)
        if not result["ok"]:
            await callback.answer(result["error"], show_alert=True)
            return
        if page is None:
            await finalize_admin_action(callback, 'Расход подтвержден.')
        else:
            await callback.answer('Расход подтвержден.')
        schedule_sheet_sync(result["sync_year"], result["sync_month"])
        await notify_expense_origin(
            result["source_platform"],
            result["source_peer_id"],
            result["chat_id"],
            result["vk_id"],
            result["notify_text"],
        )
        if page is not None:
            await refresh_pending_expenses_message(callback, page)
        return

    if data.startswith("show_expense_receipt_"):
        expense_id, _page = parse_paged_action(data, "show_expense_receipt_")
        await callback.answer()
        await show_expense_receipt_by_id(callback.message, expense_id)
        return

    if data.startswith("reject_expense_"):
        expense_id, page = parse_paged_action(data, "reject_expense_")
        result = await run_sync(reject_expense_result, expense_id)
        if not result["ok"]:
            await callback.answer(result["error"], show_alert=True)
            return
        if page is None:
            await finalize_admin_action(callback, 'Расход отклонен.')
        else:
            await callback.answer('Расход отклонен.')
        await notify_expense_origin(
            result["source_platform"],
            result["source_peer_id"],
            result["chat_id"],
            result["vk_id"],
            result["notify_text"],
        )
        if page is not None:
            await refresh_pending_expenses_message(callback, page)
        return

    if data == "confirm_yes":
        worker_id, error_text = await run_sync(get_worker_access, callback.from_user.id)
        if error_text:
            await callback.answer(error_text, show_alert=True)
            return

        form_data = await state.get_data()
        current_state = await state.get_state()
        if current_state == WorkState.confirm_past_month.state:
            try:
                await send_past_month_request_to_admins(callback.message, state, worker_id)
            except ValueError as exc:
                await callback.answer()
                await callback.message.answer(str(exc), reply_markup=back_to_menu_keyboard())
                return
            await callback.answer()
            await callback.message.answer(
                'Заявка отправлена администраторам. После одобрения данные появятся в базе и архивном отчёте.',
                reply_markup=back_to_menu_keyboard(),
            )
            await state.clear()
            return

        try:
            expense_id = await run_sync(save_to_db, form_data, worker_id)
        except ValueError as exc:
            await callback.answer()
            await callback.message.answer(str(exc), reply_markup=back_to_menu_keyboard())
            return

        saved_receipt = None
        if form_data.get("place") == 'Расходы' and form_data.get("receipt_file_id"):
            try:
                saved_receipt = await save_telegram_receipt(
                    callback.bot,
                    form_data["receipt_file_id"],
                    expense_id,
                    form_data.get("receipt_kind") or "document",
                    form_data.get("receipt_original_name"),
                )
                await run_sync(
                    update_expense_receipt,
                    expense_id,
                    saved_receipt["path"],
                    saved_receipt["original_name"],
                    saved_receipt["media_type"],
                    form_data.get("receipt_file_id"),
                    None,
                )
            except Exception:
                saved_receipt = None

        await callback.answer()
        standflow_synced = await _sync_work_log_to_standflow(callback.message, callback.from_user.id, form_data)
        await callback.message.answer(
            (
                'Данные успешно сохранены.\n'
                if standflow_synced
                else 'Данные успешно сохранены в боте, но синхронизацию со StandFlow нужно проверить.\n'
            )
            +
            'Если хотите, можете сразу вернуться в главное меню и продолжить работу.',
            reply_markup=back_to_menu_keyboard(),
        )

        if form_data.get("place") == 'Расходы':
            expense_date = date.today().replace(day=int(form_data.get("date")))
            admin_text = format_expense_admin_text(
                callback.from_user.full_name,
                expense_date,
                form_data.get("amount"),
                form_data.get("expense_type"),
                bool(saved_receipt or form_data.get("receipt_file_id")),
                project_name=form_data.get("project"),
            )
            telegram_sent = False
            if form_data.get("receipt_kind") and form_data.get("receipt_file_id"):
                telegram_sent = await send_telegram_media(
                    ADMIN_CHAT_ID,
                    form_data["receipt_kind"],
                    form_data["receipt_file_id"],
                    admin_text,
                    reply_markup=expense_keyboard(expense_id),
                )
            if not telegram_sent:
                await send_telegram_message(
                    ADMIN_CHAT_ID,
                    admin_text,
                    reply_markup=expense_keyboard(expense_id),
                )

            for admin_vk_id in await run_sync(get_admin_vk_ids):
                vk_sent = False
                if saved_receipt:
                    vk_sent = await send_vk_media(admin_vk_id, saved_receipt["path"], admin_text)
                if not vk_sent:
                    await send_vk_message(admin_vk_id, admin_text)
        else:
            schedule_sheet_sync(date.today().year, date.today().month)

        await state.clear()
        return

    if data == "confirm_no":
        await callback.answer()
        current_state = await state.get_state()
        await callback.message.answer(
            (
                'Запрос отменён.\n'
                'Вы можете вернуться в главное меню и начать заново, когда будете готовы.'
                if current_state == WorkState.confirm_past_month.state
                else 'Внесение записи отменено.\n'
                'Вы можете вернуться в главное меню и начать заново, когда будете готовы.'
            ),
            reply_markup=back_to_menu_keyboard(),
        )
        await state.clear()


@router.message(Command("my_id"))
async def my_id_handler(message: Message):
    await send_my_id_info(message, message.from_user.id)


@router.message(Command("retry_last"))
async def retry_last_handler(message: Message, state: FSMContext):
    if not is_private(message):
        return
    await replay_pending_telegram_message(message, state)


@router.message(WorkState.admin_waiting_project_id)
async def handle_admin_project_id(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    raw_value = (message.text or "").strip()
    normalized = raw_value.lower()
    if normalized in {"auto", 'авто', "-"}:
        await state.update_data(project_id=None)
        await state.set_state(WorkState.admin_waiting_project_name)
        await message.answer('Введите название нового проекта.', reply_markup=back_to_menu_keyboard())
        return

    if not raw_value.isdigit():
        await message.answer(
            'Введите ID проекта цифрами без пробелов или отправьте auto, если ID можно назначить автоматически.',
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await state.update_data(project_id=int(raw_value))
    await state.set_state(WorkState.admin_waiting_project_name)
    await message.answer('Введите название нового проекта.', reply_markup=back_to_menu_keyboard())


@router.message(WorkState.admin_waiting_project_name)
async def handle_admin_project_name(message: Message, state: FSMContext):
    if not is_admin_message(message):
        return

    project_name = (message.text or "").strip()
    if not project_name:
        await message.answer('Введите название проекта.', reply_markup=back_to_menu_keyboard())
        return

    try:
        result = await run_sync(add_project_result, project_name, (await state.get_data()).get("project_id"))
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=back_to_menu_keyboard())
        return

    await state.clear()
    try:
        await run_sync(sync_projects_reference_sheet)
    except Exception:
        logger.exception("Failed to sync projects reference sheet after adding a project.")
    await message.answer(result["message_text"], reply_markup=admin_panel_keyboard())


@router.message()
async def fallback_private_message(message: Message, state: FSMContext):
    if not is_private(message):
        return

    current_state = await state.get_state()
    if current_state:
        pending = await run_sync(get_pending_inbound_message, "telegram", str(message.from_user.id))
        if pending and pending.get("error_text"):
            await message.answer(
                'Похоже, предыдущее сообщение осталось без ответа из-за внутренней ошибки.\n'
                'После перезапуска бота оно будет попытано обработаться автоматически.',
                reply_markup=back_to_menu_keyboard(),
            )
            return

        await message.answer(
            f'Сообщение не распознано.\n{pending_state_hint(current_state)}',
            reply_markup=back_to_menu_keyboard(),
        )
        return

    await message.answer(
        'Сообщение не распознано. Используйте кнопки меню или команды /start и /help.',
        reply_markup=admin_main_menu_keyboard() if is_admin(message.from_user.id) else main_menu_keyboard(),
    )

