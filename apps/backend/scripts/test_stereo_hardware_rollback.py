"""Explicit opt-in rollback test on Bad (LEFT) and Keller (RIGHT).

From apps/backend:
    PYTHONPATH=src python scripts/test_stereo_hardware_rollback.py
    PYTHONPATH=src python scripts/test_stereo_hardware_rollback.py --execute

The default invocation is read-only. --execute temporarily writes a group file
to Bad, intentionally refuses the second commit on Keller, then verifies the
real StereoService rollback. The script does not request any reboots.

Do not repeat --execute if an earlier run fails or leaves journal files.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from defusedxml import ElementTree

from opencloudtouch.setup.ssh_client import SoundTouchSSHClient
from opencloudtouch.stereo.group_backend import SSHGroupBackend
from opencloudtouch.stereo.group_files import GROUP_FILE
from opencloudtouch.stereo.service import StereoService

LEFT_ID, LEFT_IP = "EC24B8DA6807", "192.168.55.26"
RIGHT_ID, RIGHT_IP = "341513021E2E", "192.168.55.68"
BACKUP_SHA = "51889d0b4f032089817ac95e78cd8731dc013e5cbc061300296ee3c0fef6e08c"


async def read_file(ip: str) -> bytes | None:
    ssh = SoundTouchSSHClient(ip)
    try:
        connection = await ssh.connect()
        if not connection.success:
            raise RuntimeError(f"{ip}: SSH unavailable")
        check = await ssh.execute(
            f"if test -f {GROUP_FILE}; then echo present; else echo absent; fi"
        )
        if not check.success or check.output.strip() not in ("present", "absent"):
            raise RuntimeError(f"{ip}: file-presence check failed")
        if check.output.strip() == "absent":
            return None
        response = await ssh.execute(f"base64 {GROUP_FILE}")
        if not response.success:
            raise RuntimeError(f"{ip}: cannot read GroupService.xml")
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
                raise RuntimeError(f"{ip}: /{endpoint} is not empty")


async def preflight(backend: SSHGroupBackend) -> None:
    backup_path = Path.home() / "stereo-hardware-backup/Keller-GroupService.xml"
    if not backup_path.is_file():
        raise RuntimeError(f"Independent backup missing: {backup_path}")
    backup = backup_path.read_bytes()
    if len(backup) != 50 or hashlib.sha256(backup).hexdigest() != BACKUP_SHA:
        raise RuntimeError("Independent Keller backup has changed unexpectedly")

    actual = await asyncio.gather(read_file(LEFT_IP), read_file(RIGHT_IP))
    if actual != [None, backup]:
        raise RuntimeError("Speaker files differ from the saved pre-test state")

    for name, ip in (("Bad", LEFT_IP), ("Keller", RIGHT_IP)):
        await verify_empty_api(ip)
        if await backend.leftover(ip):
            raise RuntimeError(f"{name}: unfinished stereo journal/backup exists")
    print("Preflight OK: Bad has no file; Keller matches the 50-byte backup.")
    print("Both devices report empty /getGroup and /getZone; no journals detected.")


class RejectSecondCommit(SSHGroupBackend):
    async def commit(self, ip: str, job: str, *, removing: bool = False) -> None:
        if ip == RIGHT_IP:
            print("Injected failure: refusing Keller commit before any SSH write.")
            raise RuntimeError("stereo_write_failed")
        await super().commit(ip, job, removing=removing)


async def execute() -> None:
    backend = RejectSecondCommit()
    await preflight(backend)

    bad = SimpleNamespace(
        device_id=LEFT_ID, ip=LEFT_IP, model="SoundTouch 10",
        ssh_permanent=True,
    )
    keller = SimpleNamespace(
        device_id=RIGHT_ID, ip=RIGHT_IP, model="SoundTouch 10",
        ssh_permanent=True,
    )
    repository = SimpleNamespace(
        get_all=AsyncMock(return_value=[bad, keller]),
        get_by_device_id=AsyncMock(
            side_effect=lambda device_id: {
                LEFT_ID: bad, RIGHT_ID: keller,
            }.get(device_id)
        ),
    )
    device_service = SimpleNamespace(reboot_device=AsyncMock())
    zone_service = SimpleNamespace(get_zone_status=AsyncMock(return_value=None))
    service = StereoService(repository, device_service, zone_service, mock_mode=False)
    service.backend = backend

    job = await service.create(LEFT_ID, RIGHT_ID, "Rollback Hardware Test")
    print(f"Created test job {job['job_id']}; first speaker may be written now.")
    try:
        task = service.tasks[job["job_id"]]
        await task
        result = service.job(job["job_id"])
        print("Job state:", result["state"])
        print("Job error:", result["error_code"])
        print("Rolled back:", result["rolled_back"])

        # Verification is independent of the service's reported job status.
        backup = (
            Path.home() / "stereo-hardware-backup/Keller-GroupService.xml"
        ).read_bytes()
        current = await asyncio.gather(read_file(LEFT_IP), read_file(RIGHT_IP))
        no_leftovers = not await backend.leftover(LEFT_IP) and not await backend.leftover(RIGHT_IP)
        group_status_ok = True
        for ip in (LEFT_IP, RIGHT_IP):
            try:
                await verify_empty_api(ip)
            except Exception:
                group_status_ok = False

        print("Bad original file absent:", current[0] is None)
        print("Keller original bytes restored:", current[1] == backup)
        print("No recovery leftovers:", no_leftovers)
        print("Empty group and zone responses:", group_status_ok)
        print("Reboot requests:", device_service.reboot_device.await_count)

        if (
            result["state"] != "failed"
            or result["error_code"] != "stereo_write_failed"
            or result["rolled_back"] is not True
            or current != [None, backup]
            or not no_leftovers
            or not group_status_ok
            or device_service.reboot_device.await_count != 0
        ):
            raise RuntimeError(
                "HARDWARE ROLLBACK INCOMPLETE: do not repeat the test or start a pair"
            )
        print("PASS: intended failure was rolled back to the exact original state.")
    finally:
        await service.shutdown()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="Perform the real write-and-rollback test")
    args = parser.parse_args()
    if not args.execute:
        await preflight(SSHGroupBackend())
        print("READ-ONLY PASS. No speaker was modified.")
        print("Write-and-rollback requires a separate --execute invocation.")
        return
    await execute()


if __name__ == "__main__":
    asyncio.run(main())
