"""Bounded stereo job phases with a separate rollback budget."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator, Callable

CREATE_REMOVE_BUDGET_SECONDS = 360
ROLLBACK_BUDGET_SECONDS = 240


@asynccontextmanager
async def phase_budget(
    seconds: float,
    error_code: str,
    *,
    clock: Callable[[], float] | None = None,
) -> AsyncIterator[None]:
    """Turn phase expiry into a stable job error without silencing cancellation.

    This context uses the event loop's monotonic clock. The caller starts a
    separate rollback budget after the primary operation expires.
    """
    start = clock() if clock is not None else None
    try:
        async with asyncio.timeout(seconds):
            yield
    except TimeoutError as exc:
        raise RuntimeError(error_code) from exc
    if start is not None and clock is not None and clock() - start > seconds:
        # A fake clock/sleep can advance instantly in deterministic tests;
        # asyncio.timeout still enforces a real deadline in production.
        raise RuntimeError(error_code)
