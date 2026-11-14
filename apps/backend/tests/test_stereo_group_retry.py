"""Regression tests for transient SoundTouch stereo group read failures."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from opencloudtouch.stereo.service import StereoService


@pytest.mark.asyncio
async def test_group_safe_retries_transient_unreachable(monkeypatch):
    service = object.__new__(StereoService)
    group = object()
    service._group = AsyncMock(
        side_effect=[httpx.ConnectError("brief standby transition"), group]
    )
    sleep = AsyncMock()
    monkeypatch.setattr("opencloudtouch.stereo.service.asyncio.sleep", sleep)

    actual, reachable = await service._group_safe(
        SimpleNamespace(device_id="device-left")
    )

    assert reachable is True
    assert actual is group
    assert service._group.await_count == 2
    sleep.assert_awaited_once_with(0.5)


@pytest.mark.asyncio
async def test_group_safe_remains_unreachable_after_retry_budget(monkeypatch):
    service = object.__new__(StereoService)
    service._group = AsyncMock(side_effect=httpx.ConnectError("offline"))
    sleep = AsyncMock()
    monkeypatch.setattr("opencloudtouch.stereo.service.asyncio.sleep", sleep)

    actual, reachable = await service._group_safe(
        SimpleNamespace(device_id="device-left")
    )

    assert actual is None
    assert reachable is False
    assert service._group.await_count == 3
    assert sleep.await_count == 2
