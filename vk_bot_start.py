import atexit
import logging
import time
from config import ENABLE_VK_BOT, VK_TOKEN
from bot.adapters.vk_handlers import set_vk_bot
from bot.core.notifications import set_vk_bot as set_vk_notification_bot
from services.monthly_closing import run_month_close_scheduler
from services.past_month_requests import run_past_request_archive_scheduler
from vkbottle import Bot as VKBot
from services.runtime_lock import VK_RUNTIME_LOCK

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    if not ENABLE_VK_BOT:
        logging.info("VK bot polling disabled by ENABLE_VK_BOT=false.")
        while True:
            time.sleep(3600)

    if not VK_RUNTIME_LOCK.acquire():
        raise SystemExit("Another VK bot instance is already running.")

    atexit.register(VK_RUNTIME_LOCK.release)

    try:
        vk_bot = VKBot(token=VK_TOKEN)
        set_vk_bot(vk_bot)
        set_vk_notification_bot(vk_bot)

        logging.info("VK Bot polling started...")
        vk_bot.loop_wrapper.add_task(vk_bot.run_polling())
        vk_bot.loop_wrapper.add_task(run_month_close_scheduler("vk_bot"))
        vk_bot.loop_wrapper.add_task(run_past_request_archive_scheduler())
        vk_bot.loop_wrapper.run()
    finally:
        VK_RUNTIME_LOCK.release()
