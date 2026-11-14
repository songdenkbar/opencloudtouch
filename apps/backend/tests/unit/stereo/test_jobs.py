"""Mock-mode stereo lifecycle and rollback regression tests."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from opencloudtouch.stereo.group_backend import (
    MockGroupBackend,
    StoredGroup,
    mock_group_store,
)
from opencloudtouch.stereo.group_files import parse_group_xml
from opencloudtouch.stereo.service import StereoService

LEFT = "DDEEFF445566"
RIGHT = "A1B2C3D4E5F6"


def device(device_id: str, ip: str):
    return SimpleNamespace(
        device_id=device_id,
        ip=ip,
        model="SoundTouch 10",
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
        get_by_device_id=AsyncMock(
            side_effect=lambda key: {
                LEFT: left,
                RIGHT: right,
            }.get(key)
        ),
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
            GroupInfo,
            GroupRole,
        )

        return (
            GroupInfo(
                group_id="7777777",
                name="Wrong",
                master_device_id=LEFT,
                roles=[GroupRole(LEFT, "LEFT", "192.168.1.101")],
                status="GROUP_OK",
            ),
            True,
        )

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
async def test_recover_mixed_verification_preserves_pair_via_service(service):
    """A crash between verified markers is resolved by a paired decision."""
    from opencloudtouch.stereo.group_files import build_group_xml

    backend = service.backend
    job_id = "c" * 32
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    for ip, right_file in ((left_ip, False), (right_ip, True)):
        content = build_group_xml(
            "8765432",
            "Recovered",
            LEFT,
            left_ip,
            RIGHT,
            right_ip,
            right_file=right_file,
        )
        await backend.prepare(ip, job_id, "create", content)
        await backend.commit(ip, job_id)

    await backend.mark_verified(left_ip, job_id)
    recovery = await service.recover(RIGHT)
    await service.tasks[recovery["job_id"]]

    assert service.job(recovery["job_id"])["state"] == "succeeded"
    pair = (await service.list_pairs())["pairs"]
    assert len(pair) == 1
    assert pair[0]["partial"] is False
    assert pair[0]["inconsistent"] is False
    assert pair[0]["group_id"] == "8765432"
    assert not any([await backend.leftover(ip) for ip in (left_ip, right_ip)])


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["prepared", "committed"])
async def test_single_speaker_recovery_rejects_unsafe_pair_transaction(service, state):
    """Never roll back only one side of an undecided stereo transaction."""
    ip = "192.168.1.101"
    mock_group_store[ip] = StoredGroup(
        content="<new-stereo/>",
        backup="<original/>",
        journal={
            "job_id": "d" * 32,
            "operation": "create",
            "had_file": True,
            "state": state,
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
            "job_id": "e" * 32,
            "operation": "create",
            "had_file": True,
            "state": "verified",
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
    assert not any([await backend.leftover(ip) for ip in ips])


@pytest.mark.asyncio
async def test_service_recovers_mixed_verified_and_committed_as_completed_pair(service):
    """If one speaker is verified, preserve a pair verified on both speakers."""
    from opencloudtouch.stereo.group_files import build_group_xml

    backend = service.backend
    job_id = "a" * 32
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    for ip, right_file in ((left_ip, False), (right_ip, True)):
        xml = build_group_xml(
            "8765432",
            "Recovered",
            LEFT,
            left_ip,
            RIGHT,
            right_ip,
            right_file=right_file,
        )
        await backend.prepare(ip, job_id, "create", xml)
        await backend.commit(ip, job_id)
    await backend.mark_verified(left_ip, job_id)

    recovery = await service.recover(RIGHT)
    await service.tasks[recovery["job_id"]]
    assert service.job(recovery["job_id"])["state"] == "succeeded"
    assert len((await service.list_pairs())["pairs"]) == 1
    assert not any([await backend.leftover(ip) for ip in (left_ip, right_ip)])


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
    assert any([await backend.leftover(ip) for ip in ips])

    second = await service.recover(RIGHT)
    await service.tasks[second["job_id"]]
    assert service.job(second["job_id"])["state"] == "succeeded"
    assert all(mock_group_store[ip].content is None for ip in ips)
    assert not any([await backend.leftover(ip) for ip in ips])


@pytest.mark.asyncio
async def test_recovery_can_resume_after_interruption_between_cleanups(service):
    """Once both journals say restored, finishing either cleanup is safe."""

    class InterruptedCleanup(MockGroupBackend):
        def __init__(self):
            self.calls = 0

        async def cleanup(self, ip, job):
            self.calls += 1
            if self.calls == 2:
                raise OSError("crash during second cleanup")
            await super().cleanup(ip, job)

    backend = InterruptedCleanup()
    service.backend = backend
    job_id = "d" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)

    first = await service.recover(LEFT)
    await service.tasks[first["job_id"]]
    assert service.job(first["job_id"])["state"] == "failed"
    assert sum([await backend.leftover(ip) for ip in ips]) == 1

    remaining_id = LEFT if await backend.leftover(ips[0]) else RIGHT
    second = await service.recover(remaining_id)
    await service.tasks[second["job_id"]]
    assert service.job(second["job_id"])["state"] == "succeeded"
    assert not any([await backend.leftover(ip) for ip in ips])
    assert all(mock_group_store[ip].content is None for ip in ips)
    service.devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_pair_recovery_refuses_unreachable_peer_without_writes(service):
    """An unknown peer state must not cause unilateral restoration."""
    backend = service.backend
    job_id = "e" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        await backend.prepare(ip, job_id, "create", "<new-stereo/>")
        await backend.commit(ip, job_id)

    original_record = backend.journal_record

    async def failing_record(ip):
        if ip == ips[1]:
            raise OSError("speaker unavailable")
        return await original_record(ip)

    backend.journal_record = failing_record
    with pytest.raises(Exception) as exc:
        await service.recover(LEFT)
    assert getattr(exc.value, "code", None) == "device_unreachable"
    assert all(mock_group_store[ip].content == "<new-stereo/>" for ip in ips)
    assert all(mock_group_store[ip].journal is not None for ip in ips)
    assert not service.busy


@pytest.mark.asyncio
async def test_verified_cleanup_only_does_not_reboot_device(service):
    """A leftover verified marker does not require changing speaker state."""
    ip = "192.168.1.101"
    job_id = "f" * 32
    mock_group_store[ip] = StoredGroup(
        content="<existing/>",
        backup="<original/>",
        journal={
            "job_id": job_id,
            "operation": "create",
            "had_file": True,
            "state": "verified",
        },
    )
    recovery = await service.recover(LEFT)
    await service.tasks[recovery["job_id"]]
    assert service.job(recovery["job_id"])["state"] == "succeeded"
    assert mock_group_store[ip].content == "<existing/>"
    assert not await service.backend.leftover(ip)
    service.devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_pair_recovery_after_backup_deleted_but_journals_restored(service):
    """Resume cleanup after SSH loses connection between rm backup and rm journal."""
    backend = service.backend
    job_id = "9" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        mock_group_store[ip] = StoredGroup(
            content="<original/>",
            backup=None,  # Previous cleanup already removed the backup.
            journal={
                "job_id": job_id,
                "operation": "remove",
                "had_file": True,
                "state": "restored",
            },
        )

    recovery = await service.recover(LEFT)
    await service.tasks[recovery["job_id"]]
    assert service.job(recovery["job_id"])["state"] == "succeeded"
    assert not any([await backend.leftover(ip) for ip in ips])
    assert all(mock_group_store[ip].content == "<original/>" for ip in ips)
    service.devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_pair_survives_failure_marking_second_journal(service):
    """Once both firmware responses are verified, never roll back one side."""

    class FailingSecondVerificationMarker(MockGroupBackend):
        def __init__(self):
            self.mark_calls = 0

        async def mark_verified(self, ip, job):
            self.mark_calls += 1
            if self.mark_calls == 2:
                raise OSError("connection dropped on second verification marker")
            await super().mark_verified(ip, job)

    backend = FailingSecondVerificationMarker()
    service.backend = backend

    created = await service.create(LEFT, RIGHT, "Verified before journal error")
    await service.tasks[created["job_id"]]

    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    assert len((await service.list_pairs())["pairs"]) == 1
    ips = ("192.168.1.101", "192.168.1.103")
    assert all(mock_group_store[ip].content is not None for ip in ips)
    assert {mock_group_store[ip].journal["state"] for ip in ips} == {
        "verified",
        "committed",
    }

    recovered = await service.recover(RIGHT)
    await service.tasks[recovered["job_id"]]
    assert service.job(recovered["job_id"])["state"] == "succeeded"
    assert len((await service.list_pairs())["pairs"]) == 1
    assert not any([await backend.leftover(ip) for ip in ips])


@pytest.mark.asyncio
async def test_automatic_rollback_cleanup_failure_retains_restored_marker(service):
    """A normal failed create must leave a resumable decision if cleanup fails."""

    class FailingRollbackCleanup(MockGroupBackend):
        def __init__(self):
            self.cleanup_calls = 0

        async def commit(self, ip, job, *, removing=False):
            raise RuntimeError("stereo_write_failed")

        async def cleanup(self, ip, job):
            self.cleanup_calls += 1
            if self.cleanup_calls == 2:
                raise OSError("SSH disconnected while cleaning backup")
            await super().cleanup(ip, job)

    backend = FailingRollbackCleanup()
    service.backend = backend
    created = await service.create(LEFT, RIGHT, "Rollback marker")
    await service.tasks[created["job_id"]]
    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    assert result["error_code"] == "stereo_rollback_failed"

    remaining = [
        (ip, state)
        for ip, state in mock_group_store.items()
        if state.journal is not None
    ]
    assert len(remaining) == 1
    ip, state = remaining[0]
    assert state.journal["state"] == "restored"
    state.backup = None  # Cleanup may have removed backup before SSH failed.
    assert await backend.recover(ip)
    assert not await backend.leftover(ip)
    assert state.content is None


@pytest.mark.asyncio
async def test_stereo_pair_listing_limits_parallel_group_reads(service):
    """Spec #549 limits concurrent /getGroup discovery to five speakers."""
    import asyncio

    active = 0
    maximum = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    devices = [device(f"{i:012X}", f"192.0.2.{i + 1}") for i in range(12)]
    service.repo.get_all.return_value = devices

    async def simulated_group(device):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 5:
            entered.set()
        try:
            await release.wait()
            return None, True
        finally:
            active -= 1

    service._group_safe = simulated_group
    task = asyncio.create_task(service.list_pairs())
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert maximum == 5
    finally:
        release.set()
        await asyncio.wait_for(task, timeout=1)
    assert maximum == 5


