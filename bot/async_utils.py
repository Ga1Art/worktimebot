import asyncio
import logging
import time
from functools import partial

logger = logging.getLogger(__name__)
SLOW_SYNC_CALL_SECONDS = 0.75

async def run_sync(func, *args, **kwargs):
    started_at = time.monotonic()
    if kwargs:
        result = await asyncio.to_thread(partial(func, *args, **kwargs))
    else:
        result = await asyncio.to_thread(func, *args)

    elapsed = time.monotonic() - started_at
    if elapsed >= SLOW_SYNC_CALL_SECONDS:
        logger.warning("Slow run_sync call: %s took %.3fs", getattr(func, "__name__", repr(func)), elapsed)
    return result
