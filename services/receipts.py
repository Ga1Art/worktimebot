from __future__ import annotations

import asyncio
from pathlib import Path
from urllib.parse import unquote, urlparse
from urllib.request import urlopen


RECEIPTS_DIR = Path("storage") / "receipts"


def ensure_receipts_dir() -> Path:
    RECEIPTS_DIR.mkdir(parents=True, exist_ok=True)
    return RECEIPTS_DIR


def _safe_suffix(original_name: str | None, media_type: str) -> str:
    if original_name:
        suffix = Path(original_name).suffix.strip()
        if suffix:
            return suffix[:16]
    if media_type == "photo":
        return ".jpg"
    if media_type == "document":
        return ".bin"
    return ".dat"


def _safe_name(original_name: str | None) -> str | None:
    if not original_name:
        return None
    return Path(original_name).name[:255]


def _build_path(expense_id: int, platform: str, media_type: str, original_name: str | None = None) -> Path:
    storage_dir = ensure_receipts_dir()
    suffix = _safe_suffix(original_name, media_type)
    return storage_dir / f"expense_{expense_id}_{platform}{suffix}"


async def save_telegram_receipt(bot, file_id: str, expense_id: int, media_type: str, original_name: str | None = None):
    destination = _build_path(expense_id, "telegram", media_type, original_name)
    await bot.download(file_id, destination=destination)
    return {
        "path": str(destination),
        "original_name": _safe_name(original_name) or destination.name,
        "media_type": media_type,
    }


def _download_to_path(url: str, destination: Path):
    with urlopen(url) as response, destination.open("wb") as output:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            output.write(chunk)


async def save_remote_receipt(
    url: str,
    expense_id: int,
    platform: str,
    media_type: str,
    original_name: str | None = None,
):
    parsed_name = Path(unquote(urlparse(url).path)).name or None
    destination = _build_path(expense_id, platform, media_type, original_name or parsed_name)
    await asyncio.to_thread(_download_to_path, url, destination)

    return {
        "path": str(destination),
        "original_name": _safe_name(original_name or parsed_name) or destination.name,
        "media_type": media_type,
    }
