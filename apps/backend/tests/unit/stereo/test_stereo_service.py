"""Unit tests for StereoService."""

from unittest.mock import AsyncMock, patch

import pytest

from opencloudtouch.devices.repository import Device
from opencloudtouch.stereo.models import StereoGroupRole, StereoGroupStatus
from opencloudtouch.stereo.service import StereoService
from opencloudtouch.zones.models import ZoneStatus


def _make_device(
    device_id="MASTER",
    ip="192.168.1.100",
    name="Living Room",
    model="SoundTouch 10",
):
    """Create a test device."""
    return Device(
        device_id=device_id,
        ip=ip,
        name=name,
        model=model,
        mac_address=device_id,
        firmware_version="27.0.6",
    )


def _make_service():
    """Create StereoService with mocked repository."""
    repo = AsyncMock()
    service = StereoService(device_repo=repo)
    return service, repo


def _make_group():
    """Create a valid stereo group."""
    return StereoGroupStatus(
        group_id="1234567",
        name="Living Room Stereo",
        master_device_id="MASTER",
        sender_ip_address="192.168.1.100",
        status="GROUP_OK",
        roles=[
            StereoGroupRole(
                device_id="MASTER",
                ip_address="192.168.1.100",
                role="LEFT",
            ),
            StereoGroupRole(
                device_id="SLAVE",
                ip_address="192.168.1.101",
                role="RIGHT",
            ),
        ],
    )


class TestValidatePair:
    """Tests for validate_pair."""

    @pytest.mark.asyncio
    async def test_rejects_same_device(self):
        service, _repo = _make_service()

        with pytest.raises(ValueError, match="different devices"):
            await service.validate_pair("MASTER", "MASTER")

    @pytest.mark.asyncio
    async def test_rejects_non_soundtouch_10(self):
        service, repo = _make_service()

        repo.get_by_device_id.side_effect = [
            _make_device("MASTER", model="SoundTouch 20"),
            _make_device("SLAVE", "192.168.1.101"),
        ]

        with pytest.raises(ValueError, match="not a SoundTouch 10"):
            await service.validate_pair("MASTER", "SLAVE")

    @pytest.mark.asyncio
    async def test_rejects_device_in_zone(self):
        service, repo = _make_service()

        master = _make_device("MASTER")
        slave = _make_device("SLAVE", "192.168.1.101")
        repo.get_by_device_id.side_effect = [master, slave]

        master_client = AsyncMock()
        slave_client = AsyncMock()

        master_client.get_zone_status.return_value = ZoneStatus(
            master_id="MASTER",
            master_ip=master.ip,
            is_master=True,
            members=[],
        )
        slave_client.get_zone_status.return_value = None

        with patch.object(
            service,
            "_get_client",
            side_effect=[master_client, slave_client],
        ):
            with pytest.raises(ValueError, match="multi-room zone"):
                await service.validate_pair("MASTER", "SLAVE")

    @pytest.mark.asyncio
    async def test_rejects_device_in_group(self):
        service, repo = _make_service()

        master = _make_device("MASTER")
        slave = _make_device("SLAVE", "192.168.1.101")
        repo.get_by_device_id.side_effect = [master, slave]

        master_client = AsyncMock()
        slave_client = AsyncMock()

        master_client.get_zone_status.return_value = None
        slave_client.get_zone_status.return_value = None
        master_client.get_group_status.return_value = _make_group()
        slave_client.get_group_status.return_value = None

        with patch.object(
            service,
            "_get_client",
            side_effect=[master_client, slave_client],
        ):
            with pytest.raises(ValueError, match="currently part of a group"):
                await service.validate_pair("MASTER", "SLAVE")


class TestVerifyPair:
    """Tests for verify_pair."""

    @pytest.mark.asyncio
    async def test_returns_verified_for_valid_pair(self):
        service, repo = _make_service()

        master = _make_device("MASTER")
        slave = _make_device("SLAVE", "192.168.1.101")

        repo.get_all.return_value = [master, slave]
        repo.get_by_device_id.side_effect = lambda device_id: {
            "MASTER": master,
            "SLAVE": slave,
        }.get(device_id)

        master_client = AsyncMock()
        slave_client = AsyncMock()
        master_client.get_group_status.return_value = _make_group()
        slave_client.get_group_status.return_value = _make_group()

        with patch.object(
            service,
            "_get_client",
            side_effect=[
                master_client,
                slave_client,
                master_client,
                slave_client,
            ],
        ):
            result = await service.verify_pair("1234567")

        assert result.verified is True
        assert result.status == "GROUP_OK"
        assert result.reason is None

    @pytest.mark.asyncio
    async def test_returns_false_when_group_missing(self):
        service, repo = _make_service()

        master = _make_device("MASTER")
        slave = _make_device("SLAVE", "192.168.1.101")
        repo.get_all.return_value = [master, slave]

        master_client = AsyncMock()
        slave_client = AsyncMock()
        master_client.get_group_status.return_value = None
        slave_client.get_group_status.return_value = None

        with patch.object(
            service,
            "_get_client",
            side_effect=[master_client, slave_client],
        ):
            result = await service.verify_pair("1234567")

        assert result.verified is False
        assert result.reason == "group_not_found"
