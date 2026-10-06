import asyncio
import json
import mimetypes
import random
import uuid
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

from aiogram.types import FSInputFile
from bot.core.outgoing_dedup import build_outgoing_key, should_skip_outgoing


telegram_bot = None
vk_bot = None


def set_telegram_bot(bot_instance):
    global telegram_bot
    telegram_bot = bot_instance


def set_vk_bot(bot_instance):
    global vk_bot
    vk_bot = bot_instance


async def send_telegram_message(chat_id: Optional[int], text: str, reply_markup=None):
    if not chat_id or telegram_bot is None:
        return False

    try:
        await telegram_bot.send_message(chat_id, text, reply_markup=reply_markup)
        return True
    except Exception:
        return False


async def send_telegram_media(chat_id: Optional[int], media_kind: str, media: str, caption: str, reply_markup=None):
    if not chat_id or telegram_bot is None or not media:
        return False

    preferred_kinds = ["photo", "document"] if media_kind == "photo" else ["document", "photo"]

    for kind in preferred_kinds:
        try:
            telegram_media = FSInputFile(media) if Path(media).exists() else media
            if kind == "photo":
                await telegram_bot.send_photo(chat_id, telegram_media, caption=caption, reply_markup=reply_markup)
                return True
            if kind == "document":
                await telegram_bot.send_document(chat_id, telegram_media, caption=caption, reply_markup=reply_markup)
                return True
        except Exception:
            continue
    return False


async def send_vk_message(vk_id: Optional[int], text: str, keyboard=None):
    if not vk_id or vk_bot is None:
        return False

    outgoing_key = build_outgoing_key("vk", vk_id, "message", text=text, markup=keyboard)
    if should_skip_outgoing(outgoing_key):
        return True

    try:
        keyboard_args = {"keyboard": keyboard} if keyboard is not None else {}
        await vk_bot.api.messages.send(
            peer_id=vk_id,
            message=text,
            random_id=random.randint(1, 2_147_483_647),
            **keyboard_args,
        )
        return True
    except Exception:
        return False


def _read_vk_api_value(data, key):
    if data is None:
        return None
    if isinstance(data, dict):
        return data.get(key)
    return getattr(data, key, None)


def _encode_multipart(field_name: str, file_path: str, content_type: str | None = None):
    boundary = f"----CodexBoundary{uuid.uuid4().hex}"
    filename = Path(file_path).name
    mime = content_type or mimetypes.guess_type(filename)[0] or "application/octet-stream"
    file_bytes = Path(file_path).read_bytes()

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, boundary


def _upload_file_to_url(upload_url: str, field_name: str, file_path: str):
    body, boundary = _encode_multipart(field_name, file_path)
    request = Request(
        upload_url,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    with urlopen(request) as response:
        return json.loads(response.read().decode("utf-8"))


async def _send_vk_photo(peer_id: int, path: Path, text: str):
    upload_server = await vk_bot.api.photos.get_messages_upload_server(peer_id=peer_id)
    upload_url = _read_vk_api_value(upload_server, "upload_url")
    if not upload_url:
        return False

    upload_result = await asyncio.to_thread(_upload_file_to_url, upload_url, "photo", str(path))
    photo = _read_vk_api_value(upload_result, "photo")
    server = _read_vk_api_value(upload_result, "server")
    upload_hash = _read_vk_api_value(upload_result, "hash")
    if not photo or server is None or not upload_hash:
        return False

    saved = await vk_bot.api.photos.save_messages_photo(photo=photo, server=server, hash=upload_hash)
    photo_item = saved[0] if isinstance(saved, list) and saved else saved
    owner_id = _read_vk_api_value(photo_item, "owner_id")
    media_id = _read_vk_api_value(photo_item, "id")
    if owner_id is None or media_id is None:
        return False

    await vk_bot.api.messages.send(
        peer_id=peer_id,
        message=text,
        attachment=f"photo{owner_id}_{media_id}",
        random_id=random.randint(1, 2_147_483_647),
    )
    return True


async def _send_vk_document(peer_id: int, path: Path, text: str):
    upload_server = await vk_bot.api.docs.get_messages_upload_server(peer_id=peer_id, type="doc")
    upload_url = _read_vk_api_value(upload_server, "upload_url")
    if not upload_url:
        return False

    upload_result = await asyncio.to_thread(_upload_file_to_url, upload_url, "file", str(path))
    uploaded_file = _read_vk_api_value(upload_result, "file")
    if not uploaded_file:
        return False

    saved = await vk_bot.api.docs.save(file=uploaded_file, title=path.name)
    if isinstance(saved, list):
        doc = saved[0] if saved else None
    elif isinstance(saved, dict):
        doc = saved.get("doc") or saved.get("type") or saved
    else:
        doc = saved

    owner_id = _read_vk_api_value(doc, "owner_id")
    media_id = _read_vk_api_value(doc, "id")
    if owner_id is None or media_id is None:
        return False

    await vk_bot.api.messages.send(
        peer_id=peer_id,
        message=text,
        attachment=f"doc{owner_id}_{media_id}",
        random_id=random.randint(1, 2_147_483_647),
    )
    return True


async def send_vk_media(vk_id: Optional[int], media_path: str, text: str):
    if not vk_id or vk_bot is None or not media_path:
        return False

    path = Path(media_path)
    if not path.exists():
        return False

    outgoing_key = build_outgoing_key("vk", vk_id, "message", text=text, attachment=str(path))
    if should_skip_outgoing(outgoing_key):
        return True

    mime = mimetypes.guess_type(path.name)[0] or ""
    is_image = mime.startswith("image/")
    senders = [_send_vk_photo, _send_vk_document] if is_image else [_send_vk_document, _send_vk_photo]

    for sender in senders:
        try:
            if await sender(vk_id, path, text):
                return True
        except Exception:
            continue
    return False


async def notify_worker(chat_id: Optional[int], vk_id: Optional[int], text: str):
    telegram_sent = await send_telegram_message(chat_id, text)
    vk_sent = await send_vk_message(vk_id, text)
    return telegram_sent or vk_sent


async def notify_expense_origin(
    source_platform: Optional[str],
    source_peer_id: Optional[int],
    chat_id: Optional[int],
    vk_id: Optional[int],
    text: str,
):
    if source_platform == "telegram":
        sent = await send_telegram_message(source_peer_id or chat_id, text)
        if sent:
            return True
        return await notify_worker(chat_id, vk_id, text)
    if source_platform == "vk":
        sent = await send_vk_message(source_peer_id or vk_id, text)
        if sent:
            return True
        return await notify_worker(chat_id, vk_id, text)
    return await notify_worker(chat_id, vk_id, text)
