import secrets
import json
import string
import logging
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path

import psycopg2
from psycopg2.pool import ThreadedConnectionPool

from config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER


LINK_CODE_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
LINK_CODE_LENGTH = 8
LINK_CODE_TTL_MINUTES = 10
logger = logging.getLogger(__name__)
DB_POOL_MIN_CONNECTIONS = 1
DB_POOL_MAX_CONNECTIONS = 12
_db_pool = None


class _PooledConnection:
    def __init__(self, pool, connection):
        self._pool = pool
        self._connection = connection
        self._returned = False

    def __getattr__(self, item):
        return getattr(self._connection, item)

    def close(self):
        if self._returned:
            return

        try:
            if not self._connection.closed:
                self._connection.rollback()
        except Exception:
            pass

        try:
            self._pool.putconn(self._connection, close=bool(self._connection.closed))
        finally:
            self._returned = True


def _get_connection_pool():
    global _db_pool
    if _db_pool is None:
        _db_pool = ThreadedConnectionPool(
            DB_POOL_MIN_CONNECTIONS,
            DB_POOL_MAX_CONNECTIONS,
            host=DB_HOST,
            database=DB_NAME,
            user=DB_USER,
            password=DB_PASSWORD,
            port=DB_PORT,
            sslmode="require",
        )
    return _db_pool


def get_connection():
    pool = _get_connection_pool()
    return _PooledConnection(pool, pool.getconn())


def _ensure_account_link_codes_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS account_link_codes (
            id SERIAL PRIMARY KEY,
            worker_id INT NOT NULL REFERENCES workers(id) ON DELETE CASCADE,
            target_platform TEXT NOT NULL CHECK (target_platform IN ('vk', 'telegram')),
            code TEXT NOT NULL UNIQUE,
            expires_at TIMESTAMP NOT NULL,
            used_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )

    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_account_link_codes_lookup
        ON account_link_codes (code, target_platform)
        """
    )


def _ensure_expense_receipt_columns(cursor):
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS receipt_path TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS receipt_original_name TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS receipt_media_type TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS receipt_source_file_id TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS receipt_source_url TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS source_platform TEXT
        """
    )
    cursor.execute(
        """
        ALTER TABLE expenses
        ADD COLUMN IF NOT EXISTS source_peer_id BIGINT
        """
    )


def _ensure_projects_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS active_projects (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            active BOOLEAN NOT NULL DEFAULT TRUE,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_active_projects_active_name
        ON active_projects (active, name)
        """
    )


def _ensure_work_logs_project_link(cursor):
    cursor.execute(
        """
        ALTER TABLE work_logs
        ADD COLUMN IF NOT EXISTS project_id INT NULL REFERENCES active_projects(id) ON DELETE SET NULL
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_work_logs_project_id
        ON work_logs (project_id)
        """
    )


def _ensure_conversation_states_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_states (
            platform TEXT NOT NULL,
            user_key TEXT NOT NULL,
            state TEXT NULL,
            data_json TEXT NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, user_key)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_states_updated_at
        ON conversation_states (updated_at)
        """
    )


def _ensure_pending_inbound_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_inbound_messages (
            platform TEXT NOT NULL,
            user_key TEXT NOT NULL,
            message_text TEXT NULL,
            state TEXT NULL,
            state_data_json TEXT NOT NULL DEFAULT '{}',
            has_media BOOLEAN NOT NULL DEFAULT FALSE,
            error_text TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, user_key)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_pending_inbound_updated_at
        ON pending_inbound_messages (updated_at)
        """
    )


def _ensure_conversation_activity_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_activity (
            platform TEXT NOT NULL,
            user_key TEXT NOT NULL,
            last_inbound_text TEXT NULL,
            last_inbound_state TEXT NULL,
            last_inbound_state_data_json TEXT NOT NULL DEFAULT '{}',
            last_inbound_has_media BOOLEAN NOT NULL DEFAULT FALSE,
            last_inbound_error_text TEXT NULL,
            last_inbound_at TIMESTAMP NULL,
            last_outbound_at TIMESTAMP NULL,
            updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
            PRIMARY KEY (platform, user_key)
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_conversation_activity_last_inbound_at
        ON conversation_activity (last_inbound_at)
        """
    )


