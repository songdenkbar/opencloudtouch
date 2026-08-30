"""Unit tests for SoundTouch group handling in BoseDeviceClientAdapter."""

from unittest.mock import MagicMock

import pytest

from opencloudtouch.devices.client_adapter import BoseDeviceClientAdapter


def _make_client():
    """Create adapter without running the real Bose constructor."""
    client = object.__new__(BoseDeviceClientAdapter)
    client.ip = "192.168.1.100"
    client._client = MagicMock()
    return client


class TestGetGroupStatus:
    """Tests for get_group_status."""

    @pytest.mark.asyncio
    async def test_returns_group_status(self):
        """Converts bosesoundtouchapi Group into internal model."""
        client = _make_client()

        left = MagicMock()
        left.DeviceId = "MASTER"
        left.IpAddress = "192.168.1.100"
        left.Role = "LEFT"

        right = MagicMock()
        right.DeviceId = "SLAVE"
        right.IpAddress = "192.168.1.101"
        right.Role = "RIGHT"

        group = MagicMock()
        group.GroupId = 1234567
        group.Name = "Living Room Stereo"
        group.MasterDeviceId = "MASTER"
        group.SenderIpAddress = "192.168.1.100"
        group.Status = "GROUP_OK"
        group.Roles = [left, right]

        client._client.GetGroupStereoPairStatus.return_value = group

        result = await client.get_group_status()

        assert result is not None
        assert result.group_id == "1234567"
        assert result.name == "Living Room Stereo"
        assert result.master_device_id == "MASTER"
        assert result.status == "GROUP_OK"
        assert [(r.device_id, r.role) for r in result.roles] == [
            ("MASTER", "LEFT"),
            ("SLAVE", "RIGHT"),
        ]

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_group(self):
        """Returns None when /getGroup contains no group state."""
        client = _make_client()

        group = MagicMock()
        group.GroupId = None
        group.MasterDeviceId = None
        group.Roles = []

        client._client.GetGroupStereoPairStatus.return_value = group

        result = await client.get_group_status()

        assert result is None
