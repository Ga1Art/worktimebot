import json
import mimetypes
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import STANDFLOW_API_BASE_URL, STANDFLOW_BOT_API_TOKEN


class StandFlowError(RuntimeError):
    pass


def _ensure_configured() -> None:
    if not STANDFLOW_API_BASE_URL or not STANDFLOW_BOT_API_TOKEN:
        raise StandFlowError("StandFlow integration is not configured.")


def _request_json(method: str, path: str, *, params: dict | None = None, payload: dict | None = None):
    _ensure_configured()
    base_url = STANDFLOW_API_BASE_URL.rstrip("/")
    query = f"?{urlencode(params)}" if params else ""
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url}{path}{query}",
        data=body,
        method=method,
        headers={
            "Content-Type": "application/json",
            "X-Work-Time-Bot-Token": STANDFLOW_BOT_API_TOKEN,
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise StandFlowError(f"StandFlow returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise StandFlowError(f"StandFlow is unavailable: {exc.reason}") from exc


def _request_multipart(
    method: str,
    path: str,
    *,
    fields: dict,
    file_field: tuple[str, str, bytes, str] | None = None,
):
    _ensure_configured()
    boundary = f"----standflow-{uuid.uuid4().hex}"
    body = bytearray()

    for name, value in fields.items():
        if value is None:
            continue
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")

    if file_field is not None:
        field_name, filename, content, content_type = file_field
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{filename}"\r\n'
            ).encode("utf-8")
        )
        body.extend(f"Content-Type: {content_type}\r\n\r\n".encode("utf-8"))
        body.extend(content)
        body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    request = Request(
        f"{STANDFLOW_API_BASE_URL.rstrip('/')}{path}",
        data=bytes(body),
        method=method,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Work-Time-Bot-Token": STANDFLOW_BOT_API_TOKEN,
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise StandFlowError(f"StandFlow returned {exc.code}: {detail}") from exc
    except URLError as exc:
        raise StandFlowError(f"StandFlow is unavailable: {exc.reason}") from exc


def get_task_cards(telegram_chat_id: int | str, *, due_today: bool = False):
    return _request_json(
        "GET",
        "/api/tasks/bot/my/cards",
        params={
            "telegram_chat_id": str(telegram_chat_id),
            "due_today": str(due_today).lower(),
        },
    )


def get_reminders(telegram_chat_id: int | str):
    return _request_json(
        "GET",
        "/api/bot/reminders",
        params={"telegram_chat_id": str(telegram_chat_id)},
    )


def get_daily_summary(telegram_chat_id: int | str):
    return _request_json(
        "GET",
        "/api/bot/daily-summary",
        params={"telegram_chat_id": str(telegram_chat_id)},
    )


def confirm_telegram_link(code: str, telegram_chat_id: int | str):
    return _request_json(
        "POST",
        "/api/users/telegram-link",
        payload={
            "code": code,
            "telegram_chat_id": str(telegram_chat_id),
        },
    )


def list_telegram_link_candidates():
    return _request_json("GET", "/api/users/bot/telegram-link-candidates")


def create_telegram_link_code(user_id: int):
    return _request_json("POST", f"/api/users/bot/{user_id}/telegram-link-code")


def list_active_projects():
    return _request_json("GET", "/api/projects/bot/active")


def report_issue(
    telegram_chat_id: int | str,
    *,
    project_id: int,
    title: str,
    description: str,
    severity: str,
    upload_filename: str | None = None,
    upload_content: bytes | None = None,
    upload_content_type: str | None = None,
):
    file_field = None
    if upload_filename and upload_content:
        content_type = upload_content_type or mimetypes.guess_type(upload_filename)[0] or "application/octet-stream"
        file_field = ("upload", upload_filename, upload_content, content_type)
    return _request_multipart(
        "POST",
        "/api/issues/bot-report",
        fields={
            "telegram_chat_id": str(telegram_chat_id),
            "project_id": project_id,
            "title": title,
            "description": description,
            "severity": severity,
        },
        file_field=file_field,
    )


def complete_task(telegram_chat_id: int | str, task_id: int, comment: str | None = None):
    payload = {"telegram_chat_id": str(telegram_chat_id)}
    if comment:
        payload["comment"] = comment
    return _request_json("POST", f"/api/tasks/bot/{task_id}/complete", payload=payload)


def add_task_comment(telegram_chat_id: int | str, task_id: int, content: str):
    return _request_json(
        "POST",
        f"/api/tasks/bot/{task_id}/comments",
        payload={"telegram_chat_id": str(telegram_chat_id), "content": content},
    )


def import_work_log(
    telegram_chat_id: int | str,
    *,
    duration_minutes: int,
    work_type: str,
    description: str | None = None,
    project_id: int | None = None,
):
    payload = {
        "telegram_chat_id": str(telegram_chat_id),
        "duration_minutes": duration_minutes,
        "work_type": work_type,
        "source": "work_time_bot",
    }
    if description:
        payload["description"] = description
    if project_id is not None:
        payload["project_id"] = project_id
    return _request_json("POST", "/api/work-logs/bot-import", payload=payload)
