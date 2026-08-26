from typing import Any, Optional, List
from vkbottle import Bot as VKBot
from vkbottle_types.objects import MessagesMessage, MessagesKeyboard, MessagesKeyboardButton

from bot.core.bot_interface import BotInterface


class VKAdapter(BotInterface):
    def __init__(self, bot_instance: VKBot):
        self.bot = bot_instance

    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[Any] = None):
        await self.bot.api.messages.send(
            peer_id=chat_id,
            message=text,
            keyboard=reply_markup,
            random_id=0
        )

    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False):
        # VK не имеет callback queries как Telegram, но можно отправить сообщение
        pass

    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: Optional[Any] = None):
        # VK не поддерживает редактирование клавиатуры напрямую
        pass

    def get_user_id(self, message: Any) -> int:
        if isinstance(message, MessagesMessage):
            return message.from_id
        return 0

    def get_chat_id(self, message: Any) -> int:
        if isinstance(message, MessagesMessage):
            return message.peer_id
        return 0

    def get_message_text(self, message: Any) -> Optional[str]:
        if isinstance(message, MessagesMessage):
            return message.text
        return None

    def get_callback_data(self, callback: Any) -> str:
        # VK использует payload в кнопках
        return ""

    def create_inline_keyboard(self, buttons: List[List[Any]]) -> MessagesKeyboard:
        keyboard = MessagesKeyboard(inline=True)
        for row in buttons:
            keyboard_row = []
            for button in row:
                if isinstance(button, MessagesKeyboardButton):
                    keyboard_row.append(button)
            keyboard.buttons.append(keyboard_row)
        return keyboard

    def create_keyboard_button(self, text: str, callback_data: str) -> MessagesKeyboardButton:
        return MessagesKeyboardButton(
            action={
                "type": "callback",
                "label": text,
                "payload": callback_data
            }
        )