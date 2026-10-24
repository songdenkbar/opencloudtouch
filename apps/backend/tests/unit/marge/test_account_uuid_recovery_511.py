"""Bounded read-only account UUID recovery tests for issue 511."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from opencloudtouch.marge.service import MargeService


def _service():
    device_repo = AsyncMock()
    device_repo.get_by_account_uuid.return_value = None
    device_repo.get_by_device_id.return_value = None
    speaker = MagicMock()
    speaker.device_id = "AABBCCDDEEFF"
    speaker.ip = "192.168.1.10"
    device_repo.get_all.return_value = [speaker]
    service = MargeService(AsyncMock(), AsyncMock(), device_repo)
    return service, device_repo, speaker


@pytest.mark.asyncio
async def test_unknown_uuid_recovered_from_known_speaker():
    service, repo, speaker = _service()
    with patch(
        "opencloudtouch.marge.service.check_marge_account_uuid",
        new_callable=AsyncMock,
        return_value="7654321",
    ) as reader:
        assert await service.resolve_device_id_for_account("7654321") == speaker.device_id
        reader.assert_awaited_once()
    repo.update_marge_account_uuid.assert_awaited_once_with(
        speaker.device_id, "7654321"
    )


@pytest.mark.asyncio
async def test_repeated_unknown_uuid_does_not_reprobe_within_five_seconds():
    service, repo, _ = _service()
    with patch(
        "opencloudtouch.marge.service.check_marge_account_uuid",
        new_callable=AsyncMock,
        return_value="1111111",
    ) as reader:
        assert await service.resolve_device_id_for_account("7654321") is None
        assert await service.resolve_device_id_for_account("7654321") is None
        reader.assert_awaited_once()


@pytest.mark.asyncio
async def test_no_device_probe_for_invalid_account_id():
    service, repo, _ = _service()
    with patch(
        "opencloudtouch.marge.service.check_marge_account_uuid",
        new_callable=AsyncMock,
    ) as reader:
        assert await service.resolve_device_id_for_account("unknown") is None
        reader.assert_not_awaited()


@pytest.mark.asyncio
async def test_three_scans_max_per_minute():
    service, repo, _ = _service()
    times = iter([0, 6, 12, 18])
    with (
        patch("opencloudtouch.marge.service.time.monotonic", side_effect=times),
        patch(
            "opencloudtouch.marge.service.check_marge_account_uuid",
            new_callable=AsyncMock,
            return_value="1111111",
        ) as reader,
    ):
        for _ in range(4):
            assert await service.resolve_device_id_for_account("7654321") is None
    assert reader.await_count == 3
