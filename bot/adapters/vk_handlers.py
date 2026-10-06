import asyncio
import logging
import random
import time
import uuid
from calendar import monthrange
from datetime import date

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from vkbottle import Bot, Keyboard, Text
from vkbottle_types.objects import MessagesMessage

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
from bot.admin_views import get_admin_worker_prompt
from bot.core.dedup import mark_and_check_duplicate, vk_message_key
from bot.core.outgoing_dedup import build_outgoing_key, should_skip_outgoing
from bot.core.notifications import notify_expense_origin, notify_worker, send_telegram_media, send_telegram_message, send_vk_media, send_vk_message
from bot.pending_views import build_pending_expenses_overview_text, build_pending_users_overview_text
from bot.past_month_requests import (
    format_saved_request, telegram_request_keyboard, vk_request_keyboard, process_request_decision,
)
from services.past_month_requests import create_request, list_pending_requests
from bot.shared_text import (
    format_active_workers as shared_format_active_workers,
    format_adjustments as shared_format_adjustments,
    format_expense_admin_text as shared_format_expense_admin_text,
    format_pending_expenses as shared_format_pending_expenses,
    format_pending_users as shared_format_pending_users,
    format_worker_rates as shared_format_worker_rates,
    vk_help_text as shared_vk_help_text,
)
from bot.user_service import (
    get_current_month_expenses_text,
    get_current_month_logs_text,
    get_half_month_stats_text,
)
from config import ADMIN_CHAT_ID
from services.db import (
    clear_pending_inbound_message,
    clear_conversation_state,
    consume_account_link_code,
    create_worker,
    get_active_projects,
    get_active_workers,
    get_admin_vk_ids,
    get_expense_by_id,
    get_pending_inbound_message,
    list_conversations_needing_reply,
    list_active_conversation_states,
    mark_conversation_outbound,
    get_project_by_id,
    get_pending_expenses,
    get_pending_workers,
    load_conversation_state,
    get_worker_by_vk_id,
    get_worker_by_id,
    get_worker_contacts_by_id,
    is_admin_by_vk_id,
    link_vk_to_existing_worker,
    save_to_db,
    save_conversation_state,
    save_conversation_inbound,
    save_pending_inbound_message,
    update_expense_receipt,
)
from services.google_sheets import google_error_message, sync_monthly_report_to_current_sheet, sync_projects_reference_sheet
from services.monthly_closing import close_month_tracked, parse_previous_month_date
from services.receipts import save_remote_receipt
from services.yougile import sync_projects


user_states = {}
user_ui_meta = {}
bot = None
logger = logging.getLogger(__name__)
_handlers_registered = False
_sheet_sync_tasks: dict[tuple[int, int], asyncio.Task] = {}
SLOW_VK_REPLY_SECONDS = 0.75
ADMIN_CACHE_TTL_SECONDS = 30
_admin_status_cache: dict[int, tuple[bool, float]] = {}
WORKER_CACHE_TTL_SECONDS = 10
ACTIVE_WORKERS_CACHE_TTL_SECONDS = 15
PROJECTS_CACHE_TTL_SECONDS = 15
_worker_vk_cache: dict[int, tuple[tuple | None, float]] = {}
_active_workers_cache: tuple[list[tuple[int, str]], float] | None = None
_active_projects_cache: tuple[list[tuple[int, str]], float] | None = None

format_active_workers = shared_format_active_workers
format_adjustments = shared_format_adjustments
format_pending_expenses = shared_format_pending_expenses
format_worker_rates = shared_format_worker_rates


def is_admin_by_vk_id_cached(vk_id: int) -> bool:
    now = time.monotonic()
    cached = _admin_status_cache.get(vk_id)
    if cached and now - cached[1] <= ADMIN_CACHE_TTL_SECONDS:
        return cached[0]

    value = is_admin_by_vk_id(vk_id)
    _admin_status_cache[vk_id] = (value, now)
    return value


def clear_worker_vk_cache(vk_id: int | None = None):
    if vk_id is None:
        _worker_vk_cache.clear()
        return
    _worker_vk_cache.pop(vk_id, None)


def get_worker_by_vk_id_cached(vk_id: int):
    now = time.monotonic()
    cached = _worker_vk_cache.get(vk_id)
    if cached and now - cached[1] <= WORKER_CACHE_TTL_SECONDS:
        return cached[0]

    worker = get_worker_by_vk_id(vk_id)
    _worker_vk_cache[vk_id] = (worker, now)
    return worker


def clear_active_workers_cache():
    global _active_workers_cache
    _active_workers_cache = None


def get_active_workers_cached():
    global _active_workers_cache
    now = time.monotonic()
    if _active_workers_cache and now - _active_workers_cache[1] <= ACTIVE_WORKERS_CACHE_TTL_SECONDS:
        return _active_workers_cache[0]

    rows = get_active_workers()
    _active_workers_cache = (rows, now)
    return rows


def clear_active_projects_cache():
    global _active_projects_cache
    _active_projects_cache = None


def get_active_projects_cached():
    global _active_projects_cache
    now = time.monotonic()
    if _active_projects_cache and now - _active_projects_cache[1] <= PROJECTS_CACHE_TTL_SECONDS:
        return _active_projects_cache[0]

    rows = get_active_projects()
    _active_projects_cache = (rows, now)
    return rows


def format_pending_users(rows):
    return shared_format_pending_users(rows, missing_chat_text='нет Telegram chat_id')


def format_expense_admin_text(full_name: str, expense_date, amount, description: str, has_receipt: bool, *, project_name=None):
    return shared_format_expense_admin_text(
        full_name,
        expense_date,
        amount,
        description,
        has_receipt,
        source_label="VK",
        project_name=project_name,
    )


def vk_help_text(is_admin_user: bool):
    return shared_vk_help_text(is_admin_user)

BTN_ADD = "\u0412\u043d\u0435\u0441\u0442\u0438 \u0438\u043d\u0444\u043e\u0440\u043c\u0430\u0446\u0438\u044e"
BTN_STATS = "\u0421\u0442\u0430\u0442\u0438\u0441\u0442\u0438\u043a\u0430"
BTN_LOGS = "\u041c\u043e\u0438 \u0437\u0430\u043f\u0438\u0441\u0438"
BTN_EXPENSES = "\u041c\u043e\u0438 \u0440\u0430\u0441\u0445\u043e\u0434\u044b"
BTN_HELP = "\u041f\u043e\u043c\u043e\u0449\u044c"
BTN_MENU = "\u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e"
BTN_MY_ID = "\u041c\u043e\u0439 ID"
BTN_LINK_TELEGRAM = "\u041f\u0440\u0438\u0432\u044f\u0437\u0430\u0442\u044c Telegram"
BTN_PAST_MONTH = "\u0417\u0430 \u043f\u0440\u043e\u0448\u043b\u044b\u0439 \u043c\u0435\u0441\u044f\u0446"
BTN_INSTALL = "\u041c\u043e\u043d\u0442\u0430\u0436"
BTN_SHIFT = "\u0421\u043c\u0435\u043d\u0430"
BTN_EXPENSE = "\u0420\u0430\u0441\u0445\u043e\u0434\u044b"
BTN_CONFIRM = "\u041f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c"
BTN_CANCEL = "\u041e\u0442\u043c\u0435\u043d\u0438\u0442\u044c"
BTN_STATS_FIRST = "1-\u044f \u043f\u043e\u043b\u043e\u0432\u0438\u043d\u0430"
BTN_STATS_SECOND = "2-\u044f \u043f\u043e\u043b\u043e\u0432\u0438\u043d\u0430"
BTN_HAVE_ACCOUNT = "\u0423\u0436\u0435 \u0435\u0441\u0442\u044c \u0430\u043a\u043a\u0430\u0443\u043d\u0442"
BTN_NEW_ACCOUNT = "\u041f\u0435\u0440\u0432\u044b\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442"
BTN_ADMIN_USERS = "\u0417\u0430\u044f\u0432\u043a\u0438"
BTN_ADMIN_EXPENSES = "\u0420\u0430\u0441\u0445\u043e\u0434\u044b \u043d\u0430 \u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443"
BTN_ADMIN_HELP = "\u0410\u0434\u043c\u0438\u043d-\u043f\u043e\u043c\u043e\u0449\u044c"
BTN_ADMIN_WORKERS = "\u041f\u043e\u043b\u044c\u0437\u043e\u0432\u0430\u0442\u0435\u043b\u0438"
BTN_ADMIN_SET_RATE = "\u0417\u0430\u0434\u0430\u0442\u044c \u0441\u0442\u0430\u0432\u043a\u0443"
BTN_ADMIN_ADD_BONUS = "\u041d\u0430\u0437\u043d\u0430\u0447\u0438\u0442\u044c \u043f\u0440\u0435\u043c\u0438\u044e"
BTN_ADMIN_ADD_PENALTY = "\u041d\u0430\u0437\u043d\u0430\u0447\u0438\u0442\u044c \u0448\u0442\u0440\u0430\u0444"
BTN_ADMIN_SHOW_RATES = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0441\u0442\u0430\u0432\u043a\u0438"
BTN_ADMIN_SHOW_BONUSES = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043f\u0440\u0435\u043c\u0438\u0438"
BTN_ADMIN_SHOW_PENALTIES = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u0448\u0442\u0440\u0430\u0444\u044b"
BTN_ADMIN_SHOW_PROJECTS = "\u041f\u043e\u043a\u0430\u0437\u0430\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442\u044b"
BTN_ADMIN_MERGE = "\u041e\u0431\u044a\u0435\u0434\u0438\u043d\u0438\u0442\u044c \u0434\u0443\u0431\u043b\u0438"
BTN_ADMIN_MAKE_ADMIN = "\u0421\u0434\u0435\u043b\u0430\u0442\u044c \u0430\u0434\u043c\u0438\u043d\u043e\u043c"
BTN_ADMIN_REMOVE_ADMIN = "\u0421\u043d\u044f\u0442\u044c \u0430\u0434\u043c\u0438\u043d\u0430"
BTN_ADMIN_DELETE_WORKER = "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0441\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0430"
BTN_ADMIN_ADD_PROJECT = "\u0414\u043e\u0431\u0430\u0432\u0438\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442"
BTN_ADMIN_DELETE_PROJECT = "\u0423\u0434\u0430\u043b\u0438\u0442\u044c \u043f\u0440\u043e\u0435\u043a\u0442"
BTN_ADMIN_CLOSE_MONTH = "\u0417\u0430\u043a\u0440\u044b\u0442\u044c \u043c\u0435\u0441\u044f\u0446"
BTN_SKIP_RECEIPT = "\u041f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c \u0431\u0435\u0437 \u0447\u0435\u043a\u0430"
BTN_WORKERS_PREV = "\u25c0 \u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0438"
BTN_WORKERS_NEXT = "\u0421\u043e\u0442\u0440\u0443\u0434\u043d\u0438\u043a\u0438 \u25b6"
BTN_PROJECTS_PREV = "\u25c0 \u041f\u0440\u043e\u0435\u043a\u0442\u044b"
BTN_PROJECTS_NEXT = "\u041f\u0440\u043e\u0435\u043a\u0442\u044b \u25b6"
BTN_PROJECT_OTHER = "\u0414\u0440\u0443\u0433\u043e\u0435"


def set_user_state(vk_id, state, data=None):
    payload = {"state": state, "data": data or {}}
    user_states[vk_id] = payload
    save_conversation_state("vk", str(vk_id), state, payload["data"])


def get_user_state(vk_id):
    cached = user_states.get(vk_id)
    if cached is not None:
        return cached

    stored = load_conversation_state("vk", str(vk_id))
    if not stored or not stored.get("state"):
        return None

    user_states[vk_id] = stored
    return stored


def clear_user_state(vk_id):
    user_states.pop(vk_id, None)
    clear_conversation_state("vk", str(vk_id))


def set_user_meta(vk_id, key, value):
    meta = user_ui_meta.get(vk_id, {})
    meta[key] = value
    user_ui_meta[vk_id] = meta


def get_user_meta(vk_id, key, default=None):
    return user_ui_meta.get(vk_id, {}).get(key, default)


def clear_user_meta(vk_id, *keys):
    if vk_id not in user_ui_meta:
        return
    if not keys:
        user_ui_meta.pop(vk_id, None)
        return
    meta = user_ui_meta[vk_id]
    for key in keys:
        meta.pop(key, None)
    if not meta:
        user_ui_meta.pop(vk_id, None)


def build_keyboard(rows, inline=False, one_time=False):
    keyboard = Keyboard(one_time=one_time, inline=inline)
    for row_index, row in enumerate(rows):
        for label in row:
            keyboard.add(Text(label))
        if row_index < len(rows) - 1:
            keyboard.row()
    return keyboard.get_json()