@pytest.mark.asyncio
async def test_request_field_errors_take_priority_over_busy_lock(service):
    """Spec #549 requires same-device/name errors before lock/limit checks."""
    service.busy.add(LEFT)
    service.repo.get_by_device_id.reset_mock()
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, LEFT, "Good name")
    assert getattr(exc.value, "code", None) == "stereo_same_device"
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "")
    assert getattr(exc.value, "code", None) == "stereo_name_invalid"
    service.repo.get_by_device_id.assert_not_awaited()
    assert service.busy == {LEFT}
    assert not service.tasks


@pytest.mark.asyncio
async def test_both_models_validated_before_zone_or_group_queries(service):
    """An invalid RIGHT model wins over LEFT's later zone failure."""
    right = await service.repo.get_by_device_id(RIGHT)
    right.model = "SoundTouch 20"
    service.zones.get_zone_status.side_effect = RuntimeError("zone offline")
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Pair")
    assert getattr(exc.value, "code", None) == "stereo_model_unsupported"
    service.zones.get_zone_status.assert_not_awaited()
    assert not service.busy
    assert not service.tasks


@pytest.mark.asyncio
async def test_both_stored_addresses_validated_before_remote_queries(service):
    """Malformed RIGHT IP must not be masked by LEFT's offline zone service."""
    right = await service.repo.get_by_device_id(RIGHT)
    right.ip = "192.0.2.1; reboot"
    service.zones.get_zone_status.side_effect = RuntimeError("zone offline")
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Pair")
    assert getattr(exc.value, "code", None) == "stereo_device_invalid"
    service.zones.get_zone_status.assert_not_awaited()
    assert not service.busy
    assert not service.tasks


