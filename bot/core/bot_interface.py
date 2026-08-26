from abc import ABC, abstractmethod
from typing import Any, Dict, Optional


class BotInterface(ABC):
    @abstractmethod
    async def send_message(self, chat_id: int, text: str, reply_markup: Optional[Any] = None):
        pass

    @abstractmethod
    async def answer_callback(self, callback_id: str, text: Optional[str] = None, show_alert: bool = False):
        pass

    @abstractmethod
    async def edit_message_reply_markup(self, chat_id: int, message_id: int, reply_markup: Optional[Any] = None):
        pass

    @abstractmethod
    def get_user_id(self, message: Any) -> int:
        pass

    @abstractmethod
    def get_chat_id(self, message: Any) -> int:
        pass

    @abstractmethod
    def get_message_text(self, message: Any) -> Optional[str]:
        pass

    @abstractmethod
    def get_callback_data(self, callback: Any) -> str:
        pass

    @abstractmethod
    def create_inline_keyboard(self, buttons: list) -> Any:
        pass

    @abstractmethod
    def create_keyboard_button(self, text: str, callback_data: str) -> Any:
        pass