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
    executed = []

    class MissingBackup(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            outputs = []
            # _run executes commands in order. A failed precondition prevents
            # later commands in the same batch from actually executing.
            for command in commands:
                if str(command).startswith("test -s "):
                    executed.append(command)
                    raise OSError("SSH command failed")
                if str(command).startswith(("cp ", "rm -f ", "mv ")):
                    raise AssertionError("Unexpected file modification")
                executed.append(command)
                if str(command).startswith("cat "):
                    outputs.append(journal)
                else:
                    outputs.append("")
            return outputs

    with pytest.raises(OSError, match="SSH command failed"):
        await MissingBackup().rollback("192.0.2.10", job)
    assert any(str(command).startswith("test -s ") for command in executed)
    assert not any(str(command).startswith(("cp ", "rm -f ", "mv "))
                   for command in executed)


@pytest.mark.asyncio
async def test_ssh_invalid_journal_does_not_trigger_recovery_writes():
    """Malformed durable transaction evidence must fail closed."""
    class CorruptJournal(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            assert not writable
            return ['{"state":"committed","job_id":"bad","had_file":true}']

    with pytest.raises(RuntimeError, match="stereo_recovery_needed"):
        await CorruptJournal().journal_record("192.0.2.10")


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_at", [1, 2])
async def test_ssh_cleanup_interruption_preserves_journal_and_can_retry(fail_at):
    """Cleanup is resumable if the first or final removal command fails."""
    import json

    job = "c" * 32
    backend = SSHGroupBackend()
    journal, backup, stage = backend._suffix(job)
    original_group = "<group id='123'/>"
    from opencloudtouch.stereo.group_files import GROUP_FILE
    files = {
        GROUP_FILE: original_group,
        journal: json.dumps({
            "job_id": job, "operation": "create",
            "had_file": False, "state": "verified",
        }),
        backup: "<old/>",
        stage: "staged",
        journal + ".tmp": "temporary",
    }
    removal_count = 0

    class InterruptedCleanup(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            nonlocal removal_count
            output = []
            for command in commands:
                if command.startswith("cat "):
                    output.append(files[command[4:]])
                elif command.startswith("rm -f "):
                    removal_count += 1
                    if removal_count == fail_at:
                        raise OSError("simulated SSH interruption")
                    for pathname in command[len("rm -f "):].split():
                        files.pop(pathname, None)
                    output.append("")
                else:
                    raise AssertionError("Unexpected SSH command")
            return output

    interrupted = InterruptedCleanup()
    with pytest.raises(OSError, match="simulated SSH interruption"):
        await interrupted.cleanup("192.0.2.10", job)
    assert journal in files, "Journal must be the last artifact removed"

    # The real group file is not part of cleanup, even on a retry.
    assert files[GROUP_FILE] == original_group
    await interrupted.cleanup("192.0.2.10", job)
    assert files == {GROUP_FILE: original_group}


@pytest.mark.asyncio
async def test_ssh_cleanup_command_order_keeps_journal_last():
    """The durable journal is removed in a separate, last SSH command."""
    import json

    job = "d" * 32
    journal, backup, stage = SSHGroupBackend._suffix(job)
    operations = []

    class RecordingBackend(SSHGroupBackend):
        async def _run(self, ip, commands, *, writable=False):
            operations.append((list(commands), writable))
            if not writable:
                return [json.dumps({"job_id": job})]
            return [""] * len(commands)

    await RecordingBackend().cleanup("192.0.2.10", job)
    commands, writable = operations[1]
    assert writable
    assert commands == [
        f"rm -f {backup} {stage} {journal}.tmp",
        f"rm -f {journal}",
    ]
