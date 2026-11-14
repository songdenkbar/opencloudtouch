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
    assert commands[1][0][0][0].endswith(".oct-job")
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
    assert writes[0][0][0].endswith(".oct-job")
    assert not any("mv " in str(cmd) for cmd in writes[0])
