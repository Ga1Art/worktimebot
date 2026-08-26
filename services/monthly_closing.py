import asyncio
import logging
from datetime import date, datetime, timedelta

from config import GOOGLE_SHEETS_CURRENT_SHEET
from services.db import get_connection
from services.google_sheets import close_month


AUTO_CLOSE_CHECK_INTERVAL_SECONDS = 3600
logger = logging.getLogger(__name__)


def get_previous_month(today: date | None = None) -> tuple[int, int]:
    today = today or date.today()
    first_day_of_month = today.replace(day=1)
    previous_day = first_day_of_month - timedelta(days=1)
    return previous_day.year, previous_day.month


def get_next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1
    return year, month + 1


def get_previous_month_bounds(today: date | None = None) -> tuple[date, date]:
    year, month = get_previous_month(today)
    month_start = date(year, month, 1)
    next_year, next_month = get_next_month(year, month)
    next_month_start = date(next_year, next_month, 1)
    return month_start, next_month_start - timedelta(days=1)


def parse_previous_month_date(raw_value: str, today: date | None = None) -> date:
    value = (raw_value or "").strip()
    if not value:
        raise ValueError("Дата не заполнена.")

    parsed = None
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(value, fmt).date()
            break
        except ValueError:
            continue

    if parsed is None:
        raise ValueError("Введите дату в формате ДД.ММ.ГГГГ.")

    month_start, month_end = get_previous_month_bounds(today)
    if not month_start <= parsed <= month_end:
        raise ValueError(
            f"Можно указать только дату из прошлого месяца: "
            f"с {month_start:%d.%m.%Y} по {month_end:%d.%m.%Y}."
        )
    return parsed


def _ensure_monthly_close_runs_table(cursor) -> None:
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS monthly_close_runs (
            year INT NOT NULL,
            month INT NOT NULL,
            status TEXT NOT NULL,
            trigger_source TEXT NULL,
            started_at TIMESTAMP NOT NULL DEFAULT NOW(),
            completed_at TIMESTAMP NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            last_error TEXT NULL,
            PRIMARY KEY (year, month)
        )
        """
    )


def _month_lock_key(year: int, month: int) -> int:
    return year * 100 + month


def _month_payload(year: int, month: int) -> dict:
    next_year, next_month = get_next_month(year, month)
    return {
        "archived_month": {
            "year": year,
            "month": month,
            "sheet_title": f"{year}-{month:02d}",
        },
        "current_month": {
            "year": next_year,
            "month": next_month,
            "sheet_title": GOOGLE_SHEETS_CURRENT_SHEET,
        },
    }


def close_month_tracked(year: int, month: int, trigger_source: str = "manual") -> dict:
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_monthly_close_runs_table(cursor)

    lock_key = _month_lock_key(year, month)
    cursor.execute("SELECT pg_try_advisory_lock(%s)", (lock_key,))
    lock_acquired = bool(cursor.fetchone()[0])
    if not lock_acquired:
        conn.close()
        return {"status": "busy", **_month_payload(year, month)}

    try:
        cursor.execute(
            """
            SELECT status
            FROM monthly_close_runs
            WHERE year = %s
              AND month = %s
            """,
            (year, month),
        )
        existing = cursor.fetchone()
        if existing and existing[0] == "completed":
            return {"status": "already_closed", **_month_payload(year, month)}

        cursor.execute(
            """
            INSERT INTO monthly_close_runs (
                year,
                month,
                status,
                trigger_source,
                started_at,
                completed_at,
                updated_at,
                last_error
            )
            VALUES (%s, %s, 'running', %s, NOW(), NULL, NOW(), NULL)
            ON CONFLICT (year, month)
            DO UPDATE SET
                status = 'running',
                trigger_source = EXCLUDED.trigger_source,
                started_at = NOW(),
                completed_at = NULL,
                updated_at = NOW(),
                last_error = NULL
            """,
            (year, month, trigger_source),
        )
        conn.commit()

        try:
            result = close_month(year, month)
        except Exception as exc:
            cursor.execute(
                """
                UPDATE monthly_close_runs
                SET status = 'failed',
                    updated_at = NOW(),
                    last_error = %s
                WHERE year = %s
                  AND month = %s
                """,
                (str(exc), year, month),
            )
            conn.commit()
            raise

        cursor.execute(
            """
            UPDATE monthly_close_runs
            SET status = 'completed',
                completed_at = NOW(),
                updated_at = NOW(),
                last_error = NULL
            WHERE year = %s
              AND month = %s
            """,
            (year, month),
        )
        conn.commit()
        return {"status": "closed", **result}
    finally:
        try:
            cursor.execute("SELECT pg_advisory_unlock(%s)", (lock_key,))
            conn.commit()
        except Exception:
            pass
        conn.close()


def ensure_previous_month_closed(trigger_source: str = "auto", today: date | None = None) -> dict:
    year, month = get_previous_month(today)
    return close_month_tracked(year, month, trigger_source=trigger_source)


async def run_month_close_scheduler(trigger_source: str, interval_seconds: int = AUTO_CLOSE_CHECK_INTERVAL_SECONDS):
    while True:
        try:
            result = await asyncio.to_thread(
                ensure_previous_month_closed,
                trigger_source,
            )
            if result["status"] == "closed":
                archived = result["archived_month"]
                current = result["current_month"]
                logger.info(
                    "Auto-closed month %02d.%04d; archive=%s current=%s",
                    archived["month"],
                    archived["year"],
                    archived["sheet_title"],
                    current["sheet_title"],
                )
        except Exception:
            logger.exception("Automatic month close check failed")

        await asyncio.sleep(interval_seconds)
