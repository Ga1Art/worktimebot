from __future__ import annotations

import logging

from services.db import get_connection


logger = logging.getLogger(__name__)


class RuntimeLock:
    def __init__(self, lock_key: int, name: str):
        self.lock_key = lock_key
        self.name = name
        self.conn = None

    def acquire(self):
        if self.conn is not None:
            return True

        conn = get_connection()
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute("SELECT pg_try_advisory_lock(%s)", (self.lock_key,))
        acquired = bool(cursor.fetchone()[0])
        cursor.close()

        if not acquired:
            conn.close()
            logger.error("Runtime lock '%s' is already held by another process.", self.name)
            return False

        self.conn = conn
        logger.info("Runtime lock '%s' acquired.", self.name)
        return True

    def release(self):
        if self.conn is None:
            return

        try:
            cursor = self.conn.cursor()
            cursor.execute("SELECT pg_advisory_unlock(%s)", (self.lock_key,))
            cursor.close()
        except Exception:
            logger.exception("Failed to release runtime lock '%s'.", self.name)
        finally:
            try:
                self.conn.close()
            finally:
                self.conn = None


TELEGRAM_RUNTIME_LOCK = RuntimeLock(910001, "telegram_bot")
VK_RUNTIME_LOCK = RuntimeLock(910002, "vk_bot")
