"""Read-only project import. Work records remain in the bot database."""
import asyncio
import json
import logging
import time
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from config import YOUGILE_API_BASE_URL, YOUGILE_API_KEY, YOUGILE_SYNC_INTERVAL_SECONDS
from services.db import get_connection, _ensure_projects_table, _ensure_work_logs_project_link, backfill_known_project_ids


def fetch_projects():
    if not YOUGILE_API_KEY:
        raise ValueError("Задайте YOUGILE_API_KEY для синхронизации проектов.")
    projects = []
    seen = set()
    offset = 0
    for _ in range(1000):
        query = urlencode({"limit": 100, "offset": offset, "includeDeleted": "true"})
        request = Request(
            f"{YOUGILE_API_BASE_URL.rstrip('/')}/projects?{query}",
            headers={"Authorization": f"Bearer {YOUGILE_API_KEY}", "Content-Type": "application/json"},
        )
        try:
            with urlopen(request, timeout=20) as response:
                data = json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"Yougile: HTTP {exc.code}") from None
        except URLError as exc:
            reason = exc.reason
            if isinstance(reason, socket.gaierror):
                detail = "не удалось определить IP-адрес сервера (DNS)"
            elif isinstance(reason, ssl.SSLError):
                detail = "ошибка проверки TLS/SSL-соединения"
            elif isinstance(reason, TimeoutError):
                detail = "истекло время ожидания соединения"
            else:
                detail = f"ошибка соединения ({type(reason).__name__})"
            raise RuntimeError(f"Не удалось загрузить проекты Yougile: {detail}.") from None
        except TimeoutError:
            raise RuntimeError("Yougile: истекло время ожидания ответа сервера.") from None
        except ValueError:
            raise RuntimeError("Yougile вернул некорректный JSON. Проверьте адрес API: нужен адрес, заканчивающийся на /api-v2.") from None
        if not isinstance(data, dict) or not isinstance(data.get("content"), list):
            raise RuntimeError("Неожиданный формат списка проектов Yougile.")
        page = data["content"]
        for project in page:
            if not isinstance(project, dict) or not project.get("id") or not isinstance(project.get("title"), str) or not project["title"].strip():
                raise RuntimeError("Yougile вернул проект без ID или названия.")
            if project["id"] in seen:
                raise RuntimeError("Yougile вернул повторную страницу проектов.")
            seen.add(project["id"])
            projects.append(project)
        if not page or data.get("paging", {}).get("next") is False:
            return projects
        offset += len(page)
        # Stay below the company-wide 50 requests/minute limit.
        time.sleep(1.5)
    raise RuntimeError("Превышен лимит страниц проектов Yougile.")


def sync_projects():
    projects = fetch_projects()  # Fetch every page before changing local records.
    conn = get_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(72819042)")
            _ensure_projects_table(cursor)
            _ensure_work_logs_project_link(cursor)
            backfill_known_project_ids(cursor)
            for project in projects:
                external_id = project["id"]
                name = project["title"].strip()
                active = not project.get("deleted", False)
                cursor.execute("SELECT id FROM active_projects WHERE yougile_id = %s", (external_id,))
                existing = cursor.fetchone()
                cursor.execute("SELECT id, yougile_id FROM active_projects WHERE name = %s", (name,))
                local = cursor.fetchone()
                if local and local[1] is not None and local[1] != external_id:
                    raise ValueError(f"Название проекта занято: {name}. Измените название в Yougile или справочнике бота.")
                if existing:
                    if local and local[0] != existing[0]:
                        raise ValueError(f"Название проекта занято: {name}. Измените название в Yougile или справочнике бота.")
                    cursor.execute("UPDATE active_projects SET name = %s, active = %s WHERE id = %s", (name, active, existing[0]))
                elif local:
                    # Preserve IDs of manually created projects on an exact name match.
                    cursor.execute("UPDATE active_projects SET yougile_id = %s, active = %s WHERE id = %s", (external_id, active, local[0]))
                else:
                    cursor.execute("INSERT INTO active_projects(name, active, yougile_id) VALUES (%s, %s, %s)", (name, active, external_id))
            backfill_known_project_ids(cursor)
        conn.commit()
        return {"synced": len(projects)}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


async def run_project_sync_scheduler():
    if not YOUGILE_API_KEY:
        return
    while True:
        try:
            await asyncio.to_thread(sync_projects)
        except Exception:
            logging.exception("Yougile project sync failed; keeping local projects.")
        await asyncio.sleep(YOUGILE_SYNC_INTERVAL_SECONDS)