def start_choice_keyboard():
    return build_keyboard([[BTN_HAVE_ACCOUNT, BTN_NEW_ACCOUNT], [BTN_LINK_TELEGRAM]])


def user_main_menu_keyboard():
    return build_keyboard(
        [
            [BTN_ADD, BTN_STATS],
            [BTN_LOGS, BTN_EXPENSES],
            [BTN_MY_ID, BTN_LINK_TELEGRAM],
            [BTN_PAST_MONTH],
        ]
    )


def admin_menu_keyboard():
    return build_keyboard(
        [
            [BTN_ADD, BTN_STATS],
            [BTN_LOGS, BTN_EXPENSES],
            [BTN_MY_ID, BTN_LINK_TELEGRAM, BTN_PAST_MONTH],
            [BTN_ADMIN_USERS, BTN_ADMIN_EXPENSES],
            [BTN_ADMIN_WORKERS, BTN_ADMIN_SHOW_RATES, BTN_ADMIN_SHOW_PROJECTS],
            ["Обновить проекты Yougile"],
            ["Заявки за прошлый месяц"],
            [BTN_ADMIN_SET_RATE, BTN_ADMIN_ADD_BONUS],
            [BTN_ADMIN_ADD_PENALTY, BTN_ADMIN_SHOW_BONUSES],
            [BTN_ADMIN_SHOW_PENALTIES, BTN_ADMIN_MERGE],
            [BTN_ADMIN_MAKE_ADMIN, BTN_ADMIN_REMOVE_ADMIN, BTN_ADMIN_CLOSE_MONTH],
            [BTN_ADMIN_DELETE_WORKER, BTN_ADMIN_ADD_PROJECT, BTN_ADMIN_DELETE_PROJECT],
        ]
    )


def main_menu_keyboard_for_user(vk_id: int):
    return admin_menu_keyboard() if is_admin_by_vk_id_cached(vk_id) else user_main_menu_keyboard()


def work_type_keyboard():
    return build_keyboard(
        [
            [BTN_INSTALL, BTN_SHIFT],
            [BTN_EXPENSE],
            [BTN_MENU, BTN_CANCEL],
        ]
    )


def admin_work_type_keyboard():
    return build_keyboard(
        [
            [BTN_INSTALL, BTN_SHIFT],
            [BTN_CANCEL, BTN_MENU],
        ]
    )


def stats_period_keyboard():
    return build_keyboard([[BTN_STATS_FIRST, BTN_STATS_SECOND], [BTN_MENU]])


def confirm_keyboard():
    return build_keyboard([[BTN_CONFIRM, BTN_CANCEL], [BTN_MENU]])


def expense_receipt_keyboard():
    return build_keyboard([[BTN_SKIP_RECEIPT], [BTN_CANCEL, BTN_MENU]])


def registration_confirm_keyboard():
    return build_keyboard([[BTN_CONFIRM, BTN_CANCEL]])


def back_to_menu_keyboard(vk_id: int):
    return build_keyboard([[BTN_MENU]])


def admin_shortcuts_keyboard():
    return build_keyboard(
        [
            [BTN_ADMIN_USERS, BTN_ADMIN_EXPENSES],
            [BTN_ADMIN_WORKERS, BTN_ADMIN_SHOW_RATES, BTN_ADMIN_SHOW_PROJECTS],
            ["Обновить проекты Yougile"],
            ["Заявки за прошлый месяц"],
            [BTN_ADMIN_SET_RATE, BTN_ADMIN_ADD_BONUS],
            [BTN_ADMIN_ADD_PENALTY, BTN_ADMIN_SHOW_BONUSES],
            [BTN_ADMIN_SHOW_PENALTIES, BTN_ADMIN_MERGE],
            [BTN_ADMIN_MAKE_ADMIN, BTN_ADMIN_REMOVE_ADMIN],
            [BTN_ADMIN_DELETE_WORKER, BTN_ADMIN_ADD_PROJECT],
            [BTN_ADMIN_DELETE_PROJECT, BTN_ADMIN_CLOSE_MONTH],
            [BTN_MENU],
        ]
    )


def input_step_keyboard():
    return build_keyboard([[BTN_CANCEL, BTN_MENU]])


def future_date_keyboard():
    return build_keyboard([[BTN_PAST_MONTH], [BTN_CANCEL, BTN_MENU]])


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


def pending_state_hint(state_name: str | None) -> str:
    hints = {
        "waiting_for_link_code": "Ожидаю код привязки из Telegram.",
        "waiting_for_full_name": "Ожидаю ваше полное имя.",
        "confirming_name": "Используйте кнопки Подтвердить или Отменить.",
        "waiting_date": "Ожидаю число месяца от 1 до 31, например: 14.",
        "waiting_project": "Выберите проект кнопкой из справочника.",
        "waiting_hours": "Ожидаю количество часов числом, например: 8 или 7.5.",
        "waiting_expense_type": "Ожидаю короткое описание расхода.",
        "waiting_expense_amount": "Ожидаю сумму числом, например: 1500 или 1500.50.",
        "waiting_expense_receipt": "Ожидаю фото или файл чека.",
        "waiting_past_month_date": "Ожидаю полную дату из прошлого месяца в формате ДД.ММ.ГГГГ.",
        "waiting_past_month_type": "Выберите тип записи кнопками ниже.",
        "waiting_past_month_project": "Выберите проект кнопкой из справочника.",
        "waiting_past_month_hours": "Ожидаю количество часов числом.",
        "waiting_past_month_expense_description": "Ожидаю краткое описание расхода.",
        "waiting_past_month_expense_amount": "Ожидаю сумму расхода числом.",
        "confirm_past_month": "Используйте кнопки Подтвердить или Отменить.",
        "choosing_stats_period": "Выберите период кнопками ниже.",
        "admin_waiting_worker_id": "Выберите сотрудника кнопкой или введите его ID.",
        "admin_waiting_work_type": "Выберите тип работ кнопкой или введите shift / install.",
        "admin_waiting_rate": "Ожидаю ставку числом.",
        "admin_waiting_amount": "Ожидаю сумму числом.",
        "admin_waiting_description": "Ожидаю описание.",
        "admin_waiting_second_worker_id": "Ожидаю ID записи-дубля.",
        "admin_waiting_project_id": "Введите ID проекта цифрами или отправьте auto.",
        "admin_waiting_project_name": "Ожидаю название проекта.",
        "admin_waiting_month": "Ожидаю месяц в формате MM.YYYY или YYYY-MM.",
        "admin_confirm": "Используйте кнопки Подтвердить или Отменить.",
        "confirm": "Используйте кнопки Подтвердить или Отменить.",
    }
    return hints.get(state_name or "", "Используйте кнопки меню или введите значение в ожидаемом формате.")


def parse_past_entry_type(text: str):
    normalized = normalize_text(text)
    mapping = {
        normalize_text(BTN_SHIFT): "shift",
        normalize_text(BTN_INSTALL): "install",
        normalize_text(BTN_EXPENSE): "expense",
        "shift": "shift",
        "install": "install",
        "expense": "expense",
    }
    return mapping.get(normalized)


async def start_vk_admin_worker_picker(message: MessagesMessage, action: str):
    set_user_state(message.from_id, "admin_waiting_worker_id", {"admin_action": action})
    set_user_meta(message.from_id, "admin_worker_picker_page", 0)
    worker_rows = await run_sync(get_active_workers_cached)
    await reply(message, get_admin_worker_prompt(action), keyboard=admin_worker_picker_keyboard_paged(worker_rows, page=0))


async def handle_vk_admin_worker_pick_action(message: MessagesMessage, action: str, worker_id: int):
    clear_user_meta(message.from_id, "admin_worker_picker_page")
    worker = await run_sync(get_worker_by_id, worker_id)
    worker_ref = f"{worker[1]} (ID: {worker_id})" if worker else f"ID: {worker_id}"
    if action == "show_rates":
        clear_user_state(message.from_id)
        await worker_rates_request(message, worker_id)
        return

    if action == "show_bonuses":
        clear_user_state(message.from_id)
        await bonuses_request(message, worker_id)
        return

    if action == "show_penalties":
        clear_user_state(message.from_id)
        await penalties_request(message, worker_id)
        return

    if action == "set_rate":
        set_user_state(message.from_id, "admin_waiting_work_type", {"worker_id": worker_id, "worker_ref": worker_ref, "admin_action": action})
        await reply(message, 'Выберите тип работ для ставки.', keyboard=admin_work_type_keyboard())
        return

    if action in {"add_bonus", "add_penalty"}:
        set_user_state(message.from_id, "admin_waiting_amount", {"worker_id": worker_id, "worker_ref": worker_ref, "admin_action": action})
        label = 'премии' if action == "add_bonus" else 'штрафа'
        await reply(message, f'Введите сумму {label} для {worker_ref}.', keyboard=input_step_keyboard())
        return

    if action in {"make_admin", "remove_admin", "delete_worker"}:
        set_user_state(message.from_id, "admin_confirm", {"worker_id": worker_id, "worker_ref": worker_ref, "admin_action": action})
        if action == "make_admin":
            prompt = f'Подтвердить назначение прав администратора для сотрудника {worker_ref}?'
        elif action == "remove_admin":
            prompt = f'Подтвердить снятие прав администратора у сотрудника {worker_ref}?'
        else:
            prompt = (
                f'Подтвердить полное удаление сотрудника {worker_ref}?\n'
                'Будут удалены связанные записи, расходы, ставки, премии и штрафы.'
            )
        await reply(message, prompt, keyboard=registration_confirm_keyboard())
        return

    if action == "merge_workers":
        set_user_state(message.from_id, "admin_waiting_second_worker_id", {"worker_id": worker_id})
        set_user_meta(message.from_id, "admin_worker_picker_page", 0)
        await reply(message, 'Выберите запись-дубль, которую нужно влить в основную.', keyboard=admin_worker_picker_keyboard_paged(page=0))
        return


def admin_month_keyboard():
    today = date.today()
    current = f"{today.month:02d}.{today.year}"
    previous_year = today.year if today.month > 1 else today.year - 1
    previous_month = today.month - 1 if today.month > 1 else 12
    previous = f"{previous_month:02d}.{previous_year}"
    return build_keyboard(
        [
            [f'Текущий: {current}'],
            [f'Предыдущий: {previous}'],
            ['Ввести другой месяц'],
            [BTN_CANCEL, BTN_MENU],
        ]
    )


def admin_worker_picker_keyboard(worker_rows=None):
    return admin_worker_picker_keyboard_paged(worker_rows)


def admin_worker_picker_keyboard_paged(worker_rows=None, page: int = 0, page_size: int = 8):
    rows = []
    source_rows = worker_rows if worker_rows is not None else get_active_workers_cached()
    start = page * page_size
    page_rows = source_rows[start:start + page_size]
    for worker_id, full_name in page_rows:
        rows.append([f"{full_name} ({worker_id})"])
    nav_row = []
    if page > 0:
        nav_row.append(BTN_WORKERS_PREV)
    if start + page_size < len(source_rows):
        nav_row.append(BTN_WORKERS_NEXT)
    if nav_row:
        rows.append(nav_row)
    rows.append([BTN_CANCEL, BTN_MENU])
    return build_keyboard(rows)


def pending_user_action_keyboard(worker_id: int):
    return build_keyboard(
        [
            [f'Одобрить заявку {worker_id}', f'Отклонить заявку {worker_id}'],
            [BTN_MENU],
        ]
    )


def pending_expense_action_keyboard(expense_id: int):
    return build_keyboard(
        [
            [f'Одобрить расход {expense_id}', f'Отклонить расход {expense_id}'],
            [f'Показать чек {expense_id}'],
            [BTN_MENU],
        ]
    )


def pending_users_page_keyboard(rows, page: int = 0, page_size: int = 5):
    start = page * page_size
    page_rows = rows[start:start + page_size]
    keyboard_rows = []
    for worker_id, _full_name, _chat_id in page_rows:
        keyboard_rows.append([f'Одобрить заявку {worker_id}', f'Отклонить заявку {worker_id}'])
    nav_row = []
    if page > 0:
        nav_row.append('◀ Заявки')
    if start + page_size < len(rows):
        nav_row.append('Заявки ▶')
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([BTN_MENU])
    return build_keyboard(keyboard_rows)


def pending_expenses_page_keyboard(rows, page: int = 0, page_size: int = 5):
    start = page * page_size
    page_rows = rows[start:start + page_size]
    keyboard_rows = []
    for expense_id, _full_name, _amount, _description, _expense_date in page_rows:
        keyboard_rows.append([f'Одобрить расход {expense_id}', f'Отклонить расход {expense_id}'])
        keyboard_rows.append([f'Показать чек {expense_id}'])
    nav_row = []
    if page > 0:
        nav_row.append('◀ Расходы')
    if start + page_size < len(rows):
        nav_row.append('Расходы ▶')
    if nav_row:
        keyboard_rows.append(nav_row)
    keyboard_rows.append([BTN_MENU])
    return build_keyboard(keyboard_rows)


def telegram_register_keyboard(worker_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='Одобрить', callback_data=f"approve_user_{worker_id}"),
                InlineKeyboardButton(text='Отклонить', callback_data=f"reject_user_{worker_id}"),
            ]
        ]
    )


def telegram_expense_keyboard(expense_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='Одобрить', callback_data=f"approve_expense_{expense_id}"),
                InlineKeyboardButton(text='Отклонить', callback_data=f"reject_expense_{expense_id}"),
            ]
        ]
    )


def normalize_text(value: str | None):
    return (value or "").strip().lower()


def extract_worker_id_from_label(text: str):
    raw = (text or "").strip()
    if raw.isdigit():
        return int(raw)
    if raw.endswith(")") and "(" in raw:
        candidate = raw.rsplit("(", 1)[1][:-1].strip()
        if candidate.isdigit():
            return int(candidate)
    raise ValueError


async def send_pending_users_overview(message: MessagesMessage, page: int = 0):
    rows = await run_sync(get_pending_workers)
    if not rows:
        clear_user_meta(message.from_id, "pending_users_page")
        await reply(message, format_pending_users(rows), keyboard=admin_shortcuts_keyboard())
        return
    text, view = build_pending_users_overview_text(rows, page=page)
    set_user_meta(message.from_id, "pending_users_page", view["page"])
    await reply(message, text, keyboard=pending_users_page_keyboard(rows, page=view["page"]))


async def send_pending_expenses_overview(message: MessagesMessage, page: int = 0):
    rows = await run_sync(get_pending_expenses)
    if not rows:
        clear_user_meta(message.from_id, "pending_expenses_page")
        await reply(message, format_pending_expenses(rows), keyboard=admin_shortcuts_keyboard())
        return
    text, view = build_pending_expenses_overview_text(rows, page=page)
    set_user_meta(message.from_id, "pending_expenses_page", view["page"])
    await reply(message, text, keyboard=pending_expenses_page_keyboard(rows, page=view["page"]))


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


def extract_vk_receipt_attachment(message: MessagesMessage):
    for attachment in getattr(message, "attachments", []) or []:
        attachment_type = str(getattr(attachment, "type", "")).lower()

        photo = getattr(attachment, "photo", None)
        if photo and "photo" in attachment_type:
            owner_id = getattr(photo, "owner_id", None)
            media_id = getattr(photo, "id", None)
            if owner_id is not None and media_id is not None:
                photo_url = None
                for size in getattr(photo, "sizes", []) or []:
                    candidate = getattr(size, "url", None)
                    if candidate:
                        photo_url = candidate
                return "photo", f"photo{owner_id}_{media_id}", photo_url, None

        document = getattr(attachment, "doc", None)
        if document and "doc" in attachment_type:
            owner_id = getattr(document, "owner_id", None)
            media_id = getattr(document, "id", None)
            if owner_id is not None and media_id is not None:
                return "document", f"doc{owner_id}_{media_id}", getattr(document, "url", None), getattr(document, "title", None)

    return None, None, None, None


def get_vk_access(vk_id: int):
    worker = get_worker_by_vk_id(vk_id)
    if not worker:
        return None, None, (
            '\u0412\u044b \u0435\u0449\u0435 \u043d\u0435 \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u043d\u044b \u0432 \u0441\u0438\u0441\u0442\u0435\u043c\u0435.\n'
            '\u041d\u0430\u0436\u043c\u0438\u0442\u0435 /start, \u0447\u0442\u043e\u0431\u044b \u0437\u0430\u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0438\u0440\u043e\u0432\u0430\u0442\u044c\u0441\u044f \u0438\u043b\u0438 \u043f\u0440\u0438\u0432\u044f\u0437\u0430\u0442\u044c \u0441\u0443\u0449\u0435\u0441\u0442\u0432\u0443\u044e\u0449\u0438\u0439 \u0430\u043a\u043a\u0430\u0443\u043d\u0442.'
        )

    worker_id, registration_status, is_approved, active, full_name = worker
    if registration_status == "rejected":
        return None, full_name, '\u0412\u0430\u0448\u0430 \u0437\u0430\u044f\u0432\u043a\u0430 \u043d\u0430 \u0440\u0435\u0433\u0438\u0441\u0442\u0440\u0430\u0446\u0438\u044e \u0431\u044b\u043b\u0430 \u043e\u0442\u043a\u043b\u043e\u043d\u0435\u043d\u0430. \u0415\u0441\u043b\u0438 \u044d\u0442\u043e \u043e\u0448\u0438\u0431\u043a\u0430, \u0441\u0432\u044f\u0436\u0438\u0442\u0435\u0441\u044c \u0441 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u043e\u043c.'
    if not is_approved:
        return None, full_name, '\u0412\u0430\u0448\u0430 \u0437\u0430\u044f\u0432\u043a\u0430 \u0443\u0436\u0435 \u043e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u0430 \u0438 \u0441\u0435\u0439\u0447\u0430\u0441 \u043d\u0430\u0445\u043e\u0434\u0438\u0442\u0441\u044f \u043d\u0430 \u0440\u0430\u0441\u0441\u043c\u043e\u0442\u0440\u0435\u043d\u0438\u0438 \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0430.'
    if not active:
        return None, full_name, '\u0412\u0430\u0448 \u0430\u043a\u043a\u0430\u0443\u043d\u0442 \u0432\u0440\u0435\u043c\u0435\u043d\u043d\u043e \u0434\u0435\u0430\u043a\u0442\u0438\u0432\u0438\u0440\u043e\u0432\u0430\u043d. \u041e\u0431\u0440\u0430\u0442\u0438\u0442\u0435\u0441\u044c \u043a \u0430\u0434\u043c\u0438\u043d\u0438\u0441\u0442\u0440\u0430\u0442\u043e\u0440\u0443.'
    return worker_id, full_name, None


async def reply(message: MessagesMessage, text: str, keyboard=None, attachment=None):
    outgoing_key = build_outgoing_key("vk", message.peer_id, "message", text=text, attachment=attachment, markup=keyboard)
    if should_skip_outgoing(outgoing_key):
        return

    started_at = time.monotonic()
    await bot.api.messages.send(
        peer_id=message.peer_id,
        message=text,
        keyboard=keyboard,
        attachment=attachment,
        random_id=random.randint(1, 2_147_483_647),
    )
    await run_sync(mark_conversation_outbound, "vk", str(message.from_id))
    await run_sync(clear_pending_inbound_message, "vk", str(message.from_id))
    elapsed = time.monotonic() - started_at
    if elapsed >= SLOW_VK_REPLY_SECONDS:
        logger.warning("Slow VK reply to peer %s took %.3fs", message.peer_id, elapsed)


async def notify_admins(
    text: str,
    telegram_reply_markup=None,
    vk_attachment=None,
    telegram_receipt_kind=None,
    telegram_receipt_url=None,
    telegram_receipt_path=None,
):
    telegram_sent = False
    if telegram_receipt_kind and telegram_receipt_url:
        telegram_sent = await send_telegram_media(
            ADMIN_CHAT_ID,
            telegram_receipt_kind,
            telegram_receipt_url,
            text,
            reply_markup=telegram_reply_markup,
        )

    if not telegram_sent and telegram_receipt_kind and telegram_receipt_path:
        telegram_sent = await send_telegram_media(
            ADMIN_CHAT_ID,
            telegram_receipt_kind,
            telegram_receipt_path,
            text,
            reply_markup=telegram_reply_markup,
        )

    if not telegram_sent:
        await send_telegram_message(ADMIN_CHAT_ID, text, reply_markup=telegram_reply_markup)

    for admin_vk_id in get_admin_vk_ids():
        await bot.api.messages.send(
            peer_id=admin_vk_id,
            message=text,
            attachment=vk_attachment,
            random_id=random.randint(1, 2_147_483_647),
        )


async def show_main_menu(message: MessagesMessage, full_name: str | None = None):
    greeting = f'\u041f\u0440\u0438\u0432\u0435\u0442, {full_name}!\n\n' if full_name else ""
    await reply(
        message,
        f'{greeting}\u0413\u043b\u0430\u0432\u043d\u043e\u0435 \u043c\u0435\u043d\u044e VK-\u0431\u043e\u0442\u0430.\n\u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u0434\u0435\u0439\u0441\u0442\u0432\u0438\u0435 \u043a\u043d\u043e\u043f\u043a\u0430\u043c\u0438 \u043d\u0438\u0436\u0435.',
        keyboard=main_menu_keyboard_for_user(message.from_id),
    )


def past_month_type_keyboard():
    return build_keyboard(
        [
            [BTN_INSTALL, BTN_SHIFT],
            [BTN_EXPENSE],
            [BTN_MENU, BTN_CANCEL],
        ]
    )


async def start_data_entry(message: MessagesMessage):
    set_user_state(message.from_id, "waiting_date", {})
    await reply(
        message,
        'Давайте внесем новую запись.\n\nВведите число месяца, к которому относится запись.\nНапример: 14',
        keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]),
    )


async def start_past_month_entry(message: MessagesMessage):
    worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
    if error_text:
        await reply(message, error_text, keyboard=start_choice_keyboard())
        return

    set_user_state(message.from_id, "waiting_past_month_date", {"worker_id": worker_id, "past_request_key": str(uuid.uuid4())})
    await reply(
        message,
        'Введите полную дату из прошлого месяца в формате ДД.ММ.ГГГГ.\nНапример: 28.06.2026',
        keyboard=input_step_keyboard(),
    )


async def send_past_month_request_to_admins(message: MessagesMessage, state_data: dict):
    worker_id = state_data["worker_id"]
    worker = await run_sync(get_worker_by_id, worker_id)
    full_name = worker[1] if worker else f"ID {worker_id}"
    submission_key = state_data.get('past_request_key') or str(uuid.uuid4())
    state_data = dict(state_data, past_request_key=submission_key)
    set_user_state(message.from_id, 'confirm_past_month', state_data)
    request = await run_sync(create_request, state_data, worker_id, full_name, submission_key, 'vk', message.peer_id)
    request_text = format_saved_request(request)
    await send_telegram_message(ADMIN_CHAT_ID, request_text, reply_markup=telegram_request_keyboard(request['id']))
    for admin_vk_id in await run_sync(get_admin_vk_ids):
        await send_vk_message(admin_vk_id, request_text, keyboard=vk_request_keyboard(request['id']))


async def handle_existing_link(message: MessagesMessage):
    _, full_name, error_text = await run_sync(get_vk_access, message.from_id)
    if error_text:
        await reply(message, error_text, keyboard=start_choice_keyboard())
        return

    await show_main_menu(message, full_name)


async def handle_link_code(message: MessagesMessage, link_code: str):
    try:
        worker = consume_account_link_code(link_code, "vk")
    except ValueError as exc:
        await reply(message, str(exc), keyboard=build_keyboard([[BTN_CANCEL]]))
        return

    worker_id, full_name, _, existing_vk_id, registration_status, is_approved, active, _ = worker
    if existing_vk_id and existing_vk_id != message.from_id:
        await reply(message, 'Этот аккаунт уже привязан к другому VK-профилю. Если это ошибка, обратитесь к администратору.')
        return

    link_vk_to_existing_worker(worker_id, message.from_id)
    clear_worker_vk_cache(message.from_id)
    clear_user_state(message.from_id)

    if registration_status == "rejected":
        await reply(message, f'VK привязан к сотруднику {full_name}, но заявка ранее была отклонена. Свяжитесь с администратором.')
        return

    if not is_approved:
        await reply(message, f'VK привязан к сотруднику {full_name}. Заявка еще ожидает подтверждения администратора.')
        return

    if not active:
        await reply(message, f'VK привязан к сотруднику {full_name}, но профиль сейчас деактивирован. Обратитесь к администратору.')
        return

    await show_main_menu(message, full_name)


async def start_telegram_link_flow(message: MessagesMessage):
    existing_user = await run_sync(get_worker_by_vk_id_cached, message.from_id)
    if existing_user:
        await reply(
            message,
            f'Этот VK-аккаунт уже привязан к сотруднику {existing_user[4]} (ID: {existing_user[0]}).\n\n'
            'Если нужно привязать другой аккаунт, сначала обратитесь к администратору.',
            keyboard=main_menu_keyboard_for_user(message.from_id),
        )
        return

    set_user_state(message.from_id, "waiting_for_link_code")
    await reply(
        message,
        'Введите код привязки из Telegram. Его можно получить кнопкой или командой /link_vk в Telegram-боте.',
        keyboard=build_keyboard([[BTN_CANCEL]]),
    )


async def approve_user_request(message: MessagesMessage, worker_id: int):
    result = await run_sync(approve_user_result, worker_id)
    clear_active_workers_cache()
    if not result["ok"]:
        await reply(message, result["error"], keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, f"Заявка пользователя {result['full_name']} одобрена.", keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])


async def reject_user_request(message: MessagesMessage, worker_id: int):
    result = await run_sync(reject_user_result, worker_id)
    clear_active_workers_cache()
    if not result["ok"]:
        await reply(message, result["error"], keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, f"Заявка пользователя {result['full_name']} отклонена.", keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["chat_id"], result["vk_id"], result["notify_text"])


async def approve_expense_request(message: MessagesMessage, expense_id: int):
    result = await run_sync(approve_expense_result, expense_id)
    if not result["ok"]:
        await reply(message, result["error"], keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, f'Расход #{expense_id} подтвержден.', keyboard=admin_shortcuts_keyboard())
    schedule_sheet_sync(result["sync_year"], result["sync_month"])
    await notify_expense_origin(
        result["source_platform"],
        result["source_peer_id"],
        result["chat_id"],
        result["vk_id"],
        result["notify_text"],
    )


async def reject_expense_request(message: MessagesMessage, expense_id: int):
    result = await run_sync(reject_expense_result, expense_id)
    if not result["ok"]:
        await reply(message, result["error"], keyboard=admin_shortcuts_keyboard())
        return

    await reply(message, f'Расход #{expense_id} отклонен.', keyboard=admin_shortcuts_keyboard())
    await notify_expense_origin(
        result["source_platform"],
        result["source_peer_id"],
        result["chat_id"],
        result["vk_id"],
        result["notify_text"],
    )


async def show_expense_receipt_request(message: MessagesMessage, expense_id: int):
    expense = await run_sync(get_expense_by_id, expense_id)
    if not expense:
        await reply(message, 'Расход не найден.', keyboard=admin_shortcuts_keyboard())
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
        _receipt_source_file_id,
        receipt_source_url,
        *_rest,
    ) = expense

    text = (
        f'Чек по расходу #{expense_id}\n'
        f'Сотрудник: {full_name}\n'
        f'Дата: {expense_date}\n'
        f'Сумма: {amount}\n'
        f'Описание: {description}'
    )
    sent = False
    if receipt_path:
        sent = await send_vk_media(message.from_id, receipt_path, text)
    if not sent and receipt_source_url:
        try:
            restored_receipt = await save_remote_receipt(
                receipt_source_url,
                expense_id,
                "restored_vk",
                receipt_media_type or "document",
                receipt_original_name,
            )
            await run_sync(
                update_expense_receipt,
                expense_id,
                restored_receipt["path"],
                restored_receipt["original_name"],
                restored_receipt["media_type"],
                None,
                receipt_source_url,
            )
            sent = await send_vk_media(message.from_id, restored_receipt["path"], text)
        except Exception:
            sent = False
    if not any([receipt_path, receipt_source_url]):
        await reply(message, f'У расхода #{expense_id} чек не приложен.', keyboard=admin_shortcuts_keyboard())
        return
    if not sent:
        await reply(message, f'Не удалось отправить чек по расходу #{expense_id}.', keyboard=admin_shortcuts_keyboard())


async def merge_workers_request(message: MessagesMessage, target_worker_id: int, source_worker_id: int):
    await reply(
        message,
        (await run_sync(merge_workers_result, target_worker_id, source_worker_id)).replace('Удалённый', 'Удаленный'),
        keyboard=admin_shortcuts_keyboard(),
    )


async def worker_rates_request(message: MessagesMessage, worker_id: int):
    text = await run_sync(get_worker_rates_result, worker_id)
    if text is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, text, keyboard=admin_shortcuts_keyboard())


async def set_rate_request(message: MessagesMessage, worker_id: int, work_type: str, rate_value: float):
    text = await run_sync(set_worker_rate_result, worker_id, work_type, rate_value)
    if text is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, text, keyboard=admin_shortcuts_keyboard())
    schedule_sheet_sync(date.today().year, date.today().month)


async def add_bonus_request(message: MessagesMessage, worker_id: int, amount: float, description: str):
    result = await run_sync(add_bonus_result, worker_id, amount, description)
    if result is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])
    schedule_sheet_sync(result["sync_year"], result["sync_month"])


async def add_penalty_request(message: MessagesMessage, worker_id: int, amount: float, description: str):
    result = await run_sync(add_penalty_result, worker_id, amount, description)
    if result is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])
    schedule_sheet_sync(result["sync_year"], result["sync_month"])


async def bonuses_request(message: MessagesMessage, worker_id: int):
    text = await run_sync(get_worker_bonuses_result, worker_id)
    if text is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, text, keyboard=admin_shortcuts_keyboard())


async def penalties_request(message: MessagesMessage, worker_id: int):
    text = await run_sync(get_worker_penalties_result, worker_id)
    if text is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, text, keyboard=admin_shortcuts_keyboard())


async def set_admin_request(message: MessagesMessage, worker_id: int, is_admin_value: bool):
    result = await run_sync(set_admin_result, worker_id, is_admin_value)
    clear_active_workers_cache()
    if result and result.get("notify_vk_id"):
        clear_worker_vk_cache(result["notify_vk_id"])
    if result is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])


async def delete_worker_request(message: MessagesMessage, worker_id: int):
    result = await run_sync(delete_worker_result, worker_id)
    clear_active_workers_cache()
    if result and result.get("notify_vk_id"):
        clear_worker_vk_cache(result["notify_vk_id"])
    if result is None:
        await reply(message, 'Пользователь не найден.', keyboard=admin_shortcuts_keyboard())
        return
    await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
    await notify_worker(result["notify_chat_id"], result["notify_vk_id"], result["notify_text"])


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


def is_duplicate_vk_message(message: MessagesMessage):
    return mark_and_check_duplicate(vk_message_key(message))


def set_vk_bot(vk_bot_instance: Bot):
    global bot, _handlers_registered
    bot = vk_bot_instance
    if _handlers_registered:
        return
    _handlers_registered = True

    async def handle_start_message(message: MessagesMessage):
        existing_user = await run_sync(get_worker_by_vk_id_cached, message.from_id)
        if existing_user:
            await handle_existing_link(message)
            return

        set_user_state(message.from_id, "waiting_for_new_account_choice")
        await reply(
            message,
            'Привет!\n\nУ вас уже есть аккаунт в Telegram-боте?',
            keyboard=start_choice_keyboard(),
        )

    @bot.on.message(text="/start")
    async def start_handler(message: MessagesMessage):
        await handle_start_message(message)

    @bot.on.message(text="\u041d\u0430\u0447\u0430\u0442\u044c")
    async def start_button_handler(message: MessagesMessage):
        await handle_start_message(message)

    @bot.on.message(text="/help")
    async def help_handler(message: MessagesMessage):
        await reply(message, vk_help_text(is_admin_by_vk_id_cached(message.from_id)), keyboard=main_menu_keyboard_for_user(message.from_id))

    @bot.on.message(text="/cancel")
    async def cancel_handler(message: MessagesMessage):
        if is_duplicate_vk_message(message):
            return
        clear_user_state(message.from_id)
        await reply(message, 'Текущий ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))

    @bot.on.message(text="/my_id")
    async def my_id_handler(message: MessagesMessage):
        if is_duplicate_vk_message(message) and not get_user_meta(message.from_id, "skip_my_id_dedup", False):
            return
        user = await run_sync(get_worker_by_vk_id_cached, message.from_id)
        if not user:
            await reply(message, 'Вы еще не зарегистрированы в системе.\nИспользуйте /start для регистрации.', keyboard=start_choice_keyboard())
            return

        await reply(
            message,
            f'Ваш ID: {user[0]}\n\nИспользуйте этот номер, если захотите подключить другой мессенджер.',
            keyboard=back_to_menu_keyboard(message.from_id),
        )

    @bot.on.message(text="/add")
    async def add_handler(message: MessagesMessage):
        worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
        if error_text:
            await reply(message, error_text, keyboard=start_choice_keyboard())
            return

        await start_data_entry(message)

    @bot.on.message(text="/stats")
    async def stats_handler(message: MessagesMessage):
        worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
        if error_text:
            await reply(message, error_text, keyboard=start_choice_keyboard())
            return

        set_user_state(message.from_id, "choosing_stats_period", {"worker_id": worker_id})
        await reply(
            message,
            'Выберите период текущего месяца:',
            keyboard=stats_period_keyboard(),
        )

    @bot.on.message(text="/my_logs")
    async def my_logs_handler(message: MessagesMessage):
        worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
        if error_text:
            await reply(message, error_text, keyboard=start_choice_keyboard())
            return

        await reply(message, await run_sync(get_current_month_logs_text, worker_id), keyboard=back_to_menu_keyboard(message.from_id))

    @bot.on.message(text="/my_expenses")
    async def my_expenses_handler(message: MessagesMessage):
        worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
        if error_text:
            await reply(message, error_text, keyboard=start_choice_keyboard())
            return

        await reply(message, await run_sync(get_current_month_expenses_text, worker_id), keyboard=back_to_menu_keyboard(message.from_id))

    @bot.on.message(text="/pending_users")
    async def pending_users_handler(message: MessagesMessage):
        if not is_admin_by_vk_id(message.from_id):
            return
        await send_pending_users_overview(message)

    @bot.on.message(text="/users")
    async def users_handler(message: MessagesMessage):
        if not is_admin_by_vk_id(message.from_id):
            return
        await reply(message, format_active_workers(await run_sync(get_active_workers_cached)), keyboard=admin_shortcuts_keyboard())

    @bot.on.message(text="/pending_expenses")
    async def pending_expenses_handler(message: MessagesMessage):
        if not is_admin_by_vk_id(message.from_id):
            return
        await send_pending_expenses_overview(message)

    @bot.on.message()
    async def message_handler(message: MessagesMessage):
        if is_duplicate_vk_message(message):
            return
        text_raw = (message.text or "").strip()
        text = normalize_text(text_raw)
        user_state = get_user_state(message.from_id)

        if text == normalize_text("/retry_last"):
            pending = await run_sync(get_pending_inbound_message, "vk", str(message.from_id))
            if not pending or not pending.get("message_text"):
                await reply(message, "Не нашёл последнего сообщения, которое осталось без ответа.", keyboard=main_menu_keyboard_for_user(message.from_id))
                return
            if pending.get("state") == "waiting_expense_receipt" and pending.get("has_media"):
                await reply(
                    message,
                    "Последним сообщением был чек с вложением. Автоматически повторить его нельзя — пожалуйста, отправьте файл ещё раз.",
                    keyboard=expense_receipt_keyboard(),
                )
                return
            if pending.get("state"):
                set_user_state(message.from_id, pending["state"], pending.get("state_data") or {})
                user_state = get_user_state(message.from_id)
            text_raw = (pending.get("message_text") or "").strip()
            text = normalize_text(text_raw)
            message.text = text_raw
        else:
            await run_sync(
                save_conversation_inbound,
                "vk",
                str(message.from_id),
                text_raw,
                user_state["state"] if user_state else None,
                (user_state or {}).get("data") or {},
                has_media=bool(getattr(message, "attachments", None)),
            )
            await run_sync(
                save_pending_inbound_message,
                "vk",
                str(message.from_id),
                text_raw,
                user_state["state"] if user_state else None,
                (user_state or {}).get("data") or {},
                has_media=bool(getattr(message, "attachments", None)),
            )

        if text in {normalize_text("\u041d\u0430\u0447\u0430\u0442\u044c"), normalize_text("/start")}:
            await handle_start_message(message)
            return

        if text in {normalize_text('/past_requests'), normalize_text('Заявки за прошлый месяц')}:
            if not await run_sync(is_admin_by_vk_id, message.from_id):
                return
            requests = await run_sync(list_pending_requests)
            if not requests:
                await reply(message, 'Нет заявок за прошлый месяц, ожидающих решения.')
            for request in requests:
                await reply(message, format_saved_request(request), keyboard=vk_request_keyboard(request['id']))
            return

        if text.startswith(('одобрить заявку #', 'отклонить заявку #')):
            if not await run_sync(is_admin_by_vk_id, message.from_id):
                return
            try:
                request_id = int(text.rsplit('#', 1)[1])
                result_text = await process_request_decision(request_id, text.startswith('одобрить'), f'vk:{message.from_id}')
            except ValueError as exc:
                await reply(message, str(exc))
                return
            await reply(message, result_text)
            return

        if text in {normalize_text(BTN_ADD)}:
            await add_handler(message)
            return

        if text in {normalize_text(BTN_PAST_MONTH)}:
            await start_past_month_entry(message)
            return

        if text in {normalize_text(BTN_STATS)}:
            await stats_handler(message)
            return

        if text in {normalize_text(BTN_LOGS)}:
            await my_logs_handler(message)
            return

        if text in {normalize_text(BTN_EXPENSES)}:
            await my_expenses_handler(message)
            return

        if text in {normalize_text(BTN_MY_ID)}:
            set_user_meta(message.from_id, "skip_my_id_dedup", True)
            try:
                await my_id_handler(message)
            finally:
                clear_user_meta(message.from_id, "skip_my_id_dedup")
            return

        if text in {normalize_text(BTN_LINK_TELEGRAM)}:
            await start_telegram_link_flow(message)
            return

        if text in {normalize_text(BTN_HELP), normalize_text(BTN_ADMIN_HELP)}:
            await help_handler(message)
            return

        if text in {normalize_text(BTN_MENU)}:
            clear_user_state(message.from_id)
            clear_user_meta(message.from_id)
            existing_user = await run_sync(get_worker_by_vk_id_cached, message.from_id)
            full_name = existing_user[4] if existing_user else None
            await show_main_menu(message, full_name)
            return

        if text in {normalize_text(BTN_ADMIN_USERS)}:
            await pending_users_handler(message)
            return

        if text in {normalize_text(BTN_ADMIN_EXPENSES)}:
            await pending_expenses_handler(message)
            return

        if text in {normalize_text('Заявки ▶')}:
            page = int(get_user_meta(message.from_id, "pending_users_page", 0)) + 1
            set_user_meta(message.from_id, "pending_users_page", page)
            await send_pending_users_overview(message, page=page)
            return

        if text in {normalize_text('◀ Заявки')}:
            page = max(0, int(get_user_meta(message.from_id, "pending_users_page", 0)) - 1)
            set_user_meta(message.from_id, "pending_users_page", page)
            await send_pending_users_overview(message, page=page)
            return

        if text in {normalize_text('Расходы ▶')}:
            page = int(get_user_meta(message.from_id, "pending_expenses_page", 0)) + 1
            set_user_meta(message.from_id, "pending_expenses_page", page)
            await send_pending_expenses_overview(message, page=page)
            return

        if text in {normalize_text('◀ Расходы')}:
            page = max(0, int(get_user_meta(message.from_id, "pending_expenses_page", 0)) - 1)
            set_user_meta(message.from_id, "pending_expenses_page", page)
            await send_pending_expenses_overview(message, page=page)
            return

        if text in {normalize_text(BTN_ADMIN_WORKERS)}:
            await users_handler(message)
            return

        if text in {normalize_text(BTN_ADMIN_SHOW_PROJECTS)}:
            if not is_admin_by_vk_id(message.from_id):
                return
            await reply(message, await run_sync(get_active_projects_text_result), keyboard=admin_shortcuts_keyboard())
            return

        if text in {normalize_text("/sync_projects"), normalize_text("Обновить проекты Yougile")}:
            if not await run_sync(is_admin_by_vk_id, message.from_id):
                return
            try:
                result = await run_sync(sync_projects)
            except ValueError as exc:
                await reply(message, str(exc))
                return
            except Exception:
                logger.exception("Yougile sync requested by VK administrator failed")
                await reply(message, "Не удалось обновить проекты Yougile. Локальный справочник сохранён.")
                return
            clear_active_projects_cache()
            await reply(message, f"Проекты Yougile обновлены: {result['synced']}.", keyboard=admin_shortcuts_keyboard())
            return

        if text in {normalize_text(BTN_ADMIN_ADD_PROJECT)}:
            if not is_admin_by_vk_id(message.from_id):
                return
            set_user_state(message.from_id, "admin_waiting_project_id", {})
            await reply(message, "Введите ID проекта цифрами без пробелов или отправьте auto.", keyboard=input_step_keyboard())
            return

        if text in {normalize_text(BTN_ADMIN_DELETE_PROJECT)}:
            if not is_admin_by_vk_id(message.from_id):
                return
            project_rows = await run_sync(get_active_projects_cached)
            if not project_rows:
                await reply(message, 'Сейчас нет активных проектов для удаления.', keyboard=admin_shortcuts_keyboard())
                return
            set_user_state(message.from_id, "admin_waiting_project_delete", {})
            set_user_meta(message.from_id, "project_picker_page", 0)
            await reply(message, 'Выберите проект для удаления.', keyboard=project_picker_keyboard(project_rows, page=0, mode="delete"))
            return

        if text in {normalize_text(BTN_WORKERS_NEXT), normalize_text(BTN_WORKERS_PREV)}:
            user_state = get_user_state(message.from_id)
            if not user_state or user_state["state"] not in {"admin_waiting_worker_id", "admin_waiting_second_worker_id"}:
                return
            page = int(get_user_meta(message.from_id, "admin_worker_picker_page", 0))
            page = page + 1 if text == normalize_text(BTN_WORKERS_NEXT) else max(0, page - 1)
            set_user_meta(message.from_id, "admin_worker_picker_page", page)
            worker_rows = await run_sync(get_active_workers_cached)
            if user_state["state"] == "admin_waiting_worker_id":
                prompt = get_admin_worker_prompt(user_state["data"].get("admin_action"))
            else:
                prompt = 'Выберите запись-дубль, которую нужно влить в основную.'
            await reply(message, prompt, keyboard=admin_worker_picker_keyboard_paged(worker_rows, page=page))
            return

        if text.startswith(normalize_text('Одобрить заявку ')):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Не удалось распознать ID заявки.', keyboard=admin_shortcuts_keyboard())
                return
            await approve_user_request(message, worker_id)
            page = int(get_user_meta(message.from_id, "pending_users_page", 0))
            await send_pending_users_overview(message, page=page)
            return

        if text.startswith(normalize_text('Отклонить заявку ')):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Не удалось распознать ID заявки.', keyboard=admin_shortcuts_keyboard())
                return
            await reject_user_request(message, worker_id)
            page = int(get_user_meta(message.from_id, "pending_users_page", 0))
            await send_pending_users_overview(message, page=page)
            return

        if text.startswith(normalize_text('Одобрить расход ')):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Не удалось распознать ID расхода.', keyboard=admin_shortcuts_keyboard())
                return
            await approve_expense_request(message, expense_id)
            page = int(get_user_meta(message.from_id, "pending_expenses_page", 0))
            await send_pending_expenses_overview(message, page=page)
            return

        if text.startswith(normalize_text('Показать чек ')):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Не удалось распознать ID расхода.', keyboard=admin_shortcuts_keyboard())
                return
            await show_expense_receipt_request(message, expense_id)
            return

        if text.startswith(normalize_text('Отклонить расход ')):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.rsplit(" ", 1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Не удалось распознать ID расхода.', keyboard=admin_shortcuts_keyboard())
                return
            await reject_expense_request(message, expense_id)
            page = int(get_user_meta(message.from_id, "pending_expenses_page", 0))
            await send_pending_expenses_overview(message, page=page)
            return

        admin_worker_actions = {
            normalize_text(BTN_ADMIN_SHOW_RATES): "show_rates",
            normalize_text(BTN_ADMIN_SET_RATE): "set_rate",
            normalize_text(BTN_ADMIN_ADD_BONUS): "add_bonus",
            normalize_text(BTN_ADMIN_ADD_PENALTY): "add_penalty",
            normalize_text(BTN_ADMIN_SHOW_BONUSES): "show_bonuses",
            normalize_text(BTN_ADMIN_SHOW_PENALTIES): "show_penalties",
            normalize_text(BTN_ADMIN_MERGE): "merge_workers",
            normalize_text(BTN_ADMIN_MAKE_ADMIN): "make_admin",
            normalize_text(BTN_ADMIN_REMOVE_ADMIN): "remove_admin",
            normalize_text(BTN_ADMIN_DELETE_WORKER): "delete_worker",
        }
        if text in admin_worker_actions:
            if not is_admin_by_vk_id(message.from_id):
                return
            await start_vk_admin_worker_picker(message, admin_worker_actions[text])
            return

        if text in {normalize_text(BTN_ADMIN_CLOSE_MONTH)}:
            if not is_admin_by_vk_id(message.from_id):
                return
            set_user_state(message.from_id, "admin_waiting_month")
            await reply(message, 'Выберите месяц для закрытия или введите его вручную.', keyboard=admin_month_keyboard())
            return

        if text.startswith("/approve_user "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /approve_user ID', keyboard=admin_shortcuts_keyboard())
                return
            await approve_user_request(message, worker_id)
            return

        if text.startswith("/reject_user "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /reject_user ID', keyboard=admin_shortcuts_keyboard())
                return
            await reject_user_request(message, worker_id)
            return

        if text.startswith("/approve_expense "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /approve_expense ID', keyboard=admin_shortcuts_keyboard())
                return
            await approve_expense_request(message, expense_id)
            return

        if text.startswith("/reject_expense "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /reject_expense ID', keyboard=admin_shortcuts_keyboard())
                return
            await reject_expense_request(message, expense_id)
            return

        if text.startswith("/show_expense_receipt "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                expense_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /show_expense_receipt ID', keyboard=admin_shortcuts_keyboard())
                return
            await show_expense_receipt_request(message, expense_id)
            return

        if text.startswith("/merge_workers "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                target_worker_id, source_worker_id = parse_two_ids_argument(text_raw)
            except ValueError:
                await reply(message, 'Формат команды: /merge_workers KEEP_ID DUPLICATE_ID', keyboard=admin_shortcuts_keyboard())
                return
            try:
                await merge_workers_request(message, target_worker_id, source_worker_id)
            except ValueError as exc:
                await reply(message, str(exc), keyboard=admin_shortcuts_keyboard())
            return

        if text.startswith("/rates "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /rates WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await worker_rates_request(message, worker_id)
            return

        if text.startswith("/set_rate "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id, work_type, rate_value = parse_rate_argument(text_raw)
            except ValueError:
                await reply(message, 'Формат команды: /set_rate WORKER_ID shift|install RATE', keyboard=admin_shortcuts_keyboard())
                return
            await set_rate_request(message, worker_id, work_type, rate_value)
            return

        if text.startswith("/add_bonus "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id, amount, description = parse_adjustment_argument(text_raw)
            except ValueError:
                await reply(message, 'Формат команды: /add_bonus WORKER_ID AMOUNT DESCRIPTION', keyboard=admin_shortcuts_keyboard())
                return
            await add_bonus_request(message, worker_id, amount, description)
            return

        if text.startswith("/add_penalty "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id, amount, description = parse_adjustment_argument(text_raw)
            except ValueError:
                await reply(message, 'Формат команды: /add_penalty WORKER_ID AMOUNT DESCRIPTION', keyboard=admin_shortcuts_keyboard())
                return
            await add_penalty_request(message, worker_id, amount, description)
            return

        if text.startswith("/bonuses "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /bonuses WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await bonuses_request(message, worker_id)
            return

        if text.startswith("/penalties "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /penalties WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await penalties_request(message, worker_id)
            return

        if text.startswith("/make_admin "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /make_admin WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await set_admin_request(message, worker_id, True)
            return

        if text.startswith("/remove_admin "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /remove_admin WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await set_admin_request(message, worker_id, False)
            return

        if text.startswith("/delete_worker "):
            if not is_admin_by_vk_id(message.from_id):
                return
            try:
                worker_id = int(text_raw.split(maxsplit=1)[1])
            except (IndexError, ValueError):
                await reply(message, 'Формат команды: /delete_worker WORKER_ID', keyboard=admin_shortcuts_keyboard())
                return
            await delete_worker_request(message, worker_id)
            return

        if text.startswith("/close_month"):
            if not is_admin_by_vk_id(message.from_id):
                return

            try:
                year, month = parse_month_argument(text_raw)
            except ValueError:
                await reply(message, 'Формат команды: /close_month MM.YYYY или /close_month YYYY-MM', keyboard=admin_shortcuts_keyboard())
                return

            await reply(
                message,
                f'Закрываю месяц {month:02d}.{year} и обновляю рабочий лист. Это может занять несколько секунд.',
                keyboard=admin_shortcuts_keyboard(),
            )
            try:
                result = await asyncio.to_thread(close_month_tracked, year, month, "vk_manual")
            except ValueError as exc:
                await reply(message, str(exc), keyboard=admin_shortcuts_keyboard())
                return
            except Exception as exc:
                await reply(message, google_error_message(exc), keyboard=admin_shortcuts_keyboard())
                return

            if result["status"] == "busy":
                await reply(message, 'Этот месяц уже закрывается в другом процессе. Попробуйте ещё раз через минуту.', keyboard=admin_shortcuts_keyboard())
                return
            if result["status"] == "already_closed":
                archived = result["archived_month"]
                current = result["current_month"]
                await reply(
                    message,
                    'Этот месяц уже был закрыт ранее.\n'
                    f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
                    f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
                    keyboard=admin_shortcuts_keyboard(),
                )
                return

            archived = result["archived_month"]
            current = result["current_month"]
            await reply(
                message,
                'Месяц успешно закрыт.\n'
                f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
                f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
                keyboard=admin_shortcuts_keyboard(),
            )
            return

        if not user_state:
            await reply(message, "Сообщение не распознано. Используйте кнопки меню или команду /start.", keyboard=main_menu_keyboard_for_user(message.from_id))
            return

        state = user_state["state"]
        state_data = user_state["data"]

        if state == "waiting_for_new_account_choice":
            if text in {normalize_text(BTN_HAVE_ACCOUNT), 'да'}:
                set_user_state(message.from_id, "waiting_for_link_code")
                await reply(
                    message,
                    'Введите код привязки из Telegram. Его можно получить командой /link_vk в Telegram-боте.',
                    keyboard=build_keyboard([[BTN_CANCEL]]),
                )
                return
            if text in {normalize_text(BTN_NEW_ACCOUNT), 'нет'}:
                set_user_state(message.from_id, "waiting_for_full_name")
                await reply(
                    message,
                    'Введите ваше полное имя для регистрации.',
                    keyboard=build_keyboard([[BTN_CANCEL]]),
                )
                return

            await reply(message, 'Выберите один из вариантов кнопками.', keyboard=start_choice_keyboard())
            return

        if state == "waiting_for_link_code":
            await handle_link_code(message, text_raw)
            return

        if state == "waiting_for_full_name":
            full_name = text_raw
            if not full_name:
                await reply(message, 'Введите, пожалуйста, полное имя.', keyboard=registration_confirm_keyboard())
                return

            set_user_state(message.from_id, "confirming_name", {"full_name": full_name})
            await reply(
                message,
                f'Вы ввели: {full_name}\n\nПодтвердить?',
                keyboard=registration_confirm_keyboard(),
            )
            return

        if state == "confirming_name":
            if text in {normalize_text(BTN_CONFIRM), 'да'}:
                full_name = state_data["full_name"]
                worker_id = create_worker(full_name, None, message.from_id)
                clear_worker_vk_cache(message.from_id)
                clear_active_workers_cache()
                clear_user_state(message.from_id)
                await reply(
                    message,
                    f'Регистрация завершена.\n\nВаш ID: {worker_id}\n\nЖдите подтверждения от администратора.',
                    keyboard=build_keyboard([[BTN_MENU]]),
                )
                await notify_admins(
                    'Новая заявка на регистрацию из VK:\n'
                    f"{full_name} (ID: {worker_id})\n\n"
                    f'Команды: /approve_user {worker_id} или /reject_user {worker_id}',
                    telegram_reply_markup=telegram_register_keyboard(worker_id),
                )
                return

            if text in {normalize_text(BTN_CANCEL), 'нет'}:
                set_user_state(message.from_id, "waiting_for_full_name")
                await reply(message, 'Введите ваше полное имя еще раз.', keyboard=build_keyboard([[BTN_CANCEL]]))
                return

            await reply(message, 'Используйте кнопки Подтвердить или Отменить.', keyboard=registration_confirm_keyboard())
            return

        if state == "admin_waiting_worker_id":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "admin_worker_picker_page")
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "admin_worker_picker_page")
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                worker_id = extract_worker_id_from_label(text_raw)
            except ValueError:
                page = int(get_user_meta(message.from_id, "admin_worker_picker_page", 0))
                await reply(message, 'Выберите сотрудника кнопкой или введите его числовой ID.', keyboard=admin_worker_picker_keyboard_paged(page=page))
                return
            action = state_data.get("admin_action")

            await handle_vk_admin_worker_pick_action(message, action, worker_id)
            return

        if state == "admin_waiting_work_type":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            normalized_type = normalize_text(text_raw)
            mapping = {
                'смена': "shift",
                'монтаж': "install",
                "shift": "shift",
                "install": "install",
            }
            work_type = mapping.get(normalized_type)
            if not work_type:
                await reply(message, 'Выберите тип работ кнопкой или введите: shift / install.', keyboard=admin_work_type_keyboard())
                return

            next_data = dict(state_data)
            next_data["work_type"] = work_type
            set_user_state(message.from_id, "admin_waiting_rate", next_data)
            await reply(message, 'Введите ставку в час.', keyboard=input_step_keyboard())
            return

        if state == "admin_waiting_rate":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                rate_value = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите ставку числом. Например: 500 или 650.50', keyboard=input_step_keyboard())
                return

            if rate_value <= 0:
                await reply(message, 'Ставка должна быть больше нуля.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["rate_value"] = rate_value
            set_user_state(message.from_id, "admin_confirm", next_data)
            work_type_label = 'Смена' if state_data["work_type"] == "shift" else 'Монтаж'
            worker_ref = state_data.get("worker_ref") or f"ID: {state_data['worker_id']}"
            await reply(
                message,
                f'Проверьте данные:\nСотрудник: {worker_ref}\nТип работ: {work_type_label}\nСтавка: {rate_value}',
                keyboard=registration_confirm_keyboard(),
            )
            return

        if state == "admin_waiting_amount":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                amount = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите сумму числом. Например: 1500 или 2500.50', keyboard=input_step_keyboard())
                return

            if amount <= 0:
                await reply(message, 'Сумма должна быть больше нуля.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["amount"] = amount
            set_user_state(message.from_id, "admin_waiting_description", next_data)
            await reply(message, 'Введите описание.', keyboard=input_step_keyboard())
            return

        if state == "admin_waiting_description":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            description = text_raw.strip()
            if not description:
                await reply(message, 'Описание не должно быть пустым.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["description"] = description
            set_user_state(message.from_id, "admin_confirm", next_data)
            action_label = 'премию' if state_data.get("admin_action") == "add_bonus" else 'штраф'
            worker_ref = state_data.get("worker_ref") or f"ID: {state_data['worker_id']}"
            await reply(
                message,
                f"Проверьте данные:\nСотрудник: {worker_ref}\nСумма: {state_data['amount']}\nОписание: {description}\n\nПодтвердить {action_label}?",
                keyboard=registration_confirm_keyboard(),
            )
            return

        if state == "admin_waiting_second_worker_id":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "admin_worker_picker_page")
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "admin_worker_picker_page")
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                source_worker_id = extract_worker_id_from_label(text_raw)
            except ValueError:
                page = int(get_user_meta(message.from_id, "admin_worker_picker_page", 0))
                await reply(message, 'Выберите запись-дубль кнопкой или введите её числовой ID.', keyboard=admin_worker_picker_keyboard_paged(page=page))
                return

            clear_user_state(message.from_id)
            try:
                await merge_workers_request(message, state_data["worker_id"], source_worker_id)
            except ValueError as exc:
                await reply(message, str(exc), keyboard=admin_shortcuts_keyboard())
            return

        if state == "admin_confirm":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL), 'нет'}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text not in {normalize_text(BTN_CONFIRM), 'да'}:
                await reply(message, 'Используйте кнопки Подтвердить или Отменить.', keyboard=registration_confirm_keyboard())
                return

            clear_user_state(message.from_id)
            if state_data.get("admin_action") == "set_rate":
                await set_rate_request(message, state_data["worker_id"], state_data["work_type"], state_data["rate_value"])
            elif state_data.get("admin_action") == "add_bonus":
                await add_bonus_request(message, state_data["worker_id"], state_data["amount"], state_data["description"])
            elif state_data.get("admin_action") == "make_admin":
                await set_admin_request(message, state_data["worker_id"], True)
            elif state_data.get("admin_action") == "remove_admin":
                await set_admin_request(message, state_data["worker_id"], False)
            elif state_data.get("admin_action") == "delete_worker":
                await delete_worker_request(message, state_data["worker_id"])
            else:
                await add_penalty_request(message, state_data["worker_id"], state_data["amount"], state_data["description"])
            return

        if state == "admin_waiting_project_id":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            if text in {"auto", "авто", "-"}:
                next_data = dict(state_data)
                next_data["project_id"] = None
                set_user_state(message.from_id, "admin_waiting_project_name", next_data)
                await reply(message, "Введите название проекта.", keyboard=input_step_keyboard())
                return

            if not text_raw.isdigit():
                await reply(message, "Введите ID проекта цифрами без пробелов или отправьте auto.", keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["project_id"] = int(text_raw)
            set_user_state(message.from_id, "admin_waiting_project_name", next_data)
            await reply(message, "Введите название проекта.", keyboard=input_step_keyboard())
            return

        if state == "admin_waiting_project_name":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            project_name = text_raw
            if not project_name:
                await reply(message, 'Введите название проекта.', keyboard=input_step_keyboard())
                return

            try:
                result = await run_sync(add_project_result, project_name, state_data.get("project_id"))
            except ValueError as exc:
                await reply(message, str(exc), keyboard=input_step_keyboard())
                return

            clear_user_state(message.from_id)
            clear_active_projects_cache()
            try:
                await run_sync(sync_projects_reference_sheet)
            except Exception:
                logger.exception("Failed to sync projects reference sheet after adding a project.")
            await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
            return

        if state == "admin_waiting_project_delete":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            project_rows = await run_sync(get_active_projects_cached)
            current_page = int(get_user_meta(message.from_id, "project_picker_page", 0))
            if text == normalize_text(BTN_PROJECTS_PREV):
                current_page = max(0, current_page - 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект для удаления.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="delete"))
                return

            if text == normalize_text(BTN_PROJECTS_NEXT):
                page_size = 6
                max_page = max(0, (len(project_rows) - 1) // page_size)
                current_page = min(max_page, current_page + 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект для удаления.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="delete"))
                return

            try:
                project_id = extract_project_id_from_label(text_raw)
            except ValueError:
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="delete"))
                return

            result = await run_sync(delete_project_result, project_id)
            if result is None:
                await reply(message, 'Проект не найден.', keyboard=admin_shortcuts_keyboard())
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                return
            if result.get("already_inactive"):
                await reply(message, 'Проект уже не активен.', keyboard=admin_shortcuts_keyboard())
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                return

            clear_user_state(message.from_id)
            clear_user_meta(message.from_id, "project_picker_page")
            clear_active_projects_cache()
            try:
                await run_sync(sync_projects_reference_sheet)
            except Exception:
                logger.exception("Failed to sync projects reference sheet after deleting a project.")
            await reply(message, result["message_text"], keyboard=admin_shortcuts_keyboard())
            return

        if state == "admin_waiting_month":
            if not is_admin_by_vk_id(message.from_id):
                clear_user_state(message.from_id)
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Действие отменено.', keyboard=admin_shortcuts_keyboard())
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            if text_raw in {'Ввести другой месяц', 'ввести другой месяц'}:
                await reply(message, 'Введите месяц в формате MM.YYYY или YYYY-MM.', keyboard=input_step_keyboard())
                return

            if text_raw.startswith('Текущий: '):
                text_raw = text_raw.replace('Текущий: ', "", 1)
            elif text_raw.startswith('Предыдущий: '):
                text_raw = text_raw.replace('Предыдущий: ', "", 1)

            try:
                year, month = parse_month_argument(f"/close_month {text_raw}")
            except ValueError:
                await reply(message, 'Введите месяц в формате MM.YYYY или YYYY-MM.', keyboard=input_step_keyboard())
                return

            clear_user_state(message.from_id)
            await reply(message, f'Закрываю месяц {month:02d}.{year} и обновляю рабочий лист. Это может занять несколько секунд.', keyboard=admin_shortcuts_keyboard())
            try:
                result = await asyncio.to_thread(close_month_tracked, year, month, "vk_manual")
            except ValueError as exc:
                await reply(message, str(exc), keyboard=admin_shortcuts_keyboard())
                return
            except Exception as exc:
                await reply(message, google_error_message(exc), keyboard=admin_shortcuts_keyboard())
                return

            if result["status"] == "busy":
                await reply(message, 'Этот месяц уже закрывается в другом процессе. Попробуйте ещё раз через минуту.', keyboard=admin_shortcuts_keyboard())
                return
            if result["status"] == "already_closed":
                archived = result["archived_month"]
                current = result["current_month"]
                await reply(
                    message,
                    'Этот месяц уже был закрыт ранее.\n'
                    f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
                    f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
                    keyboard=admin_shortcuts_keyboard(),
                )
                return

            archived = result["archived_month"]
            current = result["current_month"]
            await reply(
                message,
                'Месяц успешно закрыт.\n'
                f"Архивный лист: {archived['sheet_title']} ({archived['month']:02d}.{archived['year']})\n"
                f"Текущий рабочий лист: {current['sheet_title']} ({current['month']:02d}.{current['year']})",
                keyboard=admin_shortcuts_keyboard(),
            )
            return

        if state == "waiting_past_month_date":
            worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
            if error_text:
                clear_user_state(message.from_id)
                await reply(message, error_text, keyboard=start_choice_keyboard())
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                request_date = parse_previous_month_date(text_raw)
            except ValueError as exc:
                await reply(message, str(exc), keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["worker_id"] = worker_id
            next_data["request_date"] = request_date.isoformat()
            set_user_state(message.from_id, "waiting_past_month_type", next_data)
            await reply(message, 'Теперь выберите, что нужно передать администратору за эту дату.', keyboard=past_month_type_keyboard())
            return

        if state == "waiting_past_month_type":
            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            entry_type = parse_past_entry_type(text_raw)
            if not entry_type:
                await reply(message, 'Выберите тип записи кнопками ниже.', keyboard=past_month_type_keyboard())
                return

            next_data = dict(state_data)
            next_data["entry_type"] = entry_type
            next_data["manual_project_entry"] = False

            if entry_type == "install":
                project_rows = await run_sync(get_active_projects_cached)
                set_user_meta(message.from_id, "project_picker_page", 0)
                if not project_rows:
                    next_data["manual_project_entry"] = False
                    set_user_state(message.from_id, "waiting_past_month_project", next_data)
                    await reply(message, 'Нет активных проектов. Обратитесь к администратору.', keyboard=input_step_keyboard())
                    return
                set_user_state(message.from_id, "waiting_past_month_project", next_data)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=0, mode="pick"))
                return

            if entry_type == "shift":
                set_user_state(message.from_id, "waiting_past_month_hours", next_data)
                await reply(message, 'Укажите количество часов по смене.', keyboard=input_step_keyboard())
                return

            project_rows = await run_sync(get_active_projects_cached)
            if not project_rows:
                await reply(message, 'Нет активных проектов. Обратитесь к администратору.', keyboard=past_month_type_keyboard())
                return
            next_data["project_id"] = None
            set_user_meta(message.from_id, "project_picker_page", 0)
            set_user_state(message.from_id, "waiting_past_month_project", next_data)
            await reply(message, 'Выберите проект для расхода.', keyboard=project_picker_keyboard(project_rows))
            return

        if state == "waiting_past_month_project":
            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            next_data = dict(state_data)
            current_page = int(get_user_meta(message.from_id, "project_picker_page", 0))
            project_rows = await run_sync(get_active_projects_cached)

            if text == normalize_text(BTN_PROJECTS_PREV):
                current_page = max(0, current_page - 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="pick"))
                return

            if text == normalize_text(BTN_PROJECTS_NEXT):
                page_size = 6
                max_page = max(0, (len(project_rows) - 1) // page_size) if project_rows else 0
                current_page = min(max_page, current_page + 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="pick"))
                return

            if text == normalize_text(BTN_PROJECT_OTHER):
                await reply(message, "Выберите проект из справочника. Новый проект добавляет администратор.", keyboard=project_picker_keyboard(project_rows, page=current_page))
                return

            selected = None
            for selected_id, project_name in project_rows:
                if normalize_text(project_name) == text:
                    next_data["project_id"] = selected_id
                    selected = project_name
                    break
            if not selected:
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page))
                return
            next_data["project"] = selected

            next_data["manual_project_entry"] = False
            clear_user_meta(message.from_id, "project_picker_page")
            if next_data.get("entry_type") == "expense":
                set_user_state(message.from_id, "waiting_past_month_expense_description", next_data)
                await reply(message, "Напишите краткое описание расхода.", keyboard=input_step_keyboard())
                return
            set_user_state(message.from_id, "waiting_past_month_hours", next_data)
            await reply(message, 'Укажите количество часов по этому монтажу.', keyboard=input_step_keyboard())
            return

        if state == "waiting_past_month_hours":
            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                hours = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите количество часов числом. Например: 8 или 7.5', keyboard=input_step_keyboard())
                return

            if hours <= 0:
                await reply(message, 'Количество часов должно быть больше нуля.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["hours"] = hours
            set_user_state(message.from_id, "confirm_past_month", next_data)
            type_label = 'Монтаж' if state_data.get("entry_type") == "install" else 'Смена'
            request_date = date.fromisoformat(state_data["request_date"])
            lines = [
                'Проверьте запрос перед отправкой администратору:',
                f"Дата: {request_date:%d.%m.%Y}",
                f"Тип записи: {type_label}",
            ]
            if state_data.get("project"):
                lines.append(f"Проект: {state_data['project']}")
            lines.append(f"Часы: {hours}")
            await reply(message, "\n".join(lines), keyboard=confirm_keyboard())
            return

        if state == "waiting_past_month_expense_description":
            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            expense_type = text_raw.strip()
            if not expense_type:
                await reply(message, 'Напишите, пожалуйста, краткое описание расхода.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["expense_type"] = expense_type
            set_user_state(message.from_id, "waiting_past_month_expense_amount", next_data)
            await reply(message, 'Теперь укажите сумму расхода.', keyboard=input_step_keyboard())
            return

        if state == "waiting_past_month_expense_amount":
            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            try:
                amount = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите сумму числом. Например: 1500 или 1500.50', keyboard=input_step_keyboard())
                return

            if amount <= 0:
                await reply(message, 'Сумма должна быть больше нуля.', keyboard=input_step_keyboard())
                return

            next_data = dict(state_data)
            next_data["amount"] = amount
            set_user_state(message.from_id, "confirm_past_month", next_data)
            request_date = date.fromisoformat(state_data["request_date"])
            lines = [
                'Проверьте запрос перед отправкой администратору:',
                f"Дата: {request_date:%d.%m.%Y}",
                'Тип записи: Расход',
                f"Проект: {state_data.get('project')}",
                f"Описание расхода: {state_data['expense_type']}",
                f"Сумма: {amount}",
            ]
            await reply(message, "\n".join(lines), keyboard=confirm_keyboard())
            return

        if state == "confirm_past_month":
            if text in {normalize_text(BTN_CANCEL), 'нет'}:
                clear_user_state(message.from_id)
                await reply(message, 'Запрос отменён.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            if text not in {normalize_text(BTN_CONFIRM), 'да'}:
                await reply(message, 'Используйте кнопки Подтвердить или Отменить.', keyboard=confirm_keyboard())
                return

            try:
                await send_past_month_request_to_admins(message, state_data)
            except ValueError as exc:
                await reply(message, str(exc), keyboard=input_step_keyboard())
                return
            clear_user_state(message.from_id)
            clear_user_meta(message.from_id, "project_picker_page")
            await reply(
                message,
                'Заявка отправлена администраторам. После одобрения данные появятся в базе и архивном отчёте.',
                keyboard=main_menu_keyboard_for_user(message.from_id),
            )
            return

        if state == "waiting_date":
            worker_id, _, error_text = await run_sync(get_vk_access, message.from_id)
            if error_text:
                clear_user_state(message.from_id)
                await reply(message, error_text, keyboard=start_choice_keyboard())
                return

            if text in {normalize_text(BTN_CANCEL)}:
                clear_user_state(message.from_id)
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_PAST_MONTH):
                set_user_state(message.from_id, "waiting_past_month_date", {"worker_id": worker_id, "past_request_key": str(uuid.uuid4())})
                await reply(
                    message,
                    'Введите полную дату из прошлого месяца в формате ДД.ММ.ГГГГ.\nНапример: 28.06.2026',
                    keyboard=input_step_keyboard(),
                )
                return

            if not text_raw.isdigit():
                await reply(message, 'Введите число месяца от 1 до 31. Например: 14', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            work_day = int(text_raw)
            is_valid, error_text = validate_current_month_day(work_day)
            if not is_valid:
                keyboard = future_date_keyboard() if work_day > date.today().day else build_keyboard([[BTN_CANCEL], [BTN_MENU]])
                await reply(message, error_text, keyboard=keyboard)
                return

            set_user_state(message.from_id, "waiting_place", {"worker_id": worker_id, "date": work_day})
            await reply(message, 'Дата записана. Теперь выберите тип записи:', keyboard=work_type_keyboard())
            return

        if state == "waiting_place":
            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            next_data = dict(state_data)
            next_data["project_id"] = None
            if text == normalize_text(BTN_INSTALL):
                next_data["place"] = 'Монтаж'
                next_data["manual_project_entry"] = False
                project_rows = await run_sync(get_active_projects_cached)
                if not project_rows:
                    next_data["manual_project_entry"] = False
                    set_user_state(message.from_id, "waiting_project", next_data)
                    await reply(message, 'Нет активных проектов. Обратитесь к администратору.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                    return
                set_user_state(message.from_id, "waiting_project", next_data)
                set_user_meta(message.from_id, "project_picker_page", 0)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=0, mode="pick"))
                return

            if text == normalize_text(BTN_SHIFT):
                next_data["place"] = 'Смена'
                next_data["project"] = None
                set_user_state(message.from_id, "waiting_hours", next_data)
                await reply(message, 'Укажите количество часов по смене.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            if text == normalize_text(BTN_EXPENSE):
                next_data["place"] = 'Расходы'
                next_data["project"] = None
                next_data["hours"] = None
                project_rows = await run_sync(get_active_projects_cached)
                if not project_rows:
                    await reply(message, 'Нет активных проектов. Обратитесь к администратору.', keyboard=work_type_keyboard())
                    return
                next_data["manual_project_entry"] = False
                set_user_meta(message.from_id, "project_picker_page", 0)
                set_user_state(message.from_id, "waiting_project", next_data)
                await reply(message, 'Выберите проект для расхода.', keyboard=project_picker_keyboard(project_rows))
                return

            await reply(message, 'Выберите тип записи кнопками ниже.', keyboard=work_type_keyboard())
            return

        if state == "waiting_project":
            if text in {normalize_text(BTN_CANCEL), normalize_text(BTN_MENU)}:
                clear_user_state(message.from_id)
                clear_user_meta(message.from_id, "project_picker_page")
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            next_data = dict(state_data)
            current_page = int(get_user_meta(message.from_id, "project_picker_page", 0))
            project_rows = await run_sync(get_active_projects_cached)

            if text == normalize_text(BTN_PROJECTS_PREV):
                current_page = max(0, current_page - 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="pick"))
                return

            if text == normalize_text(BTN_PROJECTS_NEXT):
                page_size = 6
                max_page = max(0, (len(project_rows) - 1) // page_size) if project_rows else 0
                current_page = min(max_page, current_page + 1)
                set_user_meta(message.from_id, "project_picker_page", current_page)
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page, mode="pick"))
                return

            if text == normalize_text(BTN_PROJECT_OTHER):
                await reply(message, "Выберите проект из справочника. Новый проект добавляет администратор.", keyboard=project_picker_keyboard(project_rows, page=current_page))
                return

            selected = None
            for selected_id, project_name in project_rows:
                if normalize_text(project_name) == text:
                    next_data["project_id"] = selected_id
                    selected = project_name
                    break
            if not selected:
                await reply(message, 'Выберите проект кнопкой ниже.', keyboard=project_picker_keyboard(project_rows, page=current_page))
                return
            next_data["project"] = selected

            next_data["manual_project_entry"] = False
            clear_user_meta(message.from_id, "project_picker_page")
            if next_data.get("place") == "Расходы":
                set_user_state(message.from_id, "waiting_expense_type", next_data)
                await reply(message, "Напишите краткое описание расхода.", keyboard=input_step_keyboard())
                return
            set_user_state(message.from_id, "waiting_hours", next_data)
            await reply(message, 'Укажите количество часов по этому монтажу.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
            return

        if state == "waiting_hours":
            if text in {normalize_text(BTN_CANCEL), normalize_text(BTN_MENU)}:
                clear_user_state(message.from_id)
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            try:
                hours = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите количество часов числом. Например: 8 или 7.5', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            if hours <= 0:
                await reply(message, 'Количество часов должно быть больше нуля.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            next_data = dict(state_data)
            next_data["hours"] = hours
            set_user_state(message.from_id, "confirm", next_data)

            summary_lines = [
                'Проверьте данные перед сохранением:',
                f"Дата: {next_data.get('date')}",
                f"Тип записи: {next_data.get('place')}",
            ]
            if next_data.get("project"):
                summary_lines.append(f"Проект: {next_data.get('project')}")
            summary_lines.append(f"Часы: {next_data.get('hours')}")
            await reply(message, "\n".join(summary_lines), keyboard=confirm_keyboard())
            return

        if state == "waiting_expense_type":
            if text in {normalize_text(BTN_CANCEL), normalize_text(BTN_MENU)}:
                clear_user_state(message.from_id)
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if not text_raw:
                await reply(message, 'Напишите, пожалуйста, краткое описание расхода.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            next_data = dict(state_data)
            next_data["expense_type"] = text_raw
            next_data["source_platform"] = "vk"
            next_data["source_peer_id"] = message.peer_id
            set_user_state(message.from_id, "waiting_expense_amount", next_data)
            await reply(message, 'Теперь укажите сумму расхода.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
            return

        if state == "waiting_expense_amount":
            if text in {normalize_text(BTN_CANCEL), normalize_text(BTN_MENU)}:
                clear_user_state(message.from_id)
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            try:
                amount = float(text_raw.replace(",", "."))
            except ValueError:
                await reply(message, 'Введите сумму числом. Например: 1500 или 1500.50', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            if amount <= 0:
                await reply(message, 'Сумма должна быть больше нуля.', keyboard=build_keyboard([[BTN_CANCEL], [BTN_MENU]]))
                return

            next_data = dict(state_data)
            next_data["amount"] = amount
            set_user_state(message.from_id, "waiting_expense_receipt", next_data)

            await reply(
                message,
                'Теперь приложите чек фотографией или файлом.\nЕсли чека нет, нажмите кнопку «Продолжить без чека».',
                keyboard=expense_receipt_keyboard(),
            )
            return

        if state == "waiting_expense_receipt":
            if text in {normalize_text(BTN_CANCEL), normalize_text(BTN_MENU)}:
                clear_user_state(message.from_id)
                await reply(message, 'Ввод отменен.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            next_data = dict(state_data)
            if text == normalize_text(BTN_SKIP_RECEIPT):
                next_data["receipt_kind"] = None
                next_data["receipt_attachment"] = None
                next_data["receipt_url"] = None
                next_data["receipt_original_name"] = None
            else:
                receipt_kind, receipt_attachment, receipt_url, receipt_original_name = extract_vk_receipt_attachment(message)
                if not receipt_attachment:
                    await reply(
                        message,
                        'Отправьте фотографию или файл с чеком. Если чека нет, нажмите кнопку «Продолжить без чека».',
                        keyboard=expense_receipt_keyboard(),
                    )
                    return
                next_data["receipt_kind"] = receipt_kind
                next_data["receipt_attachment"] = receipt_attachment
                next_data["receipt_url"] = receipt_url
                next_data["receipt_original_name"] = receipt_original_name

            set_user_state(message.from_id, "confirm", next_data)
            summary_lines = [
                'Проверьте данные перед сохранением:',
                f"Дата: {next_data.get('date')}",
                f"Тип записи: {next_data.get('place')}",
                f"Проект: {next_data.get('project')}",
                f"Описание расхода: {next_data.get('expense_type')}",
                f"Сумма: {next_data.get('amount')}",
                f"Чек: {('приложен' if next_data.get('receipt_attachment') else 'без чека')}",
            ]
            await reply(message, "\n".join(summary_lines), keyboard=confirm_keyboard())
            return

        if state == "confirm":
            if text in {normalize_text(BTN_CANCEL), 'нет'}:
                clear_user_state(message.from_id)
                await reply(message, 'Внесение записи отменено.', keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            if text not in {normalize_text(BTN_CONFIRM), 'да'}:
                await reply(message, 'Используйте кнопки Подтвердить или Отменить.', keyboard=confirm_keyboard())
                return

            worker_id = state_data["worker_id"]
            form_data = dict(state_data)
            form_data.pop("worker_id", None)
            try:
                expense_id = await run_sync(save_to_db, form_data, worker_id)
            except ValueError as exc:
                clear_user_state(message.from_id)
                await reply(message, str(exc), keyboard=main_menu_keyboard_for_user(message.from_id))
                return

            saved_receipt = None
            if form_data.get("place") == 'Расходы' and form_data.get("receipt_url"):
                try:
                    saved_receipt = await save_remote_receipt(
                        form_data["receipt_url"],
                        expense_id,
                        "vk",
                        form_data.get("receipt_kind") or "document",
                        form_data.get("receipt_original_name"),
                    )
                    await run_sync(
                        update_expense_receipt,
                        expense_id,
                        saved_receipt["path"],
                        saved_receipt["original_name"],
                        saved_receipt["media_type"],
                        None,
                        form_data.get("receipt_url"),
                    )
                except Exception:
                    saved_receipt = None

            clear_user_state(message.from_id)
            await reply(message, 'Данные успешно сохранены.', keyboard=main_menu_keyboard_for_user(message.from_id))

            if form_data.get("place") == 'Расходы':
                worker = get_worker_contacts_by_id(worker_id)
                full_name = worker[1] if worker else 'сотрудника'
                expense_date = date.today().replace(day=int(form_data.get("date")))
                admin_text = format_expense_admin_text(
                    full_name,
                    expense_date,
                    form_data.get("amount"),
                    form_data.get("expense_type"),
                    bool(saved_receipt or form_data.get("receipt_attachment")),
                    project_name=form_data.get("project"),
                )
                await notify_admins(
                    admin_text + "\n\n"
                    f'Команды: /approve_expense {expense_id} или /reject_expense {expense_id}',
                    telegram_reply_markup=telegram_expense_keyboard(expense_id),
                    vk_attachment=form_data.get("receipt_attachment"),
                    telegram_receipt_kind=form_data.get("receipt_kind"),
                    telegram_receipt_url=form_data.get("receipt_url"),
                    telegram_receipt_path=saved_receipt["path"] if saved_receipt else None,
                )
            else:
                schedule_sheet_sync(date.today().year, date.today().month)
            return

        if state == "choosing_stats_period":
            if text == normalize_text(BTN_MENU):
                clear_user_state(message.from_id)
                await show_main_menu(message, (await run_sync(get_worker_by_vk_id_cached, message.from_id))[4])
                return

            worker_id = state_data["worker_id"]

            if text == normalize_text(BTN_STATS_FIRST):
                half = "first"
            elif text == normalize_text(BTN_STATS_SECOND):
                half = "second"
            else:
                await reply(message, 'Выберите период кнопками ниже.', keyboard=stats_period_keyboard())
                return

            clear_user_state(message.from_id)
            await reply(message, await run_sync(get_half_month_stats_text, worker_id, half), keyboard=back_to_menu_keyboard(message.from_id))
            return

        await reply(message, f"Сообщение не распознано.\n{pending_state_hint(state)}", keyboard=input_step_keyboard())

    class ReplayVKMessage:
        def __init__(self, from_id: int, text: str | None):
            self.from_id = int(from_id)
            self.peer_id = int(from_id)
            self.text = text or ""
            self.attachments = []
            self.conversation_message_id = None
            self.id = None

    async def auto_replay_pending_vk_messages():
        await asyncio.sleep(1)
        rows = await run_sync(list_conversations_needing_reply, "vk")
        logger.info("VK startup replay: found %d conversations needing reply", len(rows))
        replayed_user_ids = set()
        for row in rows:
            message_text = (row.get("message_text") or "").strip()
            state_name = row.get("state")
            if not message_text and not state_name:
                logger.info("VK startup replay: skipping empty pending row for user %s", row["user_key"])
                continue

            replay_message = ReplayVKMessage(int(row["user_key"]), message_text)
            if state_name == "waiting_expense_receipt" and row.get("has_media"):
                logger.info(
                    "VK startup replay: pending media receipt for user %s in state %s, requesting resend",
                    row["user_key"],
                    state_name,
                )
                try:
                    await reply(
                        replay_message,
                        "Последним сообщением был чек с вложением. После перезапуска его нужно отправить заново.",
                        keyboard=expense_receipt_keyboard(),
                    )
                except Exception:
                    logger.exception("Failed to notify VK user %s about receipt resend after restart", row["user_key"])
                continue

            if state_name:
                set_user_state(replay_message.from_id, state_name, row.get("state_data") or {})

            logger.info(
                "VK startup replay: reprocessing pending message for user %s (state=%s, updated_at=%s)",
                row["user_key"],
                state_name,
                row.get("updated_at"),
            )
            try:
                await message_handler(replay_message)
            except Exception:
                logger.exception("Failed to auto-replay pending VK message for user %s", row["user_key"])
            else:
                logger.info("VK startup replay: completed replay for user %s", row["user_key"])
                replayed_user_ids.add(str(row["user_key"]))

        active_states = await run_sync(list_active_conversation_states, "vk")
        logger.info("VK startup replay: found %d active conversation states", len(active_states))
        for row in active_states:
            if str(row["user_key"]) in replayed_user_ids:
                continue
            try:
                reminder_message = ReplayVKMessage(int(row["user_key"]), "")
                await reply(
                    reminder_message,
                    "Бот перезапустился, но ваш сценарий сохранён.\n" + pending_state_hint(row.get("state")),
                    keyboard=input_step_keyboard(),
                )
            except Exception:
                logger.exception("Failed to send VK continuation reminder for user %s", row["user_key"])
            else:
                logger.info(
                    "VK startup reminder: sent continuation hint to user %s for state %s",
                    row["user_key"],
                    row.get("state"),
                )

    bot.loop_wrapper.add_task(auto_replay_pending_vk_messages())


def extract_project_id_from_label(label: str) -> int:
    text = (label or "").strip()
    if not text.endswith(")") or " (" not in text:
        raise ValueError("Project ID not found")
    raw_id = text.rsplit("(", 1)[1].rstrip(")")
    return int(raw_id)


def project_picker_keyboard(rows, page: int = 0, page_size: int = 6, *, mode: str = "pick"):
    start = page * page_size
    page_rows = rows[start:start + page_size]
    keyboard_rows = []

    for project_id, project_name in page_rows:
        label = project_name if mode == "pick" else f"{project_name} ({project_id})"
        keyboard_rows.append([label])

    nav_row = []
    if page > 0:
        nav_row.append(BTN_PROJECTS_PREV)
    if start + page_size < len(rows):
        nav_row.append(BTN_PROJECTS_NEXT)
    if nav_row:
        keyboard_rows.append(nav_row)

    keyboard_rows.append([BTN_CANCEL, BTN_MENU])
    return build_keyboard(keyboard_rows)


def admin_menu_keyboard():
    return build_keyboard(
        [
            [BTN_ADD, BTN_STATS],
            [BTN_LOGS, BTN_EXPENSES],
            [BTN_MY_ID, BTN_LINK_TELEGRAM, BTN_PAST_MONTH],
            [BTN_ADMIN_USERS, BTN_ADMIN_EXPENSES],
            [BTN_ADMIN_WORKERS, BTN_ADMIN_SHOW_RATES, BTN_ADMIN_SHOW_PROJECTS],
            ["Обновить проекты Yougile"],
            ["Заявки за прошлый месяц"],
            [BTN_ADMIN_SET_RATE, BTN_ADMIN_ADD_BONUS],
            [BTN_ADMIN_ADD_PENALTY, BTN_ADMIN_SHOW_BONUSES],
            [BTN_ADMIN_SHOW_PENALTIES, BTN_ADMIN_MERGE],
            [BTN_ADMIN_MAKE_ADMIN, BTN_ADMIN_REMOVE_ADMIN, BTN_ADMIN_CLOSE_MONTH],
            [BTN_ADMIN_DELETE_WORKER, BTN_ADMIN_ADD_PROJECT, BTN_ADMIN_DELETE_PROJECT],
        ]
    )


def admin_shortcuts_keyboard():
    return build_keyboard(
        [
            [BTN_ADMIN_USERS, BTN_ADMIN_EXPENSES],
            [BTN_ADMIN_WORKERS, BTN_ADMIN_SHOW_RATES, BTN_ADMIN_SHOW_PROJECTS],
            ["Обновить проекты Yougile"],
            ["Заявки за прошлый месяц"],
            [BTN_ADMIN_SET_RATE, BTN_ADMIN_ADD_BONUS],
            [BTN_ADMIN_ADD_PENALTY, BTN_ADMIN_SHOW_BONUSES],
            [BTN_ADMIN_SHOW_PENALTIES, BTN_ADMIN_MERGE],
            [BTN_ADMIN_MAKE_ADMIN, BTN_ADMIN_REMOVE_ADMIN],
            [BTN_ADMIN_DELETE_WORKER, BTN_ADMIN_ADD_PROJECT],
            [BTN_ADMIN_DELETE_PROJECT, BTN_ADMIN_CLOSE_MONTH],
            [BTN_MENU],
        ]
    )

