"""Opt-in rollback test against two user-selected SoundTouch 10 speakers.

Examples (from apps/backend):
  PYTHONPATH=src python scripts/test_stereo_hardware_rollback.py \
    --left-id ID --left-ip IP --right-id ID --right-ip IP \
    --left-original absent --right-original /path/to/backup.xml

Add --execute ONLY after reviewing a successful read-only preflight.
Use the literal 'absent' when a speaker originally has no GroupService.xml.
For an existing file, provide a local byte-for-byte backup of that file.

The test temporarily commits a staged file to LEFT, rejects the RIGHT commit,
and checks that both speakers return to their exact pre-test file contents.
It does NOT reboot. Do not repeat after any failure or leftover journal.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from defusedxml import ElementTree

from opencloudtouch.setup.ssh_client import SoundTouchSSHClient
from opencloudtouch.stereo.group_backend import SSHGroupBackend
from opencloudtouch.stereo.group_files import GROUP_FILE, validate_device
from opencloudtouch.stereo.service import StereoService


@dataclass(frozen=True)
class Speaker:
    device_id: str
    ip: str
    original: bytes | None


def expected_original(value: str) -> bytes | None:
    if value == "absent":
        return None
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"Original backup not found: {path}")
    data = path.read_bytes()
    if not data:
        raise ValueError(f"Original backup is empty: {path}")
    return data


async def read_file(ip: str) -> bytes | None:
    ssh = SoundTouchSSHClient(ip)
    try:
        connection = await ssh.connect()
        if not connection.success:
            raise RuntimeError("SSH unavailable")
        check = await ssh.execute(
            f"if test -f {GROUP_FILE}; then echo present; else echo absent; fi"
        )
        if not check.success or check.output.strip() not in ("present", "absent"):
            raise RuntimeError("File-presence check failed")
        if check.output.strip() == "absent":
            return None
        response = await ssh.execute(f"base64 {GROUP_FILE}")
        if not response.success:
            raise RuntimeError("Cannot read group file")
        return base64.b64decode("".join(response.output.split()), validate=True)
    finally:
        await ssh.close()


async def verify_empty_api(ip: str) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        for endpoint, root_name in (("getGroup", "group"), ("getZone", "zone")):
            response = await client.get(f"http://{ip}:8090/{endpoint}")
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            if root.tag != root_name or root.attrib or list(root):
                raise RuntimeError(f"/{endpoint} is not empty")


async def preflight(backend: SSHGroupBackend, speakers: tuple[Speaker, Speaker]) -> None:
    for label, speaker in zip(("LEFT", "RIGHT"), speakers):
        current = await read_file(speaker.ip)
        if current != speaker.original:
            raise RuntimeError(f"{label}: file differs from the local original")
        await verify_empty_api(speaker.ip)
        if await backend.leftover(speaker.ip):
            raise RuntimeError(f"{label}: interrupted stereo transaction found")
    print("Preflight OK: exact original files match; group and zone are empty.")
    print("No transaction journals or backups found on either speaker.")


class RejectSecondCommit(SSHGroupBackend):
    def __init__(self, right_ip: str):
        self.right_ip = right_ip

    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None:
        if ip == self.right_ip:
            print("Injected failure: RIGHT commit rejected before file write.")
            raise RuntimeError("stereo_write_failed")
        await super().commit(ip, job, removing=removing)


async def execute(speakers: tuple[Speaker, Speaker]) -> None:
    left, right = speakers
    backend = RejectSecondCommit(right.ip)
    await preflight(backend, speakers)
    records = [
        SimpleNamespace(
            device_id=s.device_id, ip=s.ip,
            model="SoundTouch 10", ssh_permanent=True,
        )
        for s in speakers
    ]
    repo = SimpleNamespace(
        get_all=AsyncMock(return_value=records),
        get_by_device_id=AsyncMock(
            side_effect=lambda key: next(
                (d for d in records if d.device_id == key), None
            )
        ),
    )
    devices = SimpleNamespace(reboot_device=AsyncMock())
    zones = SimpleNamespace(get_zone_status=AsyncMock(return_value=None))
    service = StereoService(repo, devices, zones, mock_mode=False)
    service.backend = backend

    job = await service.create(left.device_id, right.device_id, "Rollback Test")
    try:
        await service.tasks[job["job_id"]]
        result = service.job(job["job_id"])
        current = await asyncio.gather(*(read_file(s.ip) for s in speakers))
        no_leftovers = all(
            not await backend.leftover(s.ip) for s in speakers
        )
        api_ok = True
        for s in speakers:
            try:
                await verify_empty_api(s.ip)
            except Exception:
                api_ok = False

        print("Job state:", result["state"])
        print("Error code:", result["error_code"])
        print("Rolled back:", result["rolled_back"])
        print("Original bytes restored:", all(
            actual == speaker.original
            for actual, speaker in zip(current, speakers)
        ))
        print("No leftovers:", no_leftovers)
        print("Empty group and zone:", api_ok)
        print("Reboot requests:", devices.reboot_device.await_count)

        if not (
            result["state"] == "failed"
            and result["error_code"] == "stereo_write_failed"
            and result["rolled_back"] is True
            and all(
                actual == speaker.original
                for actual, speaker in zip(current, speakers)
            )
            and no_leftovers and api_ok
            and devices.reboot_device.await_count == 0
        ):
            raise RuntimeError(
                "ROLLBACK INCOMPLETE: do not repeat or start another pair"
            )
        print("PASS: both speakers restored to their original state.")
    finally:
        await service.shutdown()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("left", "right"):
        parser.add_argument(f"--{side}-id", required=True)
        parser.add_argument(f"--{side}-ip", required=True)
        parser.add_argument(f"--{side}-original", required=True)
    parser.add_argument(
        "--execute", action="store_true",
        help="Explicitly opt in to the real device write-and-rollback test",
    )
    args = parser.parse_args()
    speakers = (
        Speaker(args.left_id, args.left_ip, expected_original(args.left_original)),
        Speaker(args.right_id, args.right_ip, expected_original(args.right_original)),
    )
    for speaker in speakers:
        validate_device(speaker.device_id, speaker.ip)
    if speakers[0].device_id == speakers[1].device_id:
        parser.error("LEFT and RIGHT must be different speakers")
    if not args.execute:
        await preflight(SSHGroupBackend(), speakers)
        print("READ-ONLY PASS. No speaker was modified.")
        return
    await execute(speakers)


if __name__ == "__main__":
    asyncio.run(main())