@pytest.mark.asyncio
async def test_group_safe_retries_transient_failures(service):
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service._group = AsyncMock(
        side_effect=[OSError("offline"), OSError("offline"), None]
    )
    with patch(
        "opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await service._group_safe(speaker) == (None, True)
    assert service._group.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_group_safe_reports_failure_after_three_attempts(service):
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service._group = AsyncMock(side_effect=OSError("offline"))
    with patch(
        "opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock
    ) as sleep:
        assert await service._group_safe(speaker) == (None, False)
    assert service._group.await_count == 3
    assert sleep.await_count == 2


@pytest.mark.asyncio
async def test_list_pairs_reports_unreachable_without_ssh_probe(service):
    from unittest.mock import AsyncMock

    service._group_safe = AsyncMock(return_value=(None, False))
    service.backend.leftover = AsyncMock()
    listing = await service.list_pairs()
    assert listing["pairs"] == []
    assert set(listing["unreachable_device_ids"]) == {LEFT, RIGHT}
    service.backend.leftover.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_pairs_ignores_unreadable_recovery_state(service):
    from unittest.mock import AsyncMock

    service._group_safe = AsyncMock(return_value=(None, True))
    service.backend.leftover = AsyncMock(side_effect=OSError("SSH lost"))
    listing = await service.list_pairs()
    assert listing["pairs"] == []
    assert listing["recovery_needed_device_ids"] == []
    assert listing["unreachable_device_ids"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [LEFT, RIGHT])
async def test_create_missing_device_returns_404_and_releases_locks(service, missing):
    original = service.repo.get_by_device_id.side_effect
    service.repo.get_by_device_id.side_effect = lambda key: (
        None if key == missing else original(key)
    )
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "device_not_found"
    assert not service.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [LEFT, RIGHT])
