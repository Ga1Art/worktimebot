import asyncio
import logging

from fastapi import FastAPI, Header, HTTPException

from aiogram import Bot, Dispatcher
from config import BOT_TOKEN, API_KEY
from bot.handlers import router
from api_con import get_connection

logging.basicConfig(level=logging.INFO)

# Telegram bot
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
dp.include_router(router)

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


async def main():
    print("Bot started...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())