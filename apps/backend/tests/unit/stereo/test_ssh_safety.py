"""Regression coverage for stereo SSH remount and interrupted backup handling."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opencloudtouch.stereo.group_backend import (
    MockGroupBackend,
    SSHGroupBackend,
    StoredGroup,
    mock_group_store,
)


@pytest.mark.asyncio
async def test_ssh_write_access_restored_on_command_failure():
    events = []

    class FakeSSH:
        def __init__(self, host):
            self.host = host

        async def connect(self):
            return SimpleNamespace(success=True)

        @asynccontextmanager
        async def write_access(self):
            events.append("remount-rw")
            try:
                yield
            finally:
                events.append("restore-mount-state")

        async def execute(self, command):
            events.append(command)
            if command.startswith("test -d"):
                return SimpleNamespace(success=True, output="")
            return SimpleNamespace(success=False, output="", error="simulated write error")

        async def close(self):
            events.append("close")

    with patch("opencloudtouch.setup.ssh_client.SoundTouchSSHClient", FakeSSH):
        backend = SSHGroupBackend()
        with pytest.raises(OSError, match="SSH command failed"):
            await backend._run("192.0.2.10", ["false"], writable=True)

    assert events[0] == "remount-rw"
    assert events[-2:] == ["restore-mount-state", "close"]


@pytest.mark.asyncio
async def test_read_only_ssh_operation_does_not_remount():
    events = []

    class FakeSSH:
        def __init__(self, host):
            pass

        async def connect(self):
            return SimpleNamespace(success=True)

        @asynccontextmanager
        async def write_access(self):
            events.append("unexpected remount")
            yield

        async def execute(self, command):
            return SimpleNamespace(success=True, output="ok")

        async def close(self):
            pass

    with patch("opencloudtouch.setup.ssh_client.SoundTouchSSHClient", FakeSSH):
        result = await SSHGroupBackend()._run("192.0.2.10", ["echo ok"])

    assert result == ["ok"]
    assert events == []


@pytest.mark.asyncio
async def test_interrupted_preparing_journal_does_not_restore_missing_backup():
    mock_group_store.clear()
    ip = "192.0.2.10"
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
    assert await backend.recover(ip) is True
    assert mock_group_store[ip].content == "<original/>"
    assert not await backend.leftover(ip)
    mock_group_store.clear()


@pytest.mark.asyncio
async def test_ssh_preparation_publishes_prepared_only_after_backup():
    commands = []

    class RecordingBackend(SSHGroupBackend):
        async def leftover(self, ip):
            return False

        async def _run(self, ip, cmds, *, writable=False):
            commands.append((list(cmds), writable))
            if not writable:
                return ["yes"]
            return [""] * len(cmds)

    backend = RecordingBackend()
    await backend.prepare("192.0.2.10", "a" * 32, "create", "<group/>")

    assert commands[1][1] is True
    assert commands[1][0][0][0].endswith(".oct-job.tmp")
    assert commands[1][0][-1].startswith("mv ")
    final_commands = commands[2][0]
    assert any(isinstance(cmd, str) and cmd.startswith("cp ") for cmd in final_commands)
    assert final_commands[-1].startswith("mv ")
    assert final_commands[-1].endswith(".oct-job.tmp /mnt/nv/BoseApp-Persistence/1/GroupService.xml.oct-job")
    assert all(writable for _, writable in commands[1:])


@pytest.mark.asyncio
async def test_ssh_preparation_backup_failure_keeps_preparing_journal():
    writes = []

    class FailingBackend(SSHGroupBackend):
        async def leftover(self, ip):
            return False

        async def _run(self, ip, cmds, *, writable=False):
            if not writable:
                return ["yes"]
            writes.append(list(cmds))
            if len(writes) == 2:
                raise OSError("backup copy failed")
            return [""] * len(cmds)

    with pytest.raises(OSError, match="backup copy failed"):
        await FailingBackend().prepare("192.0.2.10", "a" * 32, "create", "<group/>")
    assert len(writes) == 2
    # Only the initial journal is durable; the prepared state was not published.
    assert writes[0][0][0].endswith(".oct-job.tmp")
    assert writes[0][-1].startswith("mv ")



@pytest.mark.asyncio
async def test_orphan_initial_journal_upload_is_cleaned_without_touching_group():
    commands = []

    class InterruptedUpload(SSHGroupBackend):
        async def _run(self, ip, cmds, *, writable=False):
            commands.extend(cmds)
            if any("if test -f" in str(cmd) for cmd in cmds):
                return [""]
            if any("if ls " in str(cmd) for cmd in cmds):
                return ["pending"]
            return [""]

    backend = InterruptedUpload()
    assert await backend.recover("192.0.2.10") is True
    assert any("rm -f" in str(command) and ".oct-job.tmp" in str(command)
               for command in commands)
    assert not any(str(command).startswith("rm -f /mnt/nv/BoseApp-Persistence/1/GroupService.xml ")
                   for command in commands)


@pytest.mark.asyncio
async def test_orphan_with_other_files_needs_manual_recovery():
    class UnsafeUpload(SSHGroupBackend):
        async def _run(self, ip, cmds, *, writable=False):
            if any("if test -f" in str(cmd) for cmd in cmds):
                return [""]
            if any("if ls " in str(cmd) for cmd in cmds):
                return ["unsafe"]
            raise AssertionError("Unexpected modification")

    with pytest.raises(RuntimeError, match="stereo_recovery_needed"):
        await UnsafeUpload().recover("192.0.2.10")


@pytest.mark.asyncio
async def test_ssh_transfer_disconnect_restores_mount_and_closes_connection():
    """A failed base64 transfer must not leave the writable mount enabled."""
    events = []

    class FailedSession:
        async def run(self, command, *, input, check):
            events.append(("transfer", command))
            raise OSError("connection closed during upload")

    class FakeSSH:
        def __init__(self, host):
            self._connection = FailedSession()

        async def connect(self):
            return SimpleNamespace(success=True)

        @asynccontextmanager
        async def write_access(self):
            events.append("remount-rw")
            try:
                yield
            finally:
                events.append("restore-mount-state")

        async def execute(self, command):
            events.append(("command", command))
            return SimpleNamespace(success=True, output="")

        async def close(self):
            events.append("close")

    with patch("opencloudtouch.setup.ssh_client.SoundTouchSSHClient", FakeSSH):
        with pytest.raises(OSError, match="connection closed during upload"):
            await SSHGroupBackend()._run(
                "192.0.2.10",
                [SSHGroupBackend._write("/tmp/oct-stage", "payload")],
                writable=True,
            )

    assert events[-2:] == ["restore-mount-state", "close"]
    assert sum(event == "restore-mount-state" for event in events) == 1


@pytest.mark.asyncio
async def test_ssh_recovery_state_update_uses_atomic_journal_rename():
    """A failed upload cannot replace the previously durable journal."""
    import json

    journal = {
        "job_id": "a" * 32,
        "operation": "create",
        "had_file": False,
        "state": "committed",
    }
    calls = []

    class RecordingBackend(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            calls.append((list(commands), writable))
            if not writable:
                return [json.dumps(journal)]
            return [""] * len(commands)

    await RecordingBackend().mark_restored("192.0.2.10", "a" * 32)
    assert calls[0][1] is False
    assert calls[1][1] is True
    upload, rename = calls[1][0]
    assert isinstance(upload, tuple)
    assert upload[0].endswith(".oct-new." + "a" * 32)
    assert '"restored"' in __import__("base64").b64decode(upload[1]).decode()
    assert rename.startswith("mv ")
    assert rename.endswith(".oct-job")


@pytest.mark.asyncio
async def test_ssh_missing_backup_blocks_rollback_before_file_changes():
    """Never erase a current group when an expected original backup is lost."""
    import json

    job = "b" * 32
    journal = json.dumps({
        "job_id": job, "operation": "remove",
        "had_file": True, "state": "committed",
    })
    calls = []

    class MissingBackup(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            calls.extend(commands)
            if any(str(cmd).startswith("cat ") for cmd in commands):
                return [journal]
            if any(str(cmd).startswith("test -s ") for cmd in commands):
                raise OSError("SSH command failed")
            raise AssertionError("Unexpected file modification")

    with pytest.raises(OSError, match="SSH command failed"):
        await MissingBackup().rollback("192.0.2.10", job)
    assert not any(str(cmd).startswith(("cp ", "rm -f ", "mv "))
                   for cmd in calls)


@pytest.mark.asyncio
async def test_ssh_invalid_journal_does_not_trigger_recovery_writes():
    """Malformed durable transaction evidence must fail closed."""
    class CorruptJournal(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            assert not writable
            return ['{"state":"committed","job_id":"bad","had_file":true}']

    with pytest.raises(RuntimeError, match="stereo_recovery_needed"):
        await CorruptJournal().journal_record("192.0.2.10")