async def test_create_offline_group_query_reports_unreachable(service, side):
    from unittest.mock import AsyncMock

    async def group_status(speaker):
        if speaker.device_id == side:
            return None, False
        return None, True

    service._group_safe = AsyncMock(side_effect=group_status)
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "device_unreachable"
    assert not service.busy
    assert not service.tasks


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [LEFT, RIGHT])
async def test_create_zone_check_failure_reports_unreachable(service, side):
    async def get_zone(device_id):
        if device_id == side:
            raise OSError("zone API offline")
        return None

    service.zones.get_zone_status.side_effect = get_zone
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "device_unreachable"
    assert not service.busy


@pytest.mark.asyncio
async def test_create_rejects_second_device_recovery_artifacts(service):
    mock_group_store["192.168.1.103"] = StoredGroup(
        journal={"job_id": "a" * 32, "operation": "create", "had_file": False},
    )
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "stereo_recovery_needed"
    assert not service.busy


@pytest.mark.asyncio
async def test_create_rejects_busy_device_without_stealing_lock(service):
    service.busy.add(RIGHT)
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "stereo_job_in_progress"
    assert service.busy == {RIGHT}


@pytest.mark.asyncio
async def test_create_rejects_global_job_limit_without_reserving_devices(service):
    service.tasks = {"a": object(), "b": object()}
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "stereo_job_limit_reached"
    assert not service.busy


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [LEFT, RIGHT])
async def test_remove_detects_missing_persisted_member(service, missing):
    job = await service.create(LEFT, RIGHT, "Test")
    await service.tasks[job["job_id"]]
    group_id = (await service.list_pairs())["pairs"][0]["group_id"]
    original = service.repo.get_by_device_id.side_effect
    service.repo.get_by_device_id.side_effect = lambda key: (
        None if key == missing else original(key)
    )
    with pytest.raises(Exception) as exc:
        await service.remove(group_id)
    assert getattr(exc.value, "code", None) == "device_unreachable"
    assert not service.busy


@pytest.mark.asyncio
async def test_remove_rejects_unknown_valid_group(service):
    with pytest.raises(Exception) as exc:
        await service.remove("1234567")
    assert getattr(exc.value, "code", None) == "stereo_pair_not_found"
    assert not service.busy


@pytest.mark.asyncio
async def test_recover_without_journal_or_backup_reports_nothing_to_recover(service):
    with pytest.raises(Exception) as exc:
        await service.recover(LEFT)
    assert getattr(exc.value, "code", None) == "stereo_nothing_to_recover"
    assert not service.busy


@pytest.mark.asyncio
async def test_recover_missing_device_reports_404(service):
    with pytest.raises(Exception) as exc:
        await service.recover("FFFFFFFFFFFF")
    assert getattr(exc.value, "code", None) == "device_not_found"
    assert not service.busy


@pytest.mark.asyncio
async def test_reboot_dispatches_both_speakers_and_reports_failure(service):
    """A failed reboot of either speaker must fail the coordinated operation."""

    speakers = await service.repo.get_all()
    service.mock_mode = False
    await service._reboot(speakers)
    assert service.devices.reboot_device.await_count == 2

    service.devices.reboot_device.reset_mock()
    service.devices.reboot_device.side_effect = [None, OSError("telnet failed")]
    with pytest.raises(RuntimeError, match="stereo_reboot_failed"):
        await service._reboot(speakers)
    assert service.devices.reboot_device.await_count == 2


