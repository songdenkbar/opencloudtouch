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



@pytest.mark.asyncio
async def test_timeout_starts_rollback_with_independent_budget(service):
    import asyncio

    class SlowCommit(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            await asyncio.sleep(0.03)
            await super().commit(ip, job, removing=removing)

    service.backend = SlowCommit()
    service.primary_budget_seconds = 0.005
    service.rollback_budget_seconds = 1.0
    job = await service.create(LEFT, RIGHT, "Timeout test")
    await service.tasks[job["job_id"]]
    result = service.job(job["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_timeout"
    assert result["rolled_back"] is True
    assert all(group.content is None for group in mock_group_store.values())
    assert all(group.journal is None for group in mock_group_store.values())


@pytest.mark.asyncio
async def test_second_commit_rollback_has_no_reboot(service):
    class FailingSecondCommit(MockGroupBackend):
        def __init__(self):
            self.calls = 0

        async def commit(self, ip, job, *, removing=False):
            self.calls += 1
            if self.calls == 2:
                raise RuntimeError("stereo_write_failed")
            await super().commit(ip, job, removing=removing)

    service.backend = FailingSecondCommit()
    job = await service.create(LEFT, RIGHT, "Rollback before reboot")
    await service.tasks[job["job_id"]]
    assert service.job(job["job_id"])["rolled_back"] is True
    service.devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_rollback_failure_retains_recovery_journal(service):
    class FailingRollback(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            raise RuntimeError("stereo_write_failed")

        async def rollback(self, ip, job):
            raise OSError("rollback unavailable")

    service.backend = FailingRollback()
    job = await service.create(LEFT, RIGHT, "Keep journal")
    await service.tasks[job["job_id"]]
    result = service.job(job["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_rollback_failed"
    assert result["rolled_back"] is False
    assert any(group.journal is not None for group in mock_group_store.values())


@pytest.mark.asyncio
async def test_original_state_verified_before_cleanup(service):
    class VerificationFails(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            raise RuntimeError("stereo_write_failed")

    service.backend = VerificationFails()
    # Make firmware falsely report a group after restoration.
    async def wrong_group(device):
        from opencloudtouch.stereo.group_files import (
            GroupInfo, GroupRole,
        )
        return GroupInfo(
            group_id="7777777",
            name="Wrong",
            master_device_id=LEFT,
            roles=[GroupRole(LEFT, "LEFT", "192.168.1.101")],
            status="GROUP_OK",
        ), True

    original_group_safe = service._group_safe
    failures = 0

    async def group_safe(device):
        nonlocal failures
        result = await original_group_safe(device)
        if not mock_group_store.get("192.168.1.101"):
            return result
        if mock_group_store["192.168.1.101"].journal is not None:
            failures += 1
            return await wrong_group(device)
        return result

    service._group_safe = group_safe
    # This also affects initial validation if installed too early; install
    # only after the service has accepted the create request.
    service._group_safe = original_group_safe
    created = await service.create(LEFT, RIGHT, "Verify state")
    service._group_safe = group_safe
    await service.tasks[created["job_id"]]
    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    assert result["error_code"] == "stereo_rollback_failed"
    assert failures > 0
    assert any(group.journal is not None for group in mock_group_store.values())



@pytest.mark.asyncio
async def test_shutdown_during_staging_keeps_journal_and_finishes_task(service):
    import asyncio

    preparing = asyncio.Event()

    class InterruptedPreparation(MockGroupBackend):
        async def prepare(self, ip, job, operation, new):
            await super().prepare(ip, job, operation, new)
            preparing.set()
            await asyncio.Event().wait()  # interrupted before any file commit

    service.backend = InterruptedPreparation()
    created = await service.create(LEFT, RIGHT, "Interrupted")
    await asyncio.wait_for(preparing.wait(), 1)
    await service.shutdown()
    await asyncio.sleep(0)
    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    assert not service.tasks
    assert any(state.journal is not None for state in mock_group_store.values())
    assert all(state.content is None for state in mock_group_store.values())


@pytest.mark.asyncio
async def test_failed_verification_after_reboot_restores_original_state(service):
    class VerificationFailure(MockGroupBackend):
        pass

    service.backend = VerificationFailure()

    async def fail_verification(devices, group_id):
        raise RuntimeError("stereo_verify_failed")

    service._verify = fail_verification
    job = await service.create(LEFT, RIGHT, "Reboot and rollback")
    await service.tasks[job["job_id"]]
    result = service.job(job["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_verify_failed"
    assert result["rolled_back"] is True
    assert all(state.content is None for state in mock_group_store.values())
    assert all(state.journal is None for state in mock_group_store.values())


@pytest.mark.asyncio
async def test_preparing_mock_rollback_never_overwrites_original_file(service):
    ip = "192.168.1.101"
    mock_group_store[ip] = StoredGroup(
        content="<original/>",
        journal={
            "job_id": "a" * 32,
            "operation": "create",
            "had_file": True,
            "state": "preparing",
        },
        backup=None,
    )
    backend = MockGroupBackend()
    await backend.rollback(ip, "a" * 32)
    assert mock_group_store[ip].content == "<original/>"
    assert mock_group_store[ip].journal is not None



@pytest.mark.asyncio
async def test_rollback_budget_expiry_retains_journals(service):
    import asyncio

    class SlowRollback(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            raise RuntimeError("stereo_write_failed")

        async def rollback(self, ip, job):
            await asyncio.sleep(0.05)
            await super().rollback(ip, job)

    service.backend = SlowRollback()
    service.rollback_budget_seconds = 0.005
    job = await service.create(LEFT, RIGHT, "Rollback deadline")
    await service.tasks[job["job_id"]]
    result = service.job(job["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_rollback_failed"
    assert result["rolled_back"] is False
    assert any(state.journal is not None for state in mock_group_store.values())


@pytest.mark.asyncio
async def test_recover_both_unverified_speakers_restores_original_pair_state(service):
    """A crash after both commits, before verification, restores both sides."""
    backend = MockGroupBackend()
    job_id = "b" * 32
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    for ip, content in [(left_ip, "<left-original/>"), (right_ip, "<right-original/>")]:
        mock_group_store[ip] = StoredGroup(content=content)
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)

    assert await backend.recover(left_ip)
    assert await backend.recover(right_ip)
    assert mock_group_store[left_ip].content == "<left-original/>"
    assert mock_group_store[right_ip].content == "<right-original/>"
    assert not await backend.leftover(left_ip)
    assert not await backend.leftover(right_ip)


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="Known issue: per-device recovery cannot coordinate a partly verified stereo pair",
)
async def test_recover_mixed_verification_must_not_leave_split_pair(service):
    """A crash between mark_verified calls needs coordinated recovery."""
    backend = MockGroupBackend()
    job_id = "c" * 32
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    for ip in (left_ip, right_ip):
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)

    # Simulate a process crash after verifying only the left speaker.
    await backend.mark_verified(left_ip, job_id)
    await backend.recover(left_ip)
    await backend.recover(right_ip)

    assert mock_group_store[left_ip].content == mock_group_store[right_ip].content


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "committed"])
async def test_single_speaker_recovery_rejects_unsafe_pair_transaction(service, state):
    """Never roll back only one side of an undecided stereo transaction."""
    ip = "192.168.1.101"
    mock_group_store[ip] = StoredGroup(
        content="<new-stereo/>",
        backup="<original/>",
        journal={
            "job_id": "d" * 32, "operation": "create",
            "had_file": True, "state": state,
        },
    )
    with pytest.raises(Exception) as exc:
        await service.recover(LEFT)
    assert getattr(exc.value, "code", None) == "stereo_recovery_needed"
    assert mock_group_store[ip].content == "<new-stereo/>"
    assert mock_group_store[ip].journal["state"] == state
    assert not service.tasks
    assert not service.busy


@pytest.mark.asyncio
async def test_verified_single_speaker_recovery_only_cleans_journal(service):
    """Cleaning an already-verified transaction must retain the final file."""
    ip = "192.168.1.101"
    mock_group_store[ip] = StoredGroup(
        content="<new-stereo/>",
        backup="<original/>",
        journal={
            "job_id": "e" * 32, "operation": "create",
            "had_file": True, "state": "verified",
        },
    )
    job = await service.recover(LEFT)
    await service.tasks[job["job_id"]]
    assert service.job(job["job_id"])["state"] == "succeeded"
    assert mock_group_store[ip].content == "<new-stereo/>"
    assert not await service.backend.leftover(ip)


@pytest.mark.asyncio
async def test_service_recovers_both_pending_speaker_journals(service):
    """One recovery request rolls back a whole interrupted transaction."""
    backend = service.backend
    job_id = "f" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        mock_group_store[ip] = StoredGroup(content=None)
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)
    recovery = await service.recover(LEFT)
    await service.tasks[recovery["job_id"]]
    assert service.job(recovery["job_id"])["state"] == "succeeded"
    assert all(mock_group_store[ip].content is None for ip in ips)
    assert all(not await backend.leftover(ip) for ip in ips)


@pytest.mark.asyncio
async def test_service_recovers_mixed_verified_and_committed_as_completed_pair(service):
    """If one speaker is verified, preserve a pair verified on both speakers."""
    from opencloudtouch.stereo.group_files import build_group_xml

    backend = service.backend
    job_id = "a" * 32
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    for ip, right_file in ((left_ip, False), (right_ip, True)):
        xml = build_group_xml(
            "8765432", "Recovered", LEFT, left_ip, RIGHT, right_ip,
            right_file=right_file,
        )
        await backend.prepare(ip, job_id, "create", xml)
        await backend.commit(ip, job_id)
    await backend.mark_verified(left_ip, job_id)

    recovery = await service.recover(RIGHT)
    await service.tasks[recovery["job_id"]]
    assert service.job(recovery["job_id"])["state"] == "succeeded"
    assert len((await service.list_pairs())["pairs"]) == 1
    assert all(not await backend.leftover(ip) for ip in (left_ip, right_ip))


@pytest.mark.asyncio
async def test_service_recovery_restarts_after_interrupted_rollback_decision(service):
    """A second crash between restored markers is safe to retry."""
    class InterruptedMarker(MockGroupBackend):
        def __init__(self):
            self.calls = 0

        async def mark_restored(self, ip, job):
            self.calls += 1
            if self.calls == 2:
                raise OSError("simulated process interruption")
            await super().mark_restored(ip, job)

    backend = InterruptedMarker()
    service.backend = backend
    job_id = "b" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)
    first = await service.recover(LEFT)
    await service.tasks[first["job_id"]]
    assert service.job(first["job_id"])["state"] == "failed"
    assert any(await backend.leftover(ip) for ip in ips)

    second = await service.recover(RIGHT)
    await service.tasks[second["job_id"]]
    assert service.job(second["job_id"])["state"] == "succeeded"
    assert all(mock_group_store[ip].content is None for ip in ips)
    assert all(not await backend.leftover(ip) for ip in ips)
