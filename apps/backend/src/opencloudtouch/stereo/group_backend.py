"""On-speaker transactional file handling for SoundTouch stereo configuration."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from contextlib import nullcontext
from typing import TYPE_CHECKING, Protocol, cast
from collections.abc import Sequence

if TYPE_CHECKING:
    from asyncssh import SSHClientConnection

from opencloudtouch.stereo.group_files import GROUP_DIRECTORY, GROUP_FILE

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
    async def mark_verified(self, ip: str, job: str) -> None: ...
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
            "state": "preparing",
        }
        state.backup = state.content
        state.staged = new
        state.journal["state"] = "prepared"

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

    async def mark_verified(self, ip: str, job: str) -> None:
        state = mock_group_store[ip]
        if state.journal is None or state.journal.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        state.journal["state"] = "verified"

    async def cleanup(self, ip: str, job: str) -> None:
        state = mock_group_store[ip]
        if state.journal is None or state.journal.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        state.journal = None
        state.backup = None
        state.staged = None

    async def recover(self, ip: str) -> bool:
        state = mock_group_store.get(ip)
        if not state or not state.journal:
            return False
        if state.journal.get("state") not in {"verified", "preparing"}:
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

    async def _run(
        self, ip: str, commands: Sequence[str | tuple[str, str]], *, writable: bool = False
    ) -> list[str]:
        from opencloudtouch.setup.ssh_client import SoundTouchSSHClient

        ssh = SoundTouchSSHClient(ip)
        connected = await ssh.connect()
        if not connected.success:
            raise OSError("SSH connection failed")
        output: list[str] = []
        try:
            async with (ssh.write_access() if writable else nullcontext()):
                if writable:
                    # write_access remounts read-only / and /mnt/nv, then restores
                    # their prior state in finally; verify the target is usable.
                    check = await ssh.execute(f"test -d {GROUP_DIRECTORY} && test -w {GROUP_DIRECTORY}")
                    if not check.success:
                        raise OSError("Stereo persistence directory not writable")
                for command in commands:
                    if isinstance(command, tuple):
                        # Payload travels via SSH stdin, not the command/logs.
                        shell, payload = command
                        session = cast("SSHClientConnection | None", ssh._connection)
                        if session is None:
                            raise OSError("SSH connection closed")
                        process = await session.run(shell, input=payload, check=False)
                        if process.exit_status != 0:
                            raise OSError("SSH transfer failed")
                        stdout = process.stdout or ""
                        output.append(
                            stdout.decode("utf-8").strip()
                            if isinstance(stdout, bytes) else stdout.strip()
                        )
                    else:
                        command_result = await ssh.execute(command)
                        if not command_result.success:
                            raise OSError("SSH command failed")
                        output.append(command_result.output.strip())
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
            f"if test -e {GROUP_FILE}.oct-job || test -e {GROUP_FILE}.oct-job.tmp || "
            f"ls {GROUP_FILE}.oct-bak.* {GROUP_FILE}.oct-new.* >/dev/null 2>&1; "
            "then echo yes; else echo no; fi"
        ])
        return output[0] == "yes"

    async def prepare(self, ip: str, job: str, operation: str, new: str | None) -> None:
        journal, backup, stage = self._suffix(job)
        if await self.leftover(ip):
            raise RuntimeError("stereo_recovery_needed")
        had_file = (await self._run(ip, [
            f"if test -f {GROUP_FILE}; then echo yes; else echo no; fi"
        ]))[0] == "yes"
        record = {
            "job_id": job, "operation": operation, "had_file": had_file,
            "state": "preparing",
        }
        # The journal must exist before the backup. 'preparing' means no
        # commit was attempted; a crash before backup completion must not
        # trigger restoration from a nonexistent or incomplete backup.
        await self._run(ip, [self._write(journal, json.dumps(record))], writable=True)
        commands: list[str | tuple[str, str]] = []
        if had_file:
            commands += [f"cp {GROUP_FILE} {backup}", f"test -s {backup}"]
        if new is not None:
            commands += [self._write(stage, new), f"test -s {stage}"]
        # Atomic replacement: a partially uploaded new journal is never
        # interpreted as 'prepared'.
        record["state"] = "prepared"
        commands += [
            self._write(journal + ".tmp", json.dumps(record)),
            f"mv {journal}.tmp {journal}",
        ]
        await self._run(ip, commands, writable=True)

    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None:
        _, _, stage = self._suffix(job)
        command = f"rm -f {GROUP_FILE}" if removing else f"mv {stage} {GROUP_FILE}"
        await self._run(ip, [command], writable=True)

    async def rollback(self, ip: str, job: str) -> None:
        journal, backup, stage = self._suffix(job)
        data = (await self._run(ip, [f"cat {journal}"]))[0]
        record = json.loads(data)
        if record.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        if record.get("had_file") is True:
            await self._run(ip, [f"test -s {backup}", f"cp {backup} {GROUP_FILE}"], writable=True)
        elif record.get("had_file") is False:
            await self._run(ip, [f"rm -f {GROUP_FILE}"], writable=True)
        else:
            raise RuntimeError("stereo_recovery_needed")
        await self._run(ip, [f"rm -f {stage}"], writable=True)

    async def mark_verified(self, ip: str, job: str) -> None:
        journal, _, stage = self._suffix(job)
        raw = (await self._run(ip, [f"cat {journal}"]))[0]
        record = json.loads(raw)
        if record.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        record["state"] = "verified"
        await self._run(ip, [
            self._write(stage, json.dumps(record, separators=(",", ":"))),
            f"mv {stage} {journal}",
        ], writable=True)

    async def cleanup(self, ip: str, job: str) -> None:
        journal, backup, stage = self._suffix(job)
        raw = (await self._run(ip, [f"cat {journal}"]))[0]
        record = json.loads(raw)
        if record.get("job_id") != job:
            raise RuntimeError("stereo_recovery_needed")
        await self._run(ip, [f"rm -f {backup} {stage} {journal} {journal}.tmp"], writable=True)

    async def recover(self, ip: str) -> bool:
        data = (await self._run(ip, [
            f"if test -f {GROUP_FILE}.oct-job; then cat {GROUP_FILE}.oct-job; fi"
        ]))[0]
        if not data:
            return False
        record = json.loads(data)
        job = record["job_id"]
        self._suffix(job)
        if record.get("state") not in {"verified", "preparing"}:
            await self.rollback(ip, job)
        await self.cleanup(ip, job)
        return True