@pytest.mark.asyncio
async def test_reboot_wait_observes_offline_then_online_for_each_speaker(service):
    """A reboot is confirmed only after both devices disappear and return."""
    from unittest.mock import AsyncMock, patch

    speakers = await service.repo.get_all()
    service.mock_mode = False
    observations = {speaker.device_id: 0 for speaker in speakers}

    async def online(speaker):
        observations[speaker.device_id] += 1
        return observations[speaker.device_id] > 1

    service._info_online = AsyncMock(side_effect=online)
    with patch("opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock):
        await service._wait_online(speakers)
    assert observations == {speaker.device_id: 2 for speaker in speakers}


@pytest.mark.asyncio
async def test_reboot_wait_times_out_when_speaker_never_goes_offline(service):
    """A continuously answering /info must not count as a reboot."""
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service.mock_mode = False
    service._info_online = AsyncMock(return_value=True)
    with patch("opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="stereo_timeout"):
            await service._wait_online([speaker])
    assert service._info_online.await_count == 30


@pytest.mark.asyncio
async def test_reboot_wait_times_out_when_speaker_never_returns(service):
    """An unreachable speaker after reboot must be reported, not verified."""
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service.mock_mode = False
    service._info_online = AsyncMock(return_value=False)
    with patch("opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="stereo_timeout"):
            await service._wait_online([speaker])
    assert service._info_online.await_count == 37


@pytest.mark.asyncio
async def test_rollback_wait_allows_already_offline_speaker_to_return(service):
    """Rollback does not require a second offline transition."""
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service.mock_mode = False
    service._info_online = AsyncMock(side_effect=[False, True])
    with patch("opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock):
        await service._wait_reachable([speaker])
    assert service._info_online.await_count == 2


@pytest.mark.asyncio
async def test_rollback_wait_times_out_if_speaker_stays_unreachable(service):
    from unittest.mock import AsyncMock, patch

    speaker = await service.repo.get_by_device_id(LEFT)
    service.mock_mode = False
    service._info_online = AsyncMock(return_value=False)
    with patch("opencloudtouch.stereo.service.asyncio.sleep", new_callable=AsyncMock):
        with pytest.raises(RuntimeError, match="stereo_timeout"):
            await service._wait_reachable([speaker])
    assert service._info_online.await_count == 36


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [LEFT, RIGHT])
async def test_missing_device_error_contains_requested_device_id(service, missing):
    original = service.repo.get_by_device_id.side_effect
    service.repo.get_by_device_id.side_effect = lambda key: (
        None if key == missing else original(key)
    )
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "device_not_found"
    assert getattr(exc.value, "params", None) == {"device_id": missing}
    assert not service.busy


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", [False, OSError("connection refused"), TimeoutError("timeout")]
)
async def test_live_ssh_port_failure_returns_coded_503_without_writes(service, failure):
    from unittest.mock import AsyncMock, patch

    speakers = await service.repo.get_all()
    for speaker in speakers:
        speaker.ssh_permanent = True
    service.mock_mode = False
    service.backend.leftover = AsyncMock()
    with patch(
        "opencloudtouch.setup.ssh_client.check_ssh_port",
        new_callable=AsyncMock,
    ) as check:
        check.side_effect = [failure] if isinstance(failure, BaseException) else None
        check.return_value = failure if isinstance(failure, bool) else True
        with pytest.raises(Exception) as exc:
            await service.create(LEFT, RIGHT, "Test")
    assert getattr(exc.value, "code", None) == "stereo_ssh_unreachable"
    assert getattr(exc.value, "params", None) == {"device_id": LEFT}
    service.backend.leftover.assert_not_awaited()
    service.devices.reboot_device.assert_not_awaited()
    assert not service.busy


@pytest.mark.asyncio
async def test_write_failure_identifies_second_speaker(service):
    """A failed second commit is a write error, not an unclassified SSH error."""

    class FailsRightCommit(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            if ip == "192.168.1.103":
                raise OSError("simulated write interruption")
            await super().commit(ip, job, removing=removing)

    service.backend = FailsRightCommit()
    created = await service.create(LEFT, RIGHT, "Write failure")
    await service.tasks[created["job_id"]]
    result = service.job(created["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_write_failed"
    assert result["failed_device_id"] == RIGHT
    assert result["rolled_back"] is True
    assert all(state.content is None for state in mock_group_store.values())


@pytest.mark.asyncio
async def test_reboot_error_identifies_failing_speaker(service):
    from opencloudtouch.stereo.service import StereoDeviceFailure

    left = await service.repo.get_by_device_id(LEFT)
    right = await service.repo.get_by_device_id(RIGHT)

    async def reboot(device_id):
        if device_id == RIGHT:
            raise OSError("reboot unavailable")

    service.mock_mode = False
    service.devices.reboot_device.side_effect = reboot
    with pytest.raises(StereoDeviceFailure) as exc:
        await service._reboot([left, right])
    assert str(exc.value) == "stereo_reboot_failed"
    assert exc.value.device_id == RIGHT


@pytest.mark.asyncio
async def test_verification_failure_identifies_speaker(service):
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole
    from opencloudtouch.stereo.service import StereoDeviceFailure

    left = await service.repo.get_by_device_id(LEFT)
    right = await service.repo.get_by_device_id(RIGHT)
    roles = [
        GroupRole(LEFT, "LEFT", left.ip),
        GroupRole(RIGHT, "RIGHT", right.ip),
    ]

    async def group_safe(device):
        return (
            GroupInfo(
                "1234567" if device.device_id == LEFT else "9999999",
                "Pair",
                LEFT,
                roles,
                "GROUP_OK",
            ),
            True,
        )

    service._group_safe = group_safe
    with pytest.raises(StereoDeviceFailure) as exc:
        await service._verify([left, right], "1234567")
    assert str(exc.value) == "stereo_verify_failed"
    assert exc.value.device_id == RIGHT


@pytest.mark.asyncio
async def test_list_pairs_uses_master_report_even_if_slave_responds_first(service):
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole

    left = await service.repo.get_by_device_id(LEFT)
    right = await service.repo.get_by_device_id(RIGHT)
    service.repo.get_all.return_value = [right, left]
    roles = [GroupRole(LEFT, "LEFT", left.ip), GroupRole(RIGHT, "RIGHT", right.ip)]

    async def group_safe(device):
        return (
            GroupInfo(
                "1234567",
                "Master name" if device.device_id == LEFT else "Old name",
                LEFT,
                roles,
                "GROUP_OK",
            ),
            True,
        )

    service._group_safe = group_safe
    result = await service.list_pairs()
    assert result["pairs"][0]["name"] == "Master name"
    assert result["pairs"][0]["inconsistent"] is True


@pytest.mark.asyncio
async def test_busy_device_error_contains_device_id(service):
    service.busy.add(RIGHT)
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Busy")
    error = exc.value
    assert getattr(error, "code", None) == "stereo_job_in_progress"
    assert error.params == {"device_id": RIGHT}


@pytest.mark.asyncio
async def test_recover_pair_with_unfinished_second_preparation(service):
    """An absent backup on a preparing peer must not block paired recovery."""
    backend = service.backend
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    job_id = "8" * 32
    from opencloudtouch.stereo.group_files import build_group_xml

    staged = build_group_xml(
        "1234567",
        "Interrupted",
        LEFT,
        left_ip,
        RIGHT,
        right_ip,
        right_file=False,
    )
    # LEFT was committed; RIGHT crashed immediately after writing its journal
    # and before creating a backup or stage file. Its original file is intact.
    await backend.prepare(left_ip, job_id, "create", staged)
    await backend.commit(left_ip, job_id)
    mock_group_store[right_ip] = StoredGroup(
        content=None,
        journal={
            "job_id": job_id,
            "operation": "create",
            "had_file": False,
            "state": "preparing",
        },
        backup=None,
        staged=None,
    )

    recovery = await service.recover(RIGHT)
    await service.tasks[recovery["job_id"]]
    result = service.job(recovery["job_id"])
    assert result["state"] == "succeeded", result
    assert mock_group_store[left_ip].content is None
    assert mock_group_store[right_ip].content is None
    assert not await backend.leftover(left_ip)
    assert not await backend.leftover(right_ip)


@pytest.mark.asyncio
async def test_recover_pair_preparing_peer_with_original_file(service):
    """A preparing peer's original must be read from the untouched file."""
    backend = service.backend
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    job_id = "9" * 32
    original = "<group />"
    mock_group_store[right_ip] = StoredGroup(
        content=original,
        journal={
            "job_id": job_id,
            "operation": "remove",
            "had_file": True,
            "state": "preparing",
        },
    )
    mock_group_store[left_ip] = StoredGroup(content=original)
    await backend.prepare(left_ip, job_id, "remove", None)
    await backend.commit(left_ip, job_id)
    recovery = await service.recover(RIGHT)
    await service.tasks[recovery["job_id"]]
    result = service.job(recovery["job_id"])
    assert result["state"] == "succeeded", result
    assert mock_group_store[left_ip].content == original
    assert mock_group_store[right_ip].content == original
    assert not await backend.leftover(left_ip)
    assert not await backend.leftover(right_ip)


@pytest.mark.asyncio
async def test_force_remove_when_peer_missing_from_repository(service):
    """force=true removes the file on the surviving registered speaker."""
    job = await service.create(LEFT, RIGHT, "Surviving speaker")
    await service.tasks[job["job_id"]]
    group_id = service.job(job["job_id"])["group_id"]
    original_get = service.repo.get_by_device_id.side_effect
    surviving = await service.repo.get_by_device_id(LEFT)
    service.repo.get_all.return_value = [surviving]
    service.repo.get_by_device_id.side_effect = lambda key: (
        None if key == RIGHT else original_get(key)
    )
    with pytest.raises(Exception) as exc:
        await service.remove(group_id, force=False)
    assert getattr(exc.value, "code", None) == "device_unreachable"

    removed = await service.remove(group_id, force=True)
    await service.tasks[removed["job_id"]]
    result = service.job(removed["job_id"])
    assert result["state"] == "succeeded", result
    assert mock_group_store["192.168.1.101"].content is None
    assert mock_group_store["192.168.1.103"].content is not None


@pytest.mark.asyncio
async def test_injected_backend_and_fake_sleep_are_used(service):
    """The spec's fake backend and clock/sleep hooks work without real time."""
    from opencloudtouch.stereo.jobs import phase_budget

    backend = MockGroupBackend()
    elapsed = [100.0]
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        elapsed[0] += seconds

    injected = StereoService(
        service.repo,
        service.devices,
        service.zones,
        mock_mode=True,
        backend_factory=lambda: backend,
        clock=lambda: elapsed[0],
        sleep=fake_sleep,
        now=lambda: "2026-01-01T00:00:00Z",
    )
    assert injected.backend is backend
    assert injected._tasks is injected.tasks
    await injected._sleep(2.5)
    assert calls == [2.5]
    assert elapsed[0] == 102.5

    with pytest.raises(RuntimeError, match="stereo_timeout"):
        async with phase_budget(1, "stereo_timeout", clock=lambda: elapsed[0]):
            await injected._sleep(2)

    created = await injected.create(LEFT, RIGHT, "Injected")
    await injected.tasks[created["job_id"]]
    assert injected.job(created["job_id"])["state"] == "succeeded"
    assert injected.job(created["job_id"])["started_at"] == "2026-01-01T00:00:00Z"
    assert injected.job(created["job_id"])["finished_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_pair_list_ignores_equivalent_role_element_order(service):
    """XML role element order is not part of pair membership identity."""
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole

    left = await service.repo.get_by_device_id(LEFT)
    right = await service.repo.get_by_device_id(RIGHT)
    roles = [
        GroupRole(LEFT, "LEFT", left.ip),
        GroupRole(RIGHT, "RIGHT", right.ip),
    ]

    async def group_safe(device):
        device_roles = roles if device.device_id == LEFT else list(reversed(roles))
        return GroupInfo("1234567", "Stereo", LEFT, device_roles, "GROUP_OK"), True

    service._group_safe = group_safe
    pair = (await service.list_pairs())["pairs"][0]
    assert pair["partial"] is False
    assert pair["inconsistent"] is False



@pytest.mark.asyncio
async def test_reboot_timeout_restores_survivor_and_keeps_both_journals(service):
    """A speaker that never returns must not prevent restoring its peer."""
    from opencloudtouch.stereo.group_files import build_group_xml

    backend = service.backend
    left_ip, right_ip = "192.168.1.101", "192.168.1.103"
    job_id = "6" * 32
    for ip, right_file in ((left_ip, False), (right_ip, True)):
        content = build_group_xml(
            "1234567", "Temporary", LEFT, left_ip, RIGHT, right_ip,
            right_file=right_file,
        )
        await backend.prepare(ip, job_id, "create", content)
        await backend.commit(ip, job_id)

    original_group_safe = service._group_safe

    async def speaker_status(device):
        if device.device_id == RIGHT:
            return None, False
        return await original_group_safe(device)

    service._group_safe = speaker_status
    original_leftover = backend.leftover

    async def ssh_leftover(ip):
        if ip == right_ip:
            raise OSError("RIGHT SSH not reachable")
        return await original_leftover(ip)

    backend.leftover = ssh_leftover
    service._reboot = AsyncMock()
    service._wait_reachable = AsyncMock()
    job = {"job_id": job_id, "step": "waiting_for_devices",
           "failed_device_id": RIGHT}

    result = await service._rollback_job(
        job,
        [await service.repo.get_by_device_id(LEFT),
         await service.repo.get_by_device_id(RIGHT)],
        committed=True, reboot_started=True,
        previous={LEFT: None, RIGHT: None}, cancelled=False,
    )

    assert result is False
    assert mock_group_store[left_ip].content is None
    assert mock_group_store[left_ip].journal["state"] == "restored"
    assert mock_group_store[right_ip].content is not None
    assert mock_group_store[right_ip].journal["state"] == "committed"
    assert await original_leftover(left_ip)
    assert await original_leftover(right_ip)
    service._reboot.assert_awaited_once()
    rebooted = service._reboot.await_args.args[0]
    assert [device.device_id for device in rebooted] == [LEFT]
    assert service._wait_reachable.await_count == 1



@pytest.mark.asyncio
async def test_existing_pair_validation_precedes_unreachable_peer(service):
    """The pair-in-use code takes priority over an offline second device."""
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole

    async def status(device):
        if device.device_id == LEFT:
            return GroupInfo(
                "1234567", "Existing", LEFT,
                [GroupRole(LEFT, "LEFT", device.ip)], "GROUP_OK",
            ), True
        return None, False

    service._group_safe = status
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Another stereo")
    assert getattr(exc.value, "code", None) == "stereo_device_in_pair"
    assert not service.busy
    service.devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_trailing_newline_name_rejected_before_remote_access(service):
    """Trimming may not silently normalize an invalid control character."""
    with pytest.raises(Exception) as exc:
        await service.create(LEFT, RIGHT, "Pair\\n")
    assert getattr(exc.value, "code", None) == "stereo_name_invalid"
    service.repo.get_by_device_id.assert_not_awaited()
    assert not service.busy



@pytest.mark.asyncio
async def test_shutdown_after_first_commit_restores_files_and_retains_journal(service):
    """Cancellation restores a committed file before the job releases its lock."""
    import asyncio

    committed = asyncio.Event()

    class PauseAfterFirstCommit(MockGroupBackend):
        async def commit(self, ip, job, *, removing=False):
            await super().commit(ip, job, removing=removing)
            if ip == "192.168.1.101":
                committed.set()
                await asyncio.Event().wait()

    service.backend = PauseAfterFirstCommit()
    created = await service.create(LEFT, RIGHT, "Cancel after commit")
    await asyncio.wait_for(committed.wait(), timeout=1)
    await service.shutdown()
    await asyncio.sleep(0)
    result = service.job(created["job_id"])

    assert result["state"] == "failed"
    assert result["rolled_back"] is False
    assert all(s.content is None for s in mock_group_store.values())
    assert all(s.journal is not None for s in mock_group_store.values())
    assert not service.tasks
    assert not service.busy



@pytest.mark.asyncio
async def test_pair_list_detects_conflicting_role_ip(service):
    """Roles include their reported IP addresses, not only identity/side."""
    from opencloudtouch.stereo.group_files import GroupInfo, GroupRole

    left = await service.repo.get_by_device_id(LEFT)
    right = await service.repo.get_by_device_id(RIGHT)
    original = [
        GroupRole(LEFT, "LEFT", left.ip),
        GroupRole(RIGHT, "RIGHT", right.ip),
    ]
    conflicting = [
        GroupRole(RIGHT, "RIGHT", "192.0.2.200"),
        GroupRole(LEFT, "LEFT", left.ip),
    ]

    async def group_safe(device):
        roles = original if device.device_id == LEFT else conflicting
        return GroupInfo("1234567", "Stereo", LEFT, roles, "GROUP_OK"), True

    service._group_safe = group_safe
    pair = (await service.list_pairs())["pairs"][0]
    assert pair["inconsistent"] is True
    assert pair["partial"] is False



@pytest.mark.asyncio
async def test_http_offline_peer_still_receives_ssh_file_rollback(service):
    """A failed /getGroup probe must not suppress an available SSH restore."""
    backend = service.backend
    job_id = "7" * 32
    ips = ("192.168.1.101", "192.168.1.103")
    for ip in ips:
        await backend.prepare(ip, job_id, "create", "<new-group/>")
        await backend.commit(ip, job_id)

    original_group_safe = service._group_safe

    async def group_safe(device):
        if device.device_id == RIGHT:
            return None, False
        return await original_group_safe(device)

    service._group_safe = group_safe
    service._reboot = AsyncMock()
    service._wait_reachable = AsyncMock()
    job = {"job_id": job_id, "step": "waiting_for_devices",
           "failed_device_id": RIGHT}

    fully_restored = await service._rollback_job(
        job,
        [await service.repo.get_by_device_id(LEFT),
         await service.repo.get_by_device_id(RIGHT)],
        committed=True, reboot_started=True,
        previous={LEFT: None, RIGHT: None}, cancelled=False,
    )

    assert fully_restored is False
    assert all(mock_group_store[ip].content is None for ip in ips)
    assert mock_group_store[ips[0]].journal["state"] == "restored"
    assert mock_group_store[ips[1]].journal["state"] == "committed"
    assert all([await backend.leftover(ip) for ip in ips])
    rebooted = service._reboot.await_args.args[0]
    assert [d.device_id for d in rebooted] == [LEFT]
