import asyncio
from copy import copy
from typing import Any, Mapping, overload

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey
from aiogram.fsm.storage.memory import DataNotDictLikeError

from services.db import clear_conversation_state, load_conversation_state, save_conversation_state


def _storage_key_to_user_key(key: StorageKey) -> str:
    thread_id = "" if key.thread_id is None else str(key.thread_id)
    business_connection_id = "" if key.business_connection_id is None else key.business_connection_id
    return ":".join(
        [
            str(key.bot_id),
            str(key.chat_id),
            str(key.user_id),
            thread_id,
            business_connection_id,
            key.destiny,
        ]
    )


class PostgresFSMStorage(BaseStorage):
    platform = "telegram"

    async def close(self) -> None:
        return None

    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        user_key = _storage_key_to_user_key(key)
        current = await asyncio.to_thread(load_conversation_state, self.platform, user_key)
        current_data = (current or {}).get("data", {})
        next_state = state.state if isinstance(state, State) else state

        if next_state is None and not current_data:
            await asyncio.to_thread(clear_conversation_state, self.platform, user_key)
            return

        await asyncio.to_thread(save_conversation_state, self.platform, user_key, next_state, current_data)

    async def get_state(self, key: StorageKey) -> str | None:
        user_key = _storage_key_to_user_key(key)
        current = await asyncio.to_thread(load_conversation_state, self.platform, user_key)
        if not current:
            return None
        return current.get("state")

    async def set_data(self, key: StorageKey, data: Mapping[str, Any]) -> None:
        if not isinstance(data, dict):
            msg = f"Data must be a dict or dict-like object, got {type(data).__name__}"
            raise DataNotDictLikeError(msg)

        user_key = _storage_key_to_user_key(key)
        current = await asyncio.to_thread(load_conversation_state, self.platform, user_key)
        current_state = (current or {}).get("state")

        if current_state is None and not data:
            await asyncio.to_thread(clear_conversation_state, self.platform, user_key)
            return

        await asyncio.to_thread(save_conversation_state, self.platform, user_key, current_state, dict(data))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        user_key = _storage_key_to_user_key(key)
        current = await asyncio.to_thread(load_conversation_state, self.platform, user_key)
        if not current:
            return {}
        return dict(current.get("data") or {})

    @overload
    async def get_value(self, storage_key: StorageKey, dict_key: str) -> Any | None: ...

    @overload
    async def get_value(self, storage_key: StorageKey, dict_key: str, default: Any) -> Any: ...

    async def get_value(
        self,
        storage_key: StorageKey,
        dict_key: str,
        default: Any | None = None,
    ) -> Any | None:
        data = await self.get_data(storage_key)
        return copy(data.get(dict_key, default))
