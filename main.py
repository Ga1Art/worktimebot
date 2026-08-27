import asyncio
import atexit
import logging
from datetime import date

from fastapi import FastAPI, Header, HTTPException
import uvicorn

from aiogram import Bot, Dispatcher
from vkbottle import Bot as VKBot
from config import API_KEY, BOT_TOKEN, ENABLE_TELEGRAM_BOT, ENABLE_VK_BOT, VK_TOKEN
from bot.handlers import auto_replay_pending_telegram_messages, router
from bot.pending_inbound import TelegramPendingInboundMiddleware, clear_pending_for_current_context
from bot.postgres_fsm_storage import PostgresFSMStorage
from bot.core.dedup import TelegramDedupMiddleware
from bot.core.outgoing_dedup import build_outgoing_key, should_skip_outgoing
from bot.core.notifications import set_telegram_bot, set_vk_bot as set_vk_notification_bot
from services.google_sheets import (
    archive_monthly_report,
    ensure_admin_sheets,
    google_error_message,
    sync_admin_data_from_sheets,
    sync_monthly_report_to_current_sheet,
)
from services.monthly_closing import close_month_tracked, run_month_close_scheduler
from services.db import get_connection
from services.reporting import build_monthly_report
from services.runtime_lock import TELEGRAM_RUNTIME_LOCK

print("BOT STARTED")

logging.basicConfig(level=logging.INFO)

telegram_bot: Bot | None = None
dp: Dispatcher | None = None
vk_bot: VKBot | None = None


if ENABLE_TELEGRAM_BOT:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is required when ENABLE_TELEGRAM_BOT=true.")

    telegram_bot = Bot(token=BOT_TOKEN)

    _original_send_message = telegram_bot.send_message
    _original_send_photo = telegram_bot.send_photo
    _original_send_document = telegram_bot.send_document

    async def _dedup_send_message(chat_id, text, *args, **kwargs):
        outgoing_key = build_outgoing_key("tg", chat_id, "message", text=text, markup=kwargs.get("reply_markup"))
        if should_skip_outgoing(outgoing_key):
            return None
        result = await _original_send_message(chat_id, text, *args, **kwargs)
        await asyncio.to_thread(clear_pending_for_current_context)
        return result

    async def _dedup_send_photo(chat_id, photo, *args, **kwargs):
        caption = kwargs.get("caption")
        outgoing_key = build_outgoing_key(
            "tg",
            chat_id,
            "photo",
            text=caption,
            attachment=str(photo),
            markup=kwargs.get("reply_markup"),
        )
        if should_skip_outgoing(outgoing_key):
            return None
        result = await _original_send_photo(chat_id, photo, *args, **kwargs)
        await asyncio.to_thread(clear_pending_for_current_context)
        return result

    async def _dedup_send_document(chat_id, document, *args, **kwargs):
        caption = kwargs.get("caption")
        outgoing_key = build_outgoing_key(
            "tg",
            chat_id,
            "document",
            text=caption,
            attachment=str(document),
            markup=kwargs.get("reply_markup"),
        )
        if should_skip_outgoing(outgoing_key):
            return None
        result = await _original_send_document(chat_id, document, *args, **kwargs)
        await asyncio.to_thread(clear_pending_for_current_context)
        return result

    telegram_bot.send_message = _dedup_send_message
    telegram_bot.send_photo = _dedup_send_photo
    telegram_bot.send_document = _dedup_send_document

    set_telegram_bot(telegram_bot)
    dp = Dispatcher(storage=PostgresFSMStorage())
    dp.message.middleware(TelegramDedupMiddleware())
    dp.message.middleware(TelegramPendingInboundMiddleware())
    dp.include_router(router)
else:
    logging.info("Telegram bot polling disabled by ENABLE_TELEGRAM_BOT=false.")

if ENABLE_VK_BOT:
    if not VK_TOKEN:
        raise RuntimeError("VK_TOKEN is required when ENABLE_VK_BOT=true.")

    vk_bot = VKBot(token=VK_TOKEN)
    set_vk_notification_bot(vk_bot)
else:
    logging.info("VK notifications disabled by ENABLE_VK_BOT=false.")

# FastAPI (ОДИН app)
app = FastAPI()


