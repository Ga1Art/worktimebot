from __future__ import annotations

import asyncio
from contextvars import ContextVar, Token
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import Message

from services.db import (
    clear_pending_inbound_message,
    mark_conversation_outbound,
    save_conversation_inbound,
    save_pending_inbound_message,
)


_outbound_context: ContextVar[tuple[str, str] | None] = ContextVar("outbound_context", default=None)


def set_outbound_context(platform: str, user_key: str) -> Token:
    return _outbound_context.set((platform, str(user_key)))


def reset_outbound_context(token: Token) -> None:
    _outbound_context.reset(token)


def get_outbound_context() -> tuple[str, str] | None:
    return _outbound_context.get()


def clear_pending_for_current_context() -> None:
    current = get_outbound_context()
    if not current:
        return
    platform, user_key = current
    mark_conversation_outbound(platform, user_key)
    clear_pending_inbound_message(platform, user_key)


class TelegramPendingInboundMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: dict[str, Any]):
        if not isinstance(event, Message) or event.chat.type != "private":
            return await handler(event, data)
        if (event.text or "").strip() == "/retry_last":
            return await handler(event, data)

        state = data.get("state")
        state_name = await state.get_state() if state else None
        state_data = await state.get_data() if state else {}
        user_key = str(event.from_user.id)
        has_media = bool(event.photo or event.document)

        await asyncio.to_thread(
            save_conversation_inbound,
            "telegram",
            user_key,
            event.text or event.caption,
            state_name,
            state_data,
            has_media=has_media,
        )

        await asyncio.to_thread(
            save_pending_inbound_message,
            "telegram",
            user_key,
            event.text or event.caption,
            state_name,
            state_data,
            has_media=has_media,
        )

        token = set_outbound_context("telegram", user_key)
        try:
            return await handler(event, data)
        except Exception as exc:
            await asyncio.to_thread(
                save_conversation_inbound,
                "telegram",
                user_key,
                event.text or event.caption,
                state_name,
                state_data,
                has_media=has_media,
                error_text=str(exc),
            )
            await asyncio.to_thread(
                save_pending_inbound_message,
                "telegram",
                user_key,
                event.text or event.caption,
                state_name,
                state_data,
                has_media=has_media,
                error_text=str(exc),
            )
            raise
        finally:
            reset_outbound_context(token)
