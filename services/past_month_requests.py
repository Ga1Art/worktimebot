"""Durable moderation of historical entries and retryable archive updates."""
import asyncio
import logging
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from services.db import (
    get_connection, _ensure_expense_receipt_columns, _ensure_work_logs_project_link,
)

logger = logging.getLogger(__name__)
FIELDS = ('id', 'worker_id', 'full_name', 'request_date', 'entry_type', 'project_id',
          'project_name', 'hours', 'amount', 'description', 'source_platform',
          'source_peer_id', 'status', 'archive_sync_pending')
SELECT_FIELDS = ', '.join(FIELDS)


def ensure_requests_table(cursor):
    cursor.execute("""CREATE TABLE IF NOT EXISTS past_month_requests (
        id SERIAL PRIMARY KEY,
        submission_key TEXT NOT NULL UNIQUE,
        worker_id INT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
        full_name TEXT NOT NULL,
        request_date DATE NOT NULL,
        entry_type TEXT NOT NULL CHECK (entry_type IN ('shift', 'install', 'expense')),
        project_id INT REFERENCES active_projects(id),
        project_name TEXT,
        hours NUMERIC,
        amount NUMERIC,
        description TEXT,
        source_platform TEXT NOT NULL,
        source_peer_id BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
        archive_sync_pending BOOLEAN NOT NULL DEFAULT FALSE,
        work_log_id INT REFERENCES work_logs(id) ON DELETE SET NULL,
        expense_id INT REFERENCES expenses(id) ON DELETE SET NULL,
        decided_by TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        decided_at TIMESTAMPTZ
    )""")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_past_requests_pending ON past_month_requests(status, archive_sync_pending)")


def _positive_number(value, label):
    try:
        result = Decimal(str(value).replace(',', '.'))
    except InvalidOperation:
        raise ValueError(f'{label}: введите положительное число.') from None
    if not result.is_finite() or result <= 0:
        raise ValueError(f'{label}: введите положительное число.')
    return result


def create_request(form, worker_id, full_name, submission_key, source_platform, source_peer_id):
    requested = date.fromisoformat(form['request_date'])
    previous_end = date.today().replace(day=1) - timedelta(days=1)
    if (requested.year, requested.month) != (previous_end.year, previous_end.month):
        raise ValueError('Можно отправить заявку только за прошлый месяц.')
    entry_type = form.get('entry_type')
    if entry_type not in {'shift', 'install', 'expense'}:
        raise ValueError('Неизвестный тип записи.')
    hours = _positive_number(form.get('hours'), 'Часы') if entry_type != 'expense' else None
    amount = _positive_number(form.get('amount'), 'Сумма') if entry_type == 'expense' else None
    description = (form.get('expense_type') or '').strip() if entry_type == 'expense' else None
    if entry_type == 'expense' and not description:
        raise ValueError('Укажите описание расхода.')
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _ensure_expense_receipt_columns(cursor)
        ensure_requests_table(cursor)
        project_id, project_name = None, None
        if entry_type != 'shift':
            cursor.execute('SELECT id, name FROM active_projects WHERE id = %s', (form.get('project_id'),))
            project = cursor.fetchone()
            if not project:
                raise ValueError('Выберите проект из справочника.')
            project_id, project_name = project
        cursor.execute("""INSERT INTO past_month_requests
            (submission_key, worker_id, full_name, request_date, entry_type, project_id,
             project_name, hours, amount, description, source_platform, source_peer_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (submission_key) DO UPDATE SET submission_key = EXCLUDED.submission_key
            RETURNING id""", (submission_key, worker_id, full_name, requested, entry_type,
                                  project_id, project_name, hours, amount, description, source_platform, source_peer_id))
        request_id = cursor.fetchone()[0]
        cursor.execute(f'SELECT {SELECT_FIELDS} FROM past_month_requests WHERE id = %s', (request_id,))
        request = dict(zip(FIELDS, cursor.fetchone()))
        conn.commit()
        return request
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_pending_requests():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _ensure_expense_receipt_columns(cursor)
        ensure_requests_table(cursor)
        cursor.execute(f"SELECT {SELECT_FIELDS} FROM past_month_requests WHERE status = 'pending' ORDER BY id")
        return [dict(zip(FIELDS, row)) for row in cursor.fetchall()]
    finally:
        conn.close()


