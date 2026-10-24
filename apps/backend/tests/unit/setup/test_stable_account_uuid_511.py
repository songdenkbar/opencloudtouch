"""Regression tests for stable per-device account UUIDs (issue #511)."""

from unittest.mock import AsyncMock, patch

import pytest

from opencloudtouch.setup.account_pairing_service import (
    AccountPairingResult,
    _generate_account_uuid,
    ensure_account_uuid_unique,
    is_valid_account_uuid,
)


def test_uuid_is_stable_between_servers_and_mac_formats():
    expected = _generate_account_uuid("AABBCCDDEEFF")
    assert expected == _generate_account_uuid("aa:bb:cc:dd:ee:ff")
    assert expected == _generate_account_uuid("aa-bb-cc-dd-ee-ff")
    assert is_valid_account_uuid(expected)
    assert len(expected) == 7


def test_collision_fallback_is_stable():
    device = "AABBCCDDEEFF"
    first = _generate_account_uuid(device)
    second = _generate_account_uuid(device, 1)
    assert second == _generate_account_uuid(device, 1)
    assert first != second


@pytest.mark.asyncio
async def test_two_independent_servers_choose_identical_uuid():
    values = []

    async def write_uuid(ssh, uuid, device_name):
        return AccountPairingResult(success=True, had_uuid=False, uuid=uuid)

    for _ in range(2):
        repo = AsyncMock()
        repo.get_by_account_uuid.return_value = None
        with (
            patch(
                "opencloudtouch.setup.account_pairing_service.check_marge_account_uuid",
                new_callable=AsyncMock,
                return_value=None,
            ),
            patch(
                "opencloudtouch.setup.account_pairing_service.set_account_uuid_via_ssh",
                new_callable=AsyncMock,
                side_effect=write_uuid,
            ),
        ):
            result = await ensure_account_uuid_unique(
                "192.168.1.100", "AABBCCDDEEFF", repo, ssh=AsyncMock()
            )
            assert result.success
            values.append(result.uuid)

    assert values == [_generate_account_uuid("AABBCCDDEEFF")] * 2


@pytest.mark.asyncio
async def test_collision_uses_counter_one():
    from unittest.mock import MagicMock

    other = MagicMock()
    other.device_id = "112233445566"
    repo = AsyncMock()
    repo.get_by_account_uuid.side_effect = [other, None]

    async def write_uuid(ssh, uuid, device_name):
        return AccountPairingResult(success=True, had_uuid=False, uuid=uuid)

    with (
        patch(
            "opencloudtouch.setup.account_pairing_service.check_marge_account_uuid",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch(
            "opencloudtouch.setup.account_pairing_service.set_account_uuid_via_ssh",
            new_callable=AsyncMock,
            side_effect=write_uuid,
        ),
    ):
        result = await ensure_account_uuid_unique(
            "192.168.1.100", "AABBCCDDEEFF", repo, ssh=AsyncMock()
        )

    assert result.success
    assert result.uuid == _generate_account_uuid("AABBCCDDEEFF", 1)


@pytest.mark.asyncio
async def test_info_connection_failure_never_writes_uuid():
    import httpx

    from opencloudtouch.setup.account_pairing_service import ensure_account_uuid

    with (
        patch(
            "opencloudtouch.setup.account_pairing_service.httpx.AsyncClient"
        ) as client_cls,
        patch(
            "opencloudtouch.setup.account_pairing_service.set_account_uuid_via_ssh",
            new_callable=AsyncMock,
        ) as setter,
    ):
        client = AsyncMock()
        client.get.side_effect = httpx.ConnectError("offline")
        client_cls.return_value.__aenter__ = AsyncMock(return_value=client)
        client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await ensure_account_uuid(
            "192.168.1.100", device_id="AABBCCDDEEFF", ssh=AsyncMock()
        )
    assert result.success is False
    assert "Cannot verify" in result.error
    setter.assert_not_called()


@pytest.mark.asyncio
async def test_smaller_mac_keeps_existing_uuid_and_reassigns_other():
    from unittest.mock import MagicMock

    owner = MagicMock()
    owner.device_id = "BBBBCCDDEEFF"
    owner.ip = "192.168.1.200"
    owner.name = "Other"
    repo = AsyncMock()
    repo.get_by_account_uuid.side_effect = [owner, None]
    repo.update_marge_account_uuid = AsyncMock()

    async def write_uuid(ssh, uuid, device_name):
        return AccountPairingResult(success=True, had_uuid=False, uuid=uuid)

    with (
        patch(
            "opencloudtouch.setup.account_pairing_service.check_marge_account_uuid",
            new_callable=AsyncMock,
            side_effect=["1234567", "1234567"],
        ),
        patch(
            "opencloudtouch.setup.account_pairing_service.SoundTouchSSHClient"
        ) as ssh_cls,
        patch(
            "opencloudtouch.setup.account_pairing_service.set_account_uuid_via_ssh",
            new_callable=AsyncMock,
            side_effect=write_uuid,
        ),
    ):
        ssh_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        ssh_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await ensure_account_uuid_unique(
            "192.168.1.100", "AABBCCDDEEFF", repo
        )

    assert result.success
    assert result.uuid == "1234567"
    repo.update_marge_account_uuid.assert_awaited_once_with(
        "BBBBCCDDEEFF", _generate_account_uuid("BBBBCCDDEEFF")
    )


def test_fifth_write_triggers_five_minute_block():
    from opencloudtouch.setup.account_pairing_service import (
        _UUID_BLOCKED_UNTIL,
        _UUID_CHANGES,
        _record_uuid_write,
        _uuid_write_allowed,
    )

    device_id = "TEST_LIMITER"
    _UUID_CHANGES.pop(device_id, None)
    _UUID_BLOCKED_UNTIL.pop(device_id, None)
    try:
        for _ in range(4):
            assert _uuid_write_allowed(device_id)
            _record_uuid_write(device_id)
        assert _uuid_write_allowed(device_id)
        _record_uuid_write(device_id)
        assert not _uuid_write_allowed(device_id)
    finally:
        _UUID_CHANGES.pop(device_id, None)
        _UUID_BLOCKED_UNTIL.pop(device_id, None)


@pytest.mark.asyncio
async def test_mac_priority_ignores_case_and_delimiters():
    from unittest.mock import MagicMock

    owner = MagicMock()
    owner.device_id = "BB:BB:CC:DD:EE:FF"
    owner.ip = "192.168.1.200"
    owner.name = "Other"
    repo = AsyncMock()
    repo.get_by_account_uuid.side_effect = [owner, None]
    with (
        patch(
            "opencloudtouch.setup.account_pairing_service.check_marge_account_uuid",
            new_callable=AsyncMock,
            side_effect=["1234567", "1234567"],
        ),
        patch(
            "opencloudtouch.setup.account_pairing_service.SoundTouchSSHClient"
        ) as ssh_cls,
        patch(
            "opencloudtouch.setup.account_pairing_service.set_account_uuid_via_ssh",
            new_callable=AsyncMock,
            return_value=AccountPairingResult(
                success=True, had_uuid=False,
                uuid=_generate_account_uuid(owner.device_id),
            ),
        ),
    ):
        ssh_cls.return_value.__aenter__ = AsyncMock(return_value=AsyncMock())
        ssh_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        result = await ensure_account_uuid_unique(
            "192.168.1.100", "aa-bb-cc-dd-ee-ff", repo
        )
    assert result.success
    assert result.uuid == "1234567"
    repo.update_marge_account_uuid.assert_awaited_once()
