"""On-speaker transactional file handling for SoundTouch stereo configuration."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Protocol

from opencloudtouch.stereo.group_files import GROUP_FILE, GROUP_DIRECTORY

_JOB = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class StoredGroup:
    content: str | None = None
    journal: dict | None = None
    backup: str | None = None
    staged: str | None = None


# Shared by mock client and mock group backend. Reset between test runs.
mock_group_store: dict[str, StoredGroup] = {}


class GroupBackend(Protocol):
    async def leftover(self, ip: str) -> bool: ...
    async def prepare(self, ip: str, job: str, operation: str, new: str | None) -> None: ...
    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None: ...
    async def rollback(self, ip: str, job: str) -> None: ...
    async def cleanup(self, ip: str, job: str) -> None: ...
    async def recover(self, ip: str) -> bool: ...


class MockGroupBackend:
    """Safe fake for CI and local development."""
    async def leftover(self, ip: str) -> bool:
        state = mock_group_store.get(ip)
        return bool(state and (state.journal or state.backup or state.staged))

    async def prepare(self, ip: str, job: str, operation: str, new: str | None) -> None:
        state = mock_group_store.setdefault(ip, StoredGroup())
        if await self.leftover(ip):
            raise RuntimeError("stereo_recovery_needed")
        state.journal = {
            "job_id": job, "operation": operation, "had_file": state.content is not None,
            "state": "prepared",
        }
        state.backup = state.content
        state.staged = new

    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None:
        state = mock_group_store[ip]
        if not state.journal or state.journal["job_id"] != job:
            raise RuntimeError("stereo_ssh_failed")
        state.content = None if removing else state.staged
        state.journal["state"] = "committed"

    async def rollback(self, ip: str, job: str) -> None:
        state = mock_group_store[ip]
        if not state.journal or state.journal["job_id"] != job:
            raise RuntimeError("stereo_recovery_needed")
        state.content = state.backup if state.journal["had_file"] else None
        state.staged = None

    async def cleanup(self, ip: str, job: str) -> None:
        state = mock_group_store[ip]
        state.journal = None
        state.backup = None
        state.staged = None

    async def recover(self, ip: str) -> bool:
        state = mock_group_store.get(ip)
        if not state or not state.journal:
            return False
        await self.rollback(ip, state.journal["job_id"])
        await self.cleanup(ip, state.journal["job_id"])
        return True


class SSHGroupBackend:
    """On-device journal and backup; never overwrite an un-recovered transaction."""
    @staticmethod
    def _suffix(job: str) -> tuple[str, str, str]:
        if not _JOB.fullmatch(job):
            raise ValueError("Invalid job identifier")
        return (
            GROUP_FILE + ".oct-job",
            GROUP_FILE + ".oct-bak." + job,
            GROUP_FILE + ".oct-new." + job,
        )

    async def _run(self, ip: str, commands: list[str | tuple[str, str]]) -> list[str]:
        from opencloudtouch.setup.ssh_client import SoundTouchSSHClient
        ssh = SoundTouchSSHClient(ip)
        connection = await ssh.connect()
        if not connection.success:
            raise OSError("SSH connection failed")
        output = []
        try:
            for command in commands:
                if isinstance(command, tuple):
                    # Payload is sent via stdin, never included in the command or logs.
                    shell, payload = command
                    connection = ssh._connection
                    if connection is None:
                        raise OSError("SSH connection closed")
                    result = await connection.run(shell, input=payload, check=False)
                    if result.exit_status != 0:
                        raise OSError("SSH transfer failed")
                    output.append((result.stdout or "").strip())
                else:
                    result = await ssh.execute(command)
                    if not result.success:
                        raise OSError("SSH command failed")
                    output.append(result.output.strip())
            return output
        finally:
            await ssh.close()

    @staticmethod
    def _write(path: str, content: str) -> tuple[str, str]:
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        return (f"base64 -d > {path}", encoded)

    async def leftover(self, ip: str) -> bool:
        # glob is resolved by shell. Existence of any artifact blocks new operations.
        output = await self._run(ip, [
            f"if test -e {GROUP_FILE}.oct-job || "
            f"ls {GROUP_FILE}.oct-bak.* {GROUP_FILE}.oct-new.* >/dev/null 2>&1; "
            "then echo yes; else echo no; fi"
        ])
        return output[0] == "yes"

    async def prepare(self, ip: str, job: str, operation: str, new: str | None) -> None:
        journal, backup, stage = self._suffix(job)
        if await self.leftover(ip):
            raise RuntimeError("stereo_recovery_needed")
        # Write journal FIRST; if interrupted, recovery remains possible.
        had_file = (await self._run(ip, [
            f"if test -f {GROUP_FILE}; then echo yes; else echo no; fi"
        ]))[0] == "yes"
        data = json.dumps({
            "job_id": job, "operation": operation, "had_file": had_file,
            "state": "prepared",
        }, separators=(",", ":"))
        commands = [self._write(journal, data)]
        if had_file:
            commands.append(f"cp {GROUP_FILE} {backup}")
        if new is not None:
            commands.append(self._write(stage, new))
            commands.append(f"test -s {stage}")
        await self._run(ip, commands)

    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None:
        _, _, stage = self._suffix(job)
        command = f"rm -f {GROUP_FILE}" if removing else f"mv {stage} {GROUP_FILE}"
        await self._run(ip, [command])

    async def rollback(self, ip: str, job: str) -> None:
        journal, backup, stage = self._suffix(job)
        data = (await self._run(ip, [f"cat {journal}"]))[0]
        record = json.loads(data)
        if record.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        if record.get("had_file") is True:
            await self._run(ip, [f"test -f {backup}", f"cp {backup} {GROUP_FILE}"])
        elif record.get("had_file") is False:
            await self._run(ip, [f"rm -f {GROUP_FILE}"])
        else:
            raise RuntimeError("stereo_recovery_needed")
        await self._run(ip, [f"rm -f {stage}"])

    async def cleanup(self, ip: str, job: str) -> None:
        journal, backup, stage = self._suffix(job)
        await self._run(ip, [f"rm -f {backup} {stage} {journal}"])

    async def recover(self, ip: str) -> bool:
        data = (await self._run(ip, [
            f"if test -f {GROUP_FILE}.oct-job; then cat {GROUP_FILE}.oct-job; fi"
        ]))[0]
        if not data:
            return False
        record = json.loads(data)
        job = record["job_id"]
        self._suffix(job)
        await self.rollback(ip, job)
        await self.cleanup(ip, job)
        return True