def decide_request(request_id, approve, decided_by):
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _ensure_expense_receipt_columns(cursor)
        _ensure_work_logs_project_link(cursor)
        ensure_requests_table(cursor)
        # Release schema locks before waiting for the monthly archive lock.
        conn.commit()
        cursor.execute('SELECT request_date FROM past_month_requests WHERE id = %s', (request_id,))
        existing = cursor.fetchone()
        if not existing:
            raise ValueError('Заявка не найдена.')
        # Same lock as monthly closing/archiving; acquire it before locking the request row.
        cursor.execute('SELECT pg_advisory_xact_lock(%s)', (existing[0].year * 100 + existing[0].month,))
        cursor.execute(f'SELECT {SELECT_FIELDS} FROM past_month_requests WHERE id = %s FOR UPDATE', (request_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError('Заявка не найдена.')
        request = dict(zip(FIELDS, row))
        if request['status'] != 'pending':
            conn.commit()
            return {'changed': False, 'request': request}
        work_log_id, expense_id = None, None
        if approve:
            if request['entry_type'] == 'expense':
                cursor.execute("""INSERT INTO expenses
                    (worker_id, expense_date, amount, description, project_id, status, source_platform, source_peer_id)
                    VALUES (%s, %s, %s, %s, %s, 'approved', %s, %s) RETURNING id""",
                    (request['worker_id'], request['request_date'], request['amount'], request['description'],
                     request['project_id'], request['source_platform'], request['source_peer_id']))
                expense_id = cursor.fetchone()[0]
            else:
                cursor.execute("""INSERT INTO work_logs
                    (worker_id, work_date, work_type, project_id, project_name, hours)
                    VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                    (request['worker_id'], request['request_date'], request['entry_type'], request['project_id'],
                     request['project_name'], request['hours']))
                work_log_id = cursor.fetchone()[0]
        status = 'approved' if approve else 'rejected'
        cursor.execute("""UPDATE past_month_requests SET status = %s, archive_sync_pending = %s,
            work_log_id = %s, expense_id = %s, decided_by = %s, decided_at = NOW() WHERE id = %s""",
            (status, approve, work_log_id, expense_id, decided_by, request_id))
        conn.commit()
        request.update(status=status, archive_sync_pending=approve)
        return {'changed': True, 'request': request}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_request_archive(year, month):
    from services.google_sheets import archive_monthly_report
    conn = get_connection()
    cursor = conn.cursor()
    lock_key = year * 100 + month
    locked = False
    try:
        cursor.execute('SELECT pg_try_advisory_lock(%s)', (lock_key,))
        locked = bool(cursor.fetchone()[0])
        if not locked:
            return False
        ensure_requests_table(cursor)
        cursor.execute("""SELECT id FROM past_month_requests WHERE status = 'approved'
            AND archive_sync_pending = TRUE AND EXTRACT(YEAR FROM request_date) = %s
            AND EXTRACT(MONTH FROM request_date) = %s""", (year, month))
        request_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()
        if not request_ids:
            return True
        archive_monthly_report(year, month)
        cursor.execute('UPDATE past_month_requests SET archive_sync_pending = FALSE WHERE id = ANY(%s)', (request_ids,))
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        if locked:
            try:
                cursor.execute('SELECT pg_advisory_unlock(%s)', (lock_key,))
                conn.commit()
            except Exception:
                # Prevent a session lock from surviving return to the pool.
                conn._connection.close() if hasattr(conn, '_connection') else conn.close()
        conn.close()


def retry_pending_archives():
    conn = get_connection()
    try:
        cursor = conn.cursor()
        _ensure_expense_receipt_columns(cursor)
        ensure_requests_table(cursor)
        cursor.execute("""SELECT DISTINCT EXTRACT(YEAR FROM request_date)::INT,
            EXTRACT(MONTH FROM request_date)::INT FROM past_month_requests
            WHERE status = 'approved' AND archive_sync_pending = TRUE""")
        months = cursor.fetchall()
        conn.commit()
    finally:
        conn.close()
    for year, month in months:
        try:
            sync_request_archive(year, month)
        except Exception:
            logger.exception('Failed to retry historical archive %s-%s', year, month)


async def run_past_request_archive_scheduler():
    while True:
        try:
            await asyncio.to_thread(retry_pending_archives)
        except Exception:
            logger.exception('Historical archive retry failed')
        await asyncio.sleep(60)
