from typing import Any, Optional, List
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message, CallbackQuery

from bot.core.bot_interface import BotInterface


class TelegramAdapter(BotInterface):
    def __init__(self, bot_instance):
        self.bot = bot_instance

    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[Any] = None):
        await self.bot.send_message(chat_id, text, reply_markup=reply_markup)

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False):
        # В aiogram callback.answer() принимает query, но мы передаем id
        # Нужно адаптировать в handlers
        pass

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: Optional[Any] = None):
        await self.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)

    def get_user_id(self, message: Any) -> int:
        if isinstance(message, Message):
            return message.from_user.id
        elif isinstance(message, CallbackQuery):
            return message.from_user.id
        return 0

    def get_chat_id(self, message: Any) -> int:
        if isinstance(message, Message):
            return message.chat.id
        elif isinstance(message, CallbackQuery):
            return message.message.chat.id
        return 0

    def get_message_text(self, message: Any) -> Optional[str]:
        if isinstance(message, Message):
            return message.text
        return None

    def get_callback_data(self, callback: Any) -> str:
        if isinstance(callback, CallbackQuery):
            return callback.data
        return ""

    def create_inline_keyboard(self, buttons: List[List[Any]]) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=buttons)

    def create_keyboard_button(self, text: str, callback_data: str) -> InlineKeyboardButton:
        return InlineKeyboardButton(text=text, callback_data=callback_data)