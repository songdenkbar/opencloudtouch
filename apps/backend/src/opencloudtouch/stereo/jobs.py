"""Bounded stereo job phases with a separate rollback budget."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator

CREATE_REMOVE_BUDGET_SECONDS = 360
ROLLBACK_BUDGET_SECONDS = 240


@asynccontextmanager
async def phase_budget(seconds: float, error_code: str) -> AsyncIterator[None]:
    """Turn phase expiry into a stable job error without silencing cancellation.

    This context uses the event loop's monotonic clock. The caller starts a
    separate rollback budget after the primary operation expires.
    """
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as exc:
        raise RuntimeError(error_code) from exc