@app.get("/")
def root():
    return {"status": "ok"}


@app.get("/manager-table")
def get_manager_table(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT * FROM manager_table")

    columns = [desc[0] for desc in cursor.description]
    rows = cursor.fetchall()

    cursor.close()
    conn.close()

    return {
        "columns": columns,
        "rows": [list(r) for r in rows]
    }


@app.get("/monthly-report-preview")
def monthly_report_preview(
    year: int | None = None,
    month: int | None = None,
    x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    today = date.today()
    report_year = year or today.year
    report_month = month or today.month

    if not 1 <= report_month <= 12:
        raise HTTPException(status_code=400, detail="Month must be in range 1..12")

    return build_monthly_report(report_year, report_month)


@app.post("/monthly-report-sync-current")
def monthly_report_sync_current(
    year: int | None = None,
    month: int | None = None,
    x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    today = date.today()
    report_year = year or today.year
    report_month = month or today.month

    if not 1 <= report_month <= 12:
        raise HTTPException(status_code=400, detail="Month must be in range 1..12")

    try:
        result = sync_monthly_report_to_current_sheet(report_year, report_month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=google_error_message(exc)) from exc

    return {
        "status": "ok",
        "year": report_year,
        "month": report_month,
        "sheet_title": result["sheet_title"],
        "row_count": result["row_count"],
        "column_count": result["column_count"],
    }


@app.post("/monthly-report-archive")
def monthly_report_archive(
    year: int | None = None,
    month: int | None = None,
    x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    today = date.today()
    report_year = year or today.year
    report_month = month or today.month

    if not 1 <= report_month <= 12:
        raise HTTPException(status_code=400, detail="Month must be in range 1..12")

    try:
        result = archive_monthly_report(report_year, report_month)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=google_error_message(exc)) from exc

    return {
        "status": "ok",
        "year": report_year,
        "month": report_month,
        "sheet_title": result["sheet_title"],
        "row_count": result["row_count"],
        "column_count": result["column_count"],
    }


@app.post("/monthly-report-close")
def monthly_report_close(
    year: int | None = None,
    month: int | None = None,
    x_api_key: str = Header(None),
):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    today = date.today()
    report_year = year or today.year
    report_month = month or today.month

    if not 1 <= report_month <= 12:
        raise HTTPException(status_code=400, detail="Month must be in range 1..12")

    try:
        result = close_month_tracked(report_year, report_month, trigger_source="api")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=google_error_message(exc)) from exc

    return {
        "status": "ok",
        "close_status": result["status"],
        "archived_month": result["archived_month"],
        "current_month": result["current_month"],
    }


@app.post("/admin-sheet-sync")
def admin_sheet_sync(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        result = sync_admin_data_from_sheets()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=google_error_message(exc)) from exc

    return {
        "status": "ok",
        **result,
    }


@app.post("/admin-sheet-init")
def admin_sheet_init(x_api_key: str = Header(None)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

    try:
        result = ensure_admin_sheets()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=google_error_message(exc)) from exc

    return {
        "status": "ok",
        **result,
    }


async def main():
    telegram_lock_acquired = False
    if ENABLE_TELEGRAM_BOT:
        if telegram_bot is None or dp is None:
            raise RuntimeError("Telegram bot was not initialized.")
        if not TELEGRAM_RUNTIME_LOCK.acquire():
            raise SystemExit("Another Telegram bot instance is already running.")
        telegram_lock_acquired = True
        atexit.register(TELEGRAM_RUNTIME_LOCK.release)

    try:
        config = uvicorn.Config(app, host="0.0.0.0", port=8000, log_level="info")
        server = uvicorn.Server(config)

        tasks = [
            server.serve(),
            run_month_close_scheduler("telegram_api"),
        ]
        if ENABLE_TELEGRAM_BOT:
            logging.info("Telegram bot and FastAPI started...")
            await auto_replay_pending_telegram_messages(telegram_bot)
            tasks.insert(0, dp.start_polling(telegram_bot))
        else:
            logging.info("FastAPI started without Telegram polling.")

        await asyncio.gather(*tasks)
    finally:
        if telegram_lock_acquired:
            TELEGRAM_RUNTIME_LOCK.release()


if __name__ == "__main__":
    asyncio.run(main())