def _ensure_dedup_keys_table(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS dedup_keys (
            dedup_key TEXT PRIMARY KEY,
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT NOW()
        )
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_dedup_keys_expires_at
        ON dedup_keys (expires_at)
        """
    )


def _generate_link_code():
    return "".join(secrets.choice(LINK_CODE_ALPHABET) for _ in range(LINK_CODE_LENGTH))


def claim_dedup_key(dedup_key: str, ttl_seconds: int):
    if not dedup_key:
        return True

    conn = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        _ensure_dedup_keys_table(cursor)

        cursor.execute(
            """
            DELETE FROM dedup_keys
            WHERE expires_at <= NOW()
            """
        )

        cursor.execute(
            """
            INSERT INTO dedup_keys (dedup_key, expires_at)
            VALUES (%s, NOW() + (%s * INTERVAL '1 second'))
            ON CONFLICT (dedup_key) DO NOTHING
            RETURNING dedup_key
            """,
            (dedup_key, ttl_seconds),
        )

        inserted = cursor.fetchone() is not None
        conn.commit()
        return inserted
    except Exception as exc:
        logger.exception("Dedup storage unavailable for key %s: %s", dedup_key, exc)
        return True
    finally:
        if conn is not None:
            conn.close()


def _choose_better_full_name(primary_name, secondary_name):
    primary = (primary_name or "").strip()
    secondary = (secondary_name or "").strip()

    if not primary:
        return secondary
    if not secondary:
        return primary

    primary_tokens = len(primary.split())
    secondary_tokens = len(secondary.split())

    if secondary_tokens > primary_tokens:
        return secondary
    if secondary_tokens == primary_tokens and len(secondary) > len(primary):
        return secondary
    return primary


def save_conversation_state(platform: str, user_key: str, state: str | None, data: dict | None = None):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_states_table(cursor)

    payload = json.dumps(data or {}, ensure_ascii=False)
    cursor.execute(
        """
        INSERT INTO conversation_states (platform, user_key, state, data_json, updated_at)
        VALUES (%s, %s, %s, %s, NOW())
        ON CONFLICT (platform, user_key)
        DO UPDATE SET
            state = EXCLUDED.state,
            data_json = EXCLUDED.data_json,
            updated_at = NOW()
        """,
        (platform, str(user_key), state, payload),
    )

    conn.commit()
    conn.close()


def load_conversation_state(platform: str, user_key: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_states_table(cursor)

    cursor.execute(
        """
        SELECT state, data_json
        FROM conversation_states
        WHERE platform = %s
          AND user_key = %s
        """,
        (platform, str(user_key)),
    )

    row = cursor.fetchone()
    conn.close()
    if not row:
        return None

    state, data_json = row
    try:
        data = json.loads(data_json or "{}")
    except Exception:
        data = {}

    return {"state": state, "data": data}


def clear_conversation_state(platform: str, user_key: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_states_table(cursor)

    cursor.execute(
        """
        DELETE FROM conversation_states
        WHERE platform = %s
          AND user_key = %s
        """,
        (platform, str(user_key)),
    )

    conn.commit()
    conn.close()


def save_pending_inbound_message(
    platform: str,
    user_key: str,
    message_text: str | None,
    state: str | None,
    state_data: dict | None = None,
    *,
    has_media: bool = False,
    error_text: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_pending_inbound_table(cursor)

    cursor.execute(
        """
        INSERT INTO pending_inbound_messages (
            platform,
            user_key,
            message_text,
            state,
            state_data_json,
            has_media,
            error_text,
            created_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (platform, user_key)
        DO UPDATE SET
            message_text = EXCLUDED.message_text,
            state = EXCLUDED.state,
            state_data_json = EXCLUDED.state_data_json,
            has_media = EXCLUDED.has_media,
            error_text = EXCLUDED.error_text,
            updated_at = NOW()
        """,
        (
            platform,
            str(user_key),
            message_text,
            state,
            json.dumps(state_data or {}, ensure_ascii=False),
            bool(has_media),
            error_text,
        ),
    )

    conn.commit()
    conn.close()


def save_conversation_inbound(
    platform: str,
    user_key: str,
    message_text: str | None,
    state: str | None,
    state_data: dict | None = None,
    *,
    has_media: bool = False,
    error_text: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_activity_table(cursor)

    cursor.execute(
        """
        INSERT INTO conversation_activity (
            platform,
            user_key,
            last_inbound_text,
            last_inbound_state,
            last_inbound_state_data_json,
            last_inbound_has_media,
            last_inbound_error_text,
            last_inbound_at,
            updated_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
        ON CONFLICT (platform, user_key)
        DO UPDATE SET
            last_inbound_text = EXCLUDED.last_inbound_text,
            last_inbound_state = EXCLUDED.last_inbound_state,
            last_inbound_state_data_json = EXCLUDED.last_inbound_state_data_json,
            last_inbound_has_media = EXCLUDED.last_inbound_has_media,
            last_inbound_error_text = EXCLUDED.last_inbound_error_text,
            last_inbound_at = NOW(),
            updated_at = NOW()
        """,
        (
            platform,
            str(user_key),
            message_text,
            state,
            json.dumps(state_data or {}, ensure_ascii=False),
            bool(has_media),
            error_text,
        ),
    )

    conn.commit()
    conn.close()


def mark_conversation_outbound(platform: str, user_key: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_activity_table(cursor)

    cursor.execute(
        """
        INSERT INTO conversation_activity (platform, user_key, last_outbound_at, updated_at)
        VALUES (%s, %s, NOW(), NOW())
        ON CONFLICT (platform, user_key)
        DO UPDATE SET
            last_outbound_at = NOW(),
            updated_at = NOW()
        """,
        (platform, str(user_key)),
    )

    conn.commit()
    conn.close()


def list_conversations_needing_reply(platform: str | None = None):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_activity_table(cursor)

    where_sql = ""
    params: tuple = ()
    if platform:
        where_sql = "AND platform = %s"
        params = (platform,)

    cursor.execute(
        f"""
        SELECT
            platform,
            user_key,
            last_inbound_text,
            last_inbound_state,
            last_inbound_state_data_json,
            last_inbound_has_media,
            last_inbound_error_text,
            last_inbound_at,
            last_outbound_at
        FROM conversation_activity
        WHERE last_inbound_at IS NOT NULL
          AND (last_outbound_at IS NULL OR last_inbound_at > last_outbound_at)
          {where_sql}
        ORDER BY last_inbound_at ASC
        """,
        params,
    )

    rows = cursor.fetchall()
    conn.close()
    result = []
    for (
        platform_name,
        user_key,
        last_inbound_text,
        last_inbound_state,
        last_inbound_state_data_json,
        last_inbound_has_media,
        last_inbound_error_text,
        last_inbound_at,
        last_outbound_at,
    ) in rows:
        try:
            state_data = json.loads(last_inbound_state_data_json or "{}")
        except Exception:
            state_data = {}
        result.append(
            {
                "platform": platform_name,
                "user_key": user_key,
                "message_text": last_inbound_text,
                "state": last_inbound_state,
                "state_data": state_data,
                "has_media": bool(last_inbound_has_media),
                "error_text": last_inbound_error_text,
                "last_inbound_at": last_inbound_at,
                "last_outbound_at": last_outbound_at,
            }
        )
    return result


def list_active_conversation_states(platform: str | None = None):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_conversation_states_table(cursor)

    if platform:
        cursor.execute(
            """
            SELECT platform, user_key, state, data_json, updated_at
            FROM conversation_states
            WHERE platform = %s
              AND state IS NOT NULL
            ORDER BY updated_at ASC
            """,
            (platform,),
        )
    else:
        cursor.execute(
            """
            SELECT platform, user_key, state, data_json, updated_at
            FROM conversation_states
            WHERE state IS NOT NULL
            ORDER BY updated_at ASC
            """
        )

    rows = cursor.fetchall()
    conn.close()
    result = []
    for platform_name, user_key, state, data_json, updated_at in rows:
        try:
            data = json.loads(data_json or "{}")
        except Exception:
            data = {}
        result.append(
            {
                "platform": platform_name,
                "user_key": user_key,
                "state": state,
                "data": data,
                "updated_at": updated_at,
            }
        )
    return result


def get_pending_inbound_message(platform: str, user_key: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_pending_inbound_table(cursor)

    cursor.execute(
        """
        SELECT message_text, state, state_data_json, has_media, error_text, updated_at
        FROM pending_inbound_messages
        WHERE platform = %s
          AND user_key = %s
        """,
        (platform, str(user_key)),
    )

    row = cursor.fetchone()
    conn.close()
    if not row:
        return None

    message_text, state, state_data_json, has_media, error_text, updated_at = row
    try:
        state_data = json.loads(state_data_json or "{}")
    except Exception:
        state_data = {}

    return {
        "message_text": message_text,
        "state": state,
        "state_data": state_data,
        "has_media": bool(has_media),
        "error_text": error_text,
        "updated_at": updated_at,
    }


def clear_pending_inbound_message(platform: str, user_key: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_pending_inbound_table(cursor)

    cursor.execute(
        """
        DELETE FROM pending_inbound_messages
        WHERE platform = %s
          AND user_key = %s
        """,
        (platform, str(user_key)),
    )

    conn.commit()
    conn.close()


def list_pending_inbound_messages(platform: str | None = None):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_pending_inbound_table(cursor)

    if platform:
        cursor.execute(
            """
            SELECT platform, user_key, message_text, state, state_data_json, has_media, error_text, updated_at
            FROM pending_inbound_messages
            WHERE platform = %s
            ORDER BY updated_at ASC
            """,
            (platform,),
        )
    else:
        cursor.execute(
            """
            SELECT platform, user_key, message_text, state, state_data_json, has_media, error_text, updated_at
            FROM pending_inbound_messages
            ORDER BY updated_at ASC
            """
        )

    rows = cursor.fetchall()
    conn.close()

    result = []
    for platform_name, user_key, message_text, state, state_data_json, has_media, error_text, updated_at in rows:
        try:
            state_data = json.loads(state_data_json or "{}")
        except Exception:
            state_data = {}

        result.append(
            {
                "platform": platform_name,
                "user_key": user_key,
                "message_text": message_text,
                "state": state,
                "state_data": state_data,
                "has_media": bool(has_media),
                "error_text": error_text,
                "updated_at": updated_at,
            }
        )

    return result


def get_worker(chat_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, registration_status, is_approved, active
        FROM workers
        WHERE chat_id = %s
        """,
        (chat_id,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def get_worker_by_id(worker_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, full_name, chat_id, registration_status, is_approved, active, is_admin
        FROM workers
        WHERE id = %s
        """,
        (worker_id,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def get_worker_rates(worker_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT work_type, MAX(rate_per_hour)
        FROM rates
        WHERE worker_id = %s
        GROUP BY work_type
        """,
        (worker_id,),
    )

    rows = cursor.fetchall()
    conn.close()
    return {work_type: float(rate or 0) for work_type, rate in rows}


def _ensure_worker_default_rates(cursor, worker_id: int):
    for work_type in ("shift", "install"):
        cursor.execute(
            """
            SELECT 1
            FROM rates
            WHERE worker_id = %s
              AND work_type = %s
            LIMIT 1
            """,
            (worker_id, work_type),
        )
        if cursor.fetchone():
            continue

        cursor.execute(
            """
            INSERT INTO rates (worker_id, work_type, rate_per_hour)
            VALUES (%s, %s, 0)
            """,
            (worker_id, work_type),
        )


def ensure_default_rates_for_active_workers():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id
        FROM workers
        WHERE active = true
          AND is_approved = true
          AND registration_status = 'approved'
        """
    )
    worker_ids = [row[0] for row in cursor.fetchall()]
    for worker_id in worker_ids:
        _ensure_worker_default_rates(cursor, worker_id)

    conn.commit()
    conn.close()


def set_worker_rate(worker_id: int, work_type: str, rate_per_hour: float):
    if work_type not in {"shift", "install"}:
        raise ValueError("Unsupported work type")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        DELETE FROM rates
        WHERE worker_id = %s
          AND work_type = %s
        """,
        (worker_id, work_type),
    )
    cursor.execute(
        """
        INSERT INTO rates (worker_id, work_type, rate_per_hour)
        VALUES (%s, %s, %s)
        """,
        (worker_id, work_type, rate_per_hour),
    )

    conn.commit()
    conn.close()


def replace_rates_from_rows(rows: list[dict]):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM rates")
    for row in rows:
        cursor.execute(
            """
            INSERT INTO rates (worker_id, work_type, rate_per_hour)
            VALUES (%s, %s, %s)
            """,
            (row["worker_id"], row["work_type"], row["rate_per_hour"]),
        )

    conn.commit()
    conn.close()


def replace_bonuses_from_rows(rows: list[dict]):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM bonuses")
    for row in rows:
        cursor.execute(
            """
            INSERT INTO bonuses (worker_id, bonus_date, amount, description)
            VALUES (%s, %s, %s, %s)
            """,
            (row["worker_id"], row["bonus_date"], row["amount"], row["description"]),
        )

    conn.commit()
    conn.close()


def replace_penalties_from_rows(rows: list[dict]):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("DELETE FROM penalties")
    for row in rows:
        cursor.execute(
            """
            INSERT INTO penalties (worker_id, penalty_date, amount, description)
            VALUES (%s, %s, %s, %s)
            """,
            (row["worker_id"], row["penalty_date"], row["amount"], row["description"]),
        )

    conn.commit()
    conn.close()


def get_worker_contacts_by_id(worker_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, full_name, chat_id, vk_id, registration_status, is_approved, active, is_admin
        FROM workers
        WHERE id = %s
        """,
        (worker_id,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def create_worker(full_name, chat_id, vk_id=None):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO workers (full_name, chat_id, vk_id, registration_status)
        VALUES (%s, %s, %s, 'pending')
        RETURNING id
        """,
        (full_name, chat_id, vk_id),
    )

    worker_id = cursor.fetchone()[0]
    _ensure_worker_default_rates(cursor, worker_id)
    conn.commit()
    conn.close()

    return worker_id


def get_worker_by_vk_id(vk_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, registration_status, is_approved, active, full_name
        FROM workers
        WHERE vk_id = %s
        """,
        (vk_id,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def get_worker_by_name(full_name):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, registration_status, is_approved, active, chat_id, vk_id
        FROM workers
        WHERE LOWER(full_name) = LOWER(%s)
        LIMIT 1
        """,
        (full_name,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def link_vk_to_existing_worker(worker_id, vk_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE workers
        SET vk_id = %s
        WHERE id = %s
        """,
        (vk_id, worker_id),
    )

    conn.commit()
    conn.close()


def generate_account_link_code(worker_id: int, target_platform: str, ttl_minutes: int = LINK_CODE_TTL_MINUTES):
    if target_platform not in {"vk", "telegram"}:
        raise ValueError("Unsupported target platform")

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_account_link_codes_table(cursor)

    cursor.execute("SELECT id FROM workers WHERE id = %s", (worker_id,))
    if not cursor.fetchone():
        conn.close()
        raise ValueError("Worker not found")

    cursor.execute(
        """
        DELETE FROM account_link_codes
        WHERE worker_id = %s
          AND target_platform = %s
          AND used_at IS NULL
        """,
        (worker_id, target_platform),
    )

    code = None
    expires_at = datetime.utcnow() + timedelta(minutes=ttl_minutes)
    for _ in range(10):
        candidate = _generate_link_code()
        cursor.execute(
            "SELECT 1 FROM account_link_codes WHERE code = %s",
            (candidate,),
        )
        if not cursor.fetchone():
            code = candidate
            break

    if code is None:
        conn.close()
        raise ValueError("Could not generate a unique code")

    cursor.execute(
        """
        INSERT INTO account_link_codes (worker_id, target_platform, code, expires_at)
        VALUES (%s, %s, %s, %s)
        """,
        (worker_id, target_platform, code, expires_at),
    )

    conn.commit()
    conn.close()
    return {
        "code": code,
        "expires_at": expires_at,
        "ttl_minutes": ttl_minutes,
    }


def consume_account_link_code(code: str, target_platform: str):
    normalized_code = (code or "").strip().upper()
    if not normalized_code:
        raise ValueError("Код привязки не указан.")

    if target_platform not in {"vk", "telegram"}:
        raise ValueError("Unsupported target platform")

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_account_link_codes_table(cursor)

    cursor.execute(
        """
        SELECT id, worker_id, expires_at, used_at
        FROM account_link_codes
        WHERE code = %s
          AND target_platform = %s
        """,
        (normalized_code, target_platform),
    )
    code_row = cursor.fetchone()

    if not code_row:
        conn.close()
        raise ValueError("Код не найден. Проверьте ввод и попробуйте ещё раз.")

    code_id, worker_id, expires_at, used_at = code_row
    if used_at is not None:
        conn.close()
        raise ValueError("Этот код уже использован.")

    if expires_at < datetime.utcnow():
        conn.close()
        raise ValueError("Срок действия кода истёк. Сгенерируйте новый код в Telegram.")

    cursor.execute(
        """
        UPDATE account_link_codes
        SET used_at = NOW()
        WHERE id = %s
        """,
        (code_id,),
    )

    cursor.execute(
        """
        SELECT id, full_name, chat_id, vk_id, registration_status, is_approved, active, is_admin
        FROM workers
        WHERE id = %s
        """,
        (worker_id,),
    )
    worker = cursor.fetchone()

    conn.commit()
    conn.close()

    if not worker:
        raise ValueError("Сотрудник для этого кода не найден.")

    return worker


def is_admin(chat_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT is_admin
        FROM workers
        WHERE chat_id = %s
        """,
        (chat_id,),
    )

    result = cursor.fetchone()
    conn.close()

    return result and result[0] is True


def is_admin_by_vk_id(vk_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT is_admin
        FROM workers
        WHERE vk_id = %s
        """,
        (vk_id,),
    )

    result = cursor.fetchone()
    conn.close()

    return result and result[0] is True


def get_admin_vk_ids():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT vk_id
        FROM workers
        WHERE is_admin = true
          AND vk_id IS NOT NULL
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return [row[0] for row in rows]


def set_worker_admin(worker_id: int, is_admin_value: bool):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE workers
        SET is_admin = %s
        WHERE id = %s
        RETURNING id, full_name, chat_id, vk_id, is_admin
        """,
        (is_admin_value, worker_id),
    )

    result = cursor.fetchone()
    conn.commit()
    conn.close()
    return result


def delete_worker(worker_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_account_link_codes_table(cursor)
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        SELECT id, full_name, chat_id, vk_id, is_admin
        FROM workers
        WHERE id = %s
        """,
        (worker_id,),
    )
    worker = cursor.fetchone()
    if not worker:
        conn.close()
        return None

    cursor.execute(
        """
        SELECT receipt_path
        FROM expenses
        WHERE worker_id = %s
          AND receipt_path IS NOT NULL
        """,
        (worker_id,),
    )
    receipt_paths = [row[0] for row in cursor.fetchall() if row[0]]

    cursor.execute(
        """
        DELETE FROM workers
        WHERE id = %s
        """,
        (worker_id,),
    )

    conn.commit()
    conn.close()

    for raw_path in set(receipt_paths):
        try:
            path = Path(raw_path)
            if path.exists():
                path.unlink()
        except Exception:
            logger.exception("Failed to delete receipt file for worker %s: %s", worker_id, raw_path)

    return worker


def approve_worker(worker_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE workers
        SET registration_status = 'approved',
            is_approved = true
        WHERE id = %s
        """,
        (worker_id,),
    )
    _ensure_worker_default_rates(cursor, worker_id)

    conn.commit()
    conn.close()


def reject_worker(worker_id):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        UPDATE workers
        SET registration_status = 'rejected',
            is_approved = false
        WHERE id = %s
        """,
        (worker_id,),
    )

    conn.commit()
    conn.close()


def merge_workers(target_worker_id: int, source_worker_id: int):
    if target_worker_id == source_worker_id:
        raise ValueError("Нельзя объединить сотрудника с самим собой.")

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_account_link_codes_table(cursor)

    cursor.execute(
        """
        SELECT id, full_name, chat_id, vk_id, registration_status, is_approved, active, is_admin
        FROM workers
        WHERE id IN (%s, %s)
        ORDER BY id
        """,
        (target_worker_id, source_worker_id),
    )
    rows = cursor.fetchall()
    workers = {row[0]: row for row in rows}

    target = workers.get(target_worker_id)
    source = workers.get(source_worker_id)
    if not target:
        conn.close()
        raise ValueError("Основной сотрудник не найден.")
    if not source:
        conn.close()
        raise ValueError("Дублирующий сотрудник не найден.")

    _, target_name, target_chat_id, target_vk_id, target_status, target_approved, target_active, target_admin = target
    _, source_name, source_chat_id, source_vk_id, source_status, source_approved, source_active, source_admin = source

    if target_chat_id and source_chat_id and target_chat_id != source_chat_id:
        conn.close()
        raise ValueError("У обеих записей есть разные Telegram chat_id. Сначала проверьте, кого нужно оставить основным.")

    if target_vk_id and source_vk_id and target_vk_id != source_vk_id:
        conn.close()
        raise ValueError("У обеих записей есть разные VK ID. Сначала проверьте, кого нужно оставить основным.")

    merged_name = _choose_better_full_name(target_name, source_name)
    merged_chat_id = target_chat_id or source_chat_id
    merged_vk_id = target_vk_id or source_vk_id
    merged_is_admin = bool(target_admin or source_admin)
    merged_active = bool(target_active or source_active)
    merged_is_approved = bool(target_approved or source_approved)

    if merged_is_approved:
        merged_status = "approved"
    elif target_status == "pending" or source_status == "pending":
        merged_status = "pending"
    else:
        merged_status = "rejected"

    for table_name in ("work_logs", "expenses", "bonuses", "penalties", "rates"):
        cursor.execute(
            f"""
            UPDATE {table_name}
            SET worker_id = %s
            WHERE worker_id = %s
            """,
            (target_worker_id, source_worker_id),
        )

    cursor.execute(
        """
        DELETE FROM account_link_codes
        WHERE worker_id = %s
        """,
        (source_worker_id,),
    )

    cursor.execute(
        """
        UPDATE workers
        SET full_name = %s,
            chat_id = %s,
            vk_id = %s,
            registration_status = %s,
            is_approved = %s,
            active = %s,
            is_admin = %s
        WHERE id = %s
        """,
        (
            merged_name,
            merged_chat_id,
            merged_vk_id,
            merged_status,
            merged_is_approved,
            merged_active,
            merged_is_admin,
            target_worker_id,
        ),
    )

    cursor.execute(
        """
        DELETE FROM workers
        WHERE id = %s
        """,
        (source_worker_id,),
    )

    conn.commit()
    conn.close()

    return {
        "target_worker_id": target_worker_id,
        "source_worker_id": source_worker_id,
        "full_name": merged_name,
        "chat_id": merged_chat_id,
        "vk_id": merged_vk_id,
        "registration_status": merged_status,
    }


def get_pending_workers():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, full_name, chat_id
        FROM workers
        WHERE registration_status = 'pending'
        ORDER BY id
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_active_workers():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT id, full_name
        FROM workers
        WHERE active = true
          AND is_approved = true
          AND registration_status = 'approved'
        ORDER BY full_name, id
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_active_projects():
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)

    cursor.execute(
        """
        SELECT id, name
        FROM active_projects
        WHERE active = true
        ORDER BY LOWER(name), id
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_active_projects_with_stats():
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)
    _ensure_work_logs_project_link(cursor)

    cursor.execute(
        """
        SELECT
            ap.id,
            ap.name,
            COUNT(DISTINCT wl.worker_id) AS participants_count,
            COALESCE(SUM(wl.hours), 0) AS total_hours
        FROM active_projects ap
        LEFT JOIN work_logs wl
            ON wl.work_type = 'install'
           AND (
                wl.project_id = ap.id
                OR (
                    wl.project_id IS NULL
                    AND LOWER(TRIM(COALESCE(wl.project_name, ''))) = LOWER(TRIM(ap.name))
                )
           )
        WHERE ap.active = true
        GROUP BY ap.id, ap.name
        ORDER BY LOWER(ap.name), ap.id
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_project_by_id(project_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)

    cursor.execute(
        """
        SELECT id, name, active
        FROM active_projects
        WHERE id = %s
        """,
        (project_id,),
    )

    row = cursor.fetchone()
    conn.close()
    return row


def get_active_project_by_name(name: str):
    normalized_name = (name or "").strip()
    if not normalized_name:
        return None

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)

    cursor.execute(
        """
        SELECT id, name
        FROM active_projects
        WHERE active = true
          AND LOWER(TRIM(name)) = LOWER(TRIM(%s))
        LIMIT 1
        """,
        (normalized_name,),
    )

    row = cursor.fetchone()
    conn.close()
    return row


def backfill_project_links(project_id: int, project_name: str):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)
    _ensure_work_logs_project_link(cursor)

    cursor.execute(
        """
        UPDATE work_logs
        SET project_id = %s
        WHERE work_type = 'install'
          AND project_id IS NULL
          AND LOWER(TRIM(COALESCE(project_name, ''))) = LOWER(TRIM(%s))
        """,
        (project_id, project_name),
    )
    updated_count = cursor.rowcount
    conn.commit()
    conn.close()
    return updated_count


def add_project(name: str):
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ValueError("Название проекта не должно быть пустым.")

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)
    _ensure_work_logs_project_link(cursor)

    cursor.execute(
        """
        SELECT id, name, active
        FROM active_projects
        WHERE LOWER(name) = LOWER(%s)
        LIMIT 1
        """,
        (normalized_name,),
    )
    existing = cursor.fetchone()
    if existing:
        project_id, existing_name, is_active = existing
        if is_active:
            conn.close()
            raise ValueError(f"Проект «{existing_name}» уже есть в активном списке.")

        cursor.execute(
            """
            UPDATE active_projects
            SET name = %s,
                active = true
            WHERE id = %s
            RETURNING id, name
            """,
            (normalized_name, project_id),
        )
        result = cursor.fetchone()
        conn.commit()
        backfill_project_links(result[0], result[1])
        conn.close()
        return result

    cursor.execute(
        """
        INSERT INTO active_projects (name, active)
        VALUES (%s, true)
        RETURNING id, name
        """,
        (normalized_name,),
    )
    result = cursor.fetchone()
    conn.commit()
    backfill_project_links(result[0], result[1])
    conn.close()
    return result


def add_project(name: str, project_id: int | None = None):
    normalized_name = (name or "").strip()
    if not normalized_name:
        raise ValueError('Название проекта не должно быть пустым.')
    if project_id is not None and project_id <= 0:
        raise ValueError('ID проекта должен быть положительным числом.')

    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)
    _ensure_work_logs_project_link(cursor)

    if project_id is not None:
        cursor.execute(
            """
            SELECT id, name, active
            FROM active_projects
            WHERE id = %s
            LIMIT 1
            """,
            (project_id,),
        )
        existing_by_id = cursor.fetchone()
        if existing_by_id:
            existing_id, existing_name, existing_active = existing_by_id
            if existing_name.strip().lower() != normalized_name.lower():
                conn.close()
                raise ValueError(f'ID {project_id} уже занят проектом «{existing_name}».')
            if existing_active:
                conn.close()
                raise ValueError(f'Проект «{existing_name}» уже есть в активном списке.')

            cursor.execute(
                """
                UPDATE active_projects
                SET name = %s,
                    active = true
                WHERE id = %s
                RETURNING id, name
                """,
                (normalized_name, existing_id),
            )
            result = cursor.fetchone()
            conn.commit()
            backfill_project_links(result[0], result[1])
            conn.close()
            return result

    cursor.execute(
        """
        SELECT id, name, active
        FROM active_projects
        WHERE LOWER(name) = LOWER(%s)
        LIMIT 1
        """,
        (normalized_name,),
    )
    existing = cursor.fetchone()
    if existing:
        existing_project_id, existing_name, is_active = existing
        if project_id is not None and project_id != existing_project_id:
            conn.close()
            raise ValueError(f'Проект «{existing_name}» уже существует с ID {existing_project_id}.')
        if is_active:
            conn.close()
            raise ValueError(f'Проект «{existing_name}» уже есть в активном списке.')

        cursor.execute(
            """
            UPDATE active_projects
            SET name = %s,
                active = true
            WHERE id = %s
            RETURNING id, name
            """,
            (normalized_name, existing_project_id),
        )
        result = cursor.fetchone()
        conn.commit()
        backfill_project_links(result[0], result[1])
        conn.close()
        return result

    if project_id is None:
        cursor.execute(
            """
            INSERT INTO active_projects (name, active)
            VALUES (%s, true)
            RETURNING id, name
            """,
            (normalized_name,),
        )
    else:
        cursor.execute(
            """
            INSERT INTO active_projects (id, name, active)
            VALUES (%s, %s, true)
            RETURNING id, name
            """,
            (project_id, normalized_name),
        )
    result = cursor.fetchone()
    conn.commit()
    backfill_project_links(result[0], result[1])
    conn.close()
    return result


def deactivate_project(project_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_projects_table(cursor)

    cursor.execute(
        """
        UPDATE active_projects
        SET active = false
        WHERE id = %s
          AND active = true
        RETURNING id, name
        """,
        (project_id,),
    )
    result = cursor.fetchone()
    conn.commit()
    conn.close()
    return result


def get_active_workers_with_rates():
    ensure_default_rates_for_active_workers()
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT
            w.id,
            w.full_name,
            COALESCE(shift_rates.rate_per_hour, 0) AS shift_rate,
            COALESCE(install_rates.rate_per_hour, 0) AS install_rate
        FROM workers w
        LEFT JOIN (
            SELECT worker_id, MAX(rate_per_hour) AS rate_per_hour
            FROM rates
            WHERE work_type = 'shift'
            GROUP BY worker_id
        ) shift_rates ON shift_rates.worker_id = w.id
        LEFT JOIN (
            SELECT worker_id, MAX(rate_per_hour) AS rate_per_hour
            FROM rates
            WHERE work_type = 'install'
            GROUP BY worker_id
        ) install_rates ON install_rates.worker_id = w.id
        WHERE w.active = true
          AND w.is_approved = true
          AND w.registration_status = 'approved'
        ORDER BY w.full_name, w.id
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def add_bonus(worker_id: int, amount: float, description: str, bonus_date: date | None = None):
    conn = get_connection()
    cursor = conn.cursor()

    target_date = bonus_date or date.today()
    cursor.execute(
        """
        INSERT INTO bonuses (worker_id, bonus_date, amount, description)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (worker_id, target_date, amount, description),
    )
    bonus_id = cursor.fetchone()[0]

    conn.commit()
    conn.close()
    return bonus_id


def add_penalty(worker_id: int, amount: float, description: str, penalty_date: date | None = None):
    conn = get_connection()
    cursor = conn.cursor()

    target_date = penalty_date or date.today()
    cursor.execute(
        """
        INSERT INTO penalties (worker_id, penalty_date, amount, description)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (worker_id, target_date, amount, description),
    )
    penalty_id = cursor.fetchone()[0]

    conn.commit()
    conn.close()
    return penalty_id


def get_worker_bonuses(worker_id: int, year: int | None = None, month: int | None = None):
    conn = get_connection()
    cursor = conn.cursor()

    if year is None or month is None:
        today = date.today()
        year = today.year
        month = today.month

    cursor.execute(
        """
        SELECT bonus_date, amount, description
        FROM bonuses
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM bonus_date) = %s
          AND EXTRACT(MONTH FROM bonus_date) = %s
        ORDER BY bonus_date, id
        """,
        (worker_id, year, month),
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_worker_penalties(worker_id: int, year: int | None = None, month: int | None = None):
    conn = get_connection()
    cursor = conn.cursor()

    if year is None or month is None:
        today = date.today()
        year = today.year
        month = today.month

    cursor.execute(
        """
        SELECT penalty_date, amount, description
        FROM penalties
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM penalty_date) = %s
          AND EXTRACT(MONTH FROM penalty_date) = %s
        ORDER BY penalty_date, id
        """,
        (worker_id, year, month),
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_pending_expenses():
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        SELECT e.id, w.full_name, e.amount, e.description, e.expense_date
        FROM expenses e
        JOIN workers w ON e.worker_id = w.id
        WHERE e.status = 'pending'
        """
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_expense_by_id(expense_id):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        SELECT
            e.id,
            e.worker_id,
            w.full_name,
            w.chat_id,
            e.amount,
            e.description,
            e.expense_date,
            e.status,
            e.receipt_path,
            e.receipt_original_name,
            e.receipt_media_type,
            e.receipt_source_file_id,
            e.receipt_source_url,
            e.source_platform,
            e.source_peer_id
        FROM expenses e
        JOIN workers w ON e.worker_id = w.id
        WHERE e.id = %s
        """,
        (expense_id,),
    )

    result = cursor.fetchone()
    conn.close()
    return result


def update_expense_status(expense_id, status):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        UPDATE expenses
        SET status = %s
        WHERE id = %s
        """,
        (status, expense_id),
    )

    conn.commit()
    conn.close()


def update_expense_receipt(
    expense_id: int,
    receipt_path: str | None,
    receipt_original_name: str | None,
    receipt_media_type: str | None,
    receipt_source_file_id: str | None = None,
    receipt_source_url: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        UPDATE expenses
        SET receipt_path = %s,
            receipt_original_name = %s,
            receipt_media_type = %s,
            receipt_source_file_id = COALESCE(%s, receipt_source_file_id),
            receipt_source_url = COALESCE(%s, receipt_source_url)
        WHERE id = %s
        """,
        (
            receipt_path,
            receipt_original_name,
            receipt_media_type,
            receipt_source_file_id,
            receipt_source_url,
            expense_id,
        ),
    )

    conn.commit()
    conn.close()


def get_worker_work_logs(worker_id, year, month):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT work_date, work_type, project_name, hours
        FROM work_logs
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM work_date) = %s
          AND EXTRACT(MONTH FROM work_date) = %s
        ORDER BY work_date, id
        """,
        (worker_id, year, month),
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_worker_expenses(worker_id, year, month):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)

    cursor.execute(
        """
        SELECT expense_date, amount, description, status, receipt_path
        FROM expenses
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM expense_date) = %s
          AND EXTRACT(MONTH FROM expense_date) = %s
        ORDER BY expense_date, id
        """,
        (worker_id, year, month),
    )

    rows = cursor.fetchall()
    conn.close()
    return rows


def get_worker_period_stats(worker_id, year, month, day_from, day_to):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        SELECT work_type, COALESCE(SUM(hours), 0), COUNT(*)
        FROM work_logs
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM work_date) = %s
          AND EXTRACT(MONTH FROM work_date) = %s
          AND EXTRACT(DAY FROM work_date) BETWEEN %s AND %s
        GROUP BY work_type
        """,
        (worker_id, year, month, day_from, day_to),
    )
    work_rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM bonuses
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM bonus_date) = %s
          AND EXTRACT(MONTH FROM bonus_date) = %s
          AND EXTRACT(DAY FROM bonus_date) BETWEEN %s AND %s
        """,
        (worker_id, year, month, day_from, day_to),
    )
    bonuses = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT bonus_date, amount, description
        FROM bonuses
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM bonus_date) = %s
          AND EXTRACT(MONTH FROM bonus_date) = %s
          AND EXTRACT(DAY FROM bonus_date) BETWEEN %s AND %s
        ORDER BY bonus_date, id
        """,
        (worker_id, year, month, day_from, day_to),
    )
    bonus_rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM penalties
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM penalty_date) = %s
          AND EXTRACT(MONTH FROM penalty_date) = %s
          AND EXTRACT(DAY FROM penalty_date) BETWEEN %s AND %s
        """,
        (worker_id, year, month, day_from, day_to),
    )
    penalties = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT penalty_date, amount, description
        FROM penalties
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM penalty_date) = %s
          AND EXTRACT(MONTH FROM penalty_date) = %s
          AND EXTRACT(DAY FROM penalty_date) BETWEEN %s AND %s
        ORDER BY penalty_date, id
        """,
        (worker_id, year, month, day_from, day_to),
    )
    penalty_rows = cursor.fetchall()

    cursor.execute(
        """
        SELECT COALESCE(SUM(amount), 0)
        FROM expenses
        WHERE worker_id = %s
          AND EXTRACT(YEAR FROM expense_date) = %s
          AND EXTRACT(MONTH FROM expense_date) = %s
          AND EXTRACT(DAY FROM expense_date) BETWEEN %s AND %s
          AND status = 'approved'
        """,
        (worker_id, year, month, day_from, day_to),
    )
    approved_expenses = cursor.fetchone()[0]

    cursor.execute(
        """
        SELECT work_type, MAX(rate_per_hour)
        FROM rates
        WHERE worker_id = %s
        GROUP BY work_type
        """,
        (worker_id,),
    )
    rate_rows = cursor.fetchall()

    conn.close()

    rates = {work_type: float(rate or 0) for work_type, rate in rate_rows}
    stats = {
        "shift_count": 0,
        "shift_hours": 0.0,
        "shift_income": 0.0,
        "install_count": 0,
        "install_hours": 0.0,
        "install_income": 0.0,
        "bonuses": float(bonuses or 0),
        "bonus_rows": bonus_rows,
        "penalties": float(penalties or 0),
        "penalty_rows": penalty_rows,
        "approved_expenses": float(approved_expenses or 0),
    }

    for work_type, total_hours, count in work_rows:
        hours_value = float(total_hours or 0)
        if work_type == "shift":
            stats["shift_count"] = count
            stats["shift_hours"] = hours_value
        elif work_type == "install":
            stats["install_count"] = count
            stats["install_hours"] = hours_value

    stats["shift_income"] = stats["shift_hours"] * rates.get("shift", 0.0)
    stats["install_income"] = stats["install_hours"] * rates.get("install", 0.0)
    work_income = stats["shift_income"] + stats["install_income"]
    stats["work_income"] = work_income
    stats["total_income"] = (
        work_income
        + stats["bonuses"]
        + stats["approved_expenses"]
        - stats["penalties"]
    )
    return stats


def save_to_db(data: dict, worker_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    _ensure_expense_receipt_columns(cursor)
    _ensure_projects_table(cursor)
    _ensure_work_logs_project_link(cursor)

    today = date.today()
    days_in_month = monthrange(today.year, today.month)[1]
    requested_day = int(data.get("date"))
    if requested_day < 1 or requested_day > days_in_month:
        conn.close()
        raise ValueError(f"Дата должна быть в диапазоне от 1 до {days_in_month} для текущего месяца.")
    if requested_day > today.day:
        conn.close()
        raise ValueError(
            'Нельзя вносить смены, монтажи или расходы на будущие дни текущего месяца. '
            'Введите другую дату или используйте сценарий "За прошлый месяц".'
        )

    try:
        work_date = today.replace(day=requested_day)
    except ValueError as exc:
        conn.close()
        raise ValueError("Некорректная дата для текущего месяца") from exc

    place = data.get("place")
    created_expense_id = None

    if place in ["Монтаж", "Смена"]:
        work_type = "install" if place == "Монтаж" else "shift"
        project_name = data.get("project")
        project_id = None
        if work_type == "install" and project_name:
            project = get_active_project_by_name(project_name)
            if project:
                project_id = project[0]

        cursor.execute(
            """
            INSERT INTO work_logs (worker_id, work_type, work_date, project_name, project_id, hours)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                worker_id,
                work_type,
                work_date,
                project_name,
                project_id,
                data.get("hours"),
            ),
        )

    elif place == "Расходы":
        cursor.execute(
            """
            INSERT INTO expenses (worker_id, expense_date, amount, description, status)
            VALUES (%s, %s, %s, %s, 'pending')
            RETURNING id
            """,
            (
                worker_id,
                work_date,
                data.get("amount"),
                data.get("expense_type"),
            ),
        )
        created_expense_id = cursor.fetchone()[0]
        cursor.execute(
            """
            UPDATE expenses
            SET source_platform = %s,
                source_peer_id = %s
            WHERE id = %s
            """,
            (
                data.get("source_platform"),
                data.get("source_peer_id"),
                created_expense_id,
            ),
        )

    conn.commit()
    conn.close()

    return created_expense_id
