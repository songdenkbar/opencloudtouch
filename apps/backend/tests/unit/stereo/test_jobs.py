"""Mock-mode stereo lifecycle and rollback regression tests."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencloudtouch.stereo.group_backend import MockGroupBackend, StoredGroup, mock_group_store
from opencloudtouch.stereo.group_files import parse_group_xml
from opencloudtouch.stereo.service import StereoService


LEFT = "DDEEFF445566"
RIGHT = "A1B2C3D4E5F6"


def device(device_id: str, ip: str):
    return SimpleNamespace(
        device_id=device_id, ip=ip, model="SoundTouch 10",
        ssh_permanent=False,
    )


@pytest.fixture(autouse=True)
def clear_mock_store():
    mock_group_store.clear()
    yield
    mock_group_store.clear()


@pytest.fixture
def service():
    left = device(LEFT, "192.168.1.101")
    right = device(RIGHT, "192.168.1.103")
    repo = SimpleNamespace(
        get_all=AsyncMock(return_value=[left, right]),
        get_by_device_id=AsyncMock(side_effect=lambda key: {
            LEFT: left, RIGHT: right,
        }.get(key)),
    )
    devices = SimpleNamespace(reboot_device=AsyncMock())
    zones = SimpleNamespace(get_zone_status=AsyncMock(return_value=None))
    return StereoService(repo, devices, zones, mock_mode=True)


@pytest.mark.asyncio
async def test_create_list_remove_round_trip(service):
    job = await service.create(LEFT, RIGHT, "Study & Kitchen")
    assert job["operation"] == "create"
    assert job["state"] == "running"
    await service.tasks[job["job_id"]]
    assert service.job(job["job_id"])["state"] == "succeeded"

    listing = await service.list_pairs()
    assert len(listing["pairs"]) == 1
    pair = listing["pairs"][0]
    assert pair["partial"] is False
    assert pair["inconsistent"] is False
    assert pair["status"] == "GROUP_OK"
    assert pair["left_device_id"] == LEFT
    assert pair["right_device_id"] == RIGHT
    assert listing["recovery_needed_device_ids"] == []

    left_state = mock_group_store["192.168.1.101"]
    right_state = mock_group_store["192.168.1.103"]
    assert parse_group_xml(left_state.content).status is None
    assert parse_group_xml(right_state.content).status == "GROUP_OK"
    assert left_state.journal is None and right_state.journal is None

    removed = await service.remove(pair["group_id"])
    await service.tasks[removed["job_id"]]
    assert service.job(removed["job_id"])["state"] == "succeeded"
    assert (await service.list_pairs())["pairs"] == []
    assert mock_group_store["192.168.1.101"].content is None
    assert mock_group_store["192.168.1.103"].content is None


@pytest.mark.asyncio
async def test_rejects_same_speaker_without_writes(service):
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, LEFT, "A pair")
    assert "stereo_same_device" in str(getattr(exc.value, "code", exc.value))
    assert mock_group_store == {}


@pytest.mark.asyncio
async def test_recovery_reinstates_previous_file(service):
    backend = MockGroupBackend()
    ip = "192.168.1.101"
    mock_group_store[ip] = StoredGroup(content="<original />")
    job_id = "a" * 32
    await backend.prepare(ip, job_id, "create", "<replacement />")
    await backend.commit(ip, job_id)
    assert await backend.leftover(ip)
    assert await backend.recover(ip)
    assert mock_group_store[ip].content == "<original />"
    assert not await backend.leftover(ip)


@pytest.mark.asyncio
async def test_recovery_removes_new_file(service):
    backend = MockGroupBackend()
    ip = "192.168.1.101"
    job_id = "a" * 32
    await backend.prepare(ip, job_id, "create", "<replacement />")
    await backend.commit(ip, job_id)
    assert await backend.recover(ip)
    assert mock_group_store[ip].content is None


@pytest.mark.asyncio
async def test_device_in_zone_rejected(service):
    service.zones.get_zone_status.return_value = object()
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "A pair")
    assert getattr(exc.value, "code", None) == "stereo_device_in_zone"



@pytest.mark.asyncio
async def test_second_commit_failure_rolls_back_both_devices(service):
    class FailingSecondCommit(MockGroupBackend):
        def __init__(self):
            self.calls = 0

        async def commit(self, ip: str, job: str, *, removing: bool = False):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("stereo_write_failed")
            await super().commit(ip, job, removing=removing)

    service.backend = FailingSecondCommit()
    created = await service.create(LEFT, RIGHT, "Failed attempt")
    await service.tasks[created["job_id"]]
    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is True
    assert all(s.content is None for s in mock_group_store.values())
    assert all(s.journal is None for s in mock_group_store.values())


@pytest.mark.asyncio
async def test_rejects_leftover_before_new_pair(service):
    mock_group_store["192.168.1.101"] = StoredGroup(
        journal={"job_id": "a" * 32, "operation": "create", "had_file": False},
    )
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "New Pair")
    assert getattr(exc.value, "code", None) == "stereo_recovery_needed"



@pytest.mark.asyncio
async def test_cleanup_failure_never_undoes_verified_pair(service):
    class FailingCleanup(MockGroupBackend):
        def __init__(self):
            self.calls = 0

        async def cleanup(self, ip: str, job: str):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("stereo_ssh_failed")
            await super().cleanup(ip, job)

    service.backend = FailingCleanup()
    job = await service.create(LEFT, RIGHT, "Protected Pair")
    await service.tasks[job["job_id"]]
    result = service.job(job["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    # Speaker files must still agree: recovery is cleanup-only after verification.
    assert len((await service.list_pairs())["pairs"]) == 1
    outstanding = mock_group_store["192.168.1.103"]
    assert outstanding.journal["state"] == "verified"
    assert await service.backend.recover("192.168.1.103")
    assert len((await service.list_pairs())["pairs"]) == 1
    assert not await service.backend.leftover("192.168.1.103")
