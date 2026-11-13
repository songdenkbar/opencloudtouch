"""Stereo pair service and bounded background jobs."""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any

import httpx

from opencloudtouch.core.exceptions import raise_coded
from opencloudtouch.stereo.group_backend import (
    GroupBackend, MockGroupBackend, SSHGroupBackend, mock_group_store,
)
from opencloudtouch.stereo.group_files import (
    GroupInfo, build_group_xml, parse_group_xml, validate_device, validate_name,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StereoService:
    def __init__(self, device_repo: Any, device_service: Any, zone_service: Any, *, mock_mode: bool = False):
        self.repo = device_repo
        self.devices = device_service
        self.zones = zone_service
        self.mock_mode = mock_mode
        self.backend: GroupBackend = MockGroupBackend() if mock_mode else SSHGroupBackend()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.busy: set[str] = set()

    async def _group(self, device: Any) -> GroupInfo | None:
        if self.mock_mode:
            state = mock_group_store.get(device.ip)
            group = parse_group_xml(state.content) if state and state.content else None
            return replace(group, status="GROUP_OK") if group is not None else None
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            result = await client.get(f"http://{device.ip}:8090/getGroup")
            result.raise_for_status()
            return parse_group_xml(result.content)

    async def _group_safe(self, device: Any) -> tuple[GroupInfo | None, bool]:
        try:
            return await self._group(device), True
        except Exception:
            logger.debug("Stereo group status unavailable for %s", device.device_id)
            return None, False

    async def list_pairs(self) -> dict[str, Any]:
        all_devices = await self.repo.get_all()
        speakers = [d for d in all_devices if d.model.strip().casefold() == "soundtouch 10"]
        responses = await asyncio.gather(*(self._group_safe(d) for d in speakers))
        groups: dict[str, list[GroupInfo]] = {}
        missing: list[str] = []
        leftovers: list[str] = []
        for device, (group, reachable) in zip(speakers, responses):
            if not reachable:
                missing.append(device.device_id)
                continue
            if group:
                groups.setdefault(group.group_id, []).append(group)
            if self.mock_mode or device.ssh_permanent:
                try:
                    if await self.backend.leftover(device.ip):
                        leftovers.append(device.device_id)
                except Exception:
                    logger.debug("Could not read recovery state for %s", device.device_id)
        pairs = []
        for group_id, reports in groups.items():
            master = next(
                (g for g in reports if any(r.device_id == g.master_device_id and r.role == "LEFT" for r in g.roles)),
                reports[0],
            )
            left = next((r.device_id for r in master.roles if r.role == "LEFT"), None)
            right = next((r.device_id for r in master.roles if r.role == "RIGHT"), None)
            identity = (master.master_device_id, [(r.device_id, r.role) for r in master.roles], master.name)
            pairs.append({
                "group_id": group_id, "name": master.name,
                "status": "GROUP_OK" if all(g.status == "GROUP_OK" for g in reports) else next(
                    (g.status for g in reports if g.status != "GROUP_OK"), None
                ),
                "left_device_id": left, "right_device_id": right,
                "partial": len(reports) < 2,
                "inconsistent": any(
                    (g.master_device_id, [(r.device_id, r.role) for r in g.roles], g.name) != identity
                    for g in reports
                ),
            })
        return {
            "pairs": pairs, "unreachable_device_ids": missing,
            "recovery_needed_device_ids": leftovers,
        }

    async def _eligible(self, left_id: str, right_id: str, name: str) -> tuple[Any, Any, str]:
        if left_id == right_id:
            raise_coded(422, "stereo_same_device", "Choose two different devices", field="right_device_id")
        try:
            clean_name = validate_name(name)
        except ValueError:
            raise_coded(422, "stereo_name_invalid", "Invalid stereo name", field="name")
        left = await self.repo.get_by_device_id(left_id)
        right = await self.repo.get_by_device_id(right_id)
        if left is None or right is None:
            raise_coded(404, "device_not_found", "SoundTouch device not found")
        for device in (left, right):
            params = {"device_id": device.device_id}
            if device.model.strip().casefold() != "soundtouch 10":
                raise_coded(422, "stereo_model_unsupported", "SoundTouch 10 required", params=params)
            try:
                validate_device(device.device_id, device.ip)
            except ValueError:
                raise_coded(422, "stereo_device_invalid", "Invalid stored device identifier", params=params)
            if not self.mock_mode:
                if not device.ssh_permanent:
                    raise_coded(409, "stereo_ssh_not_available", "Permanent SSH is required", params=params)
                from opencloudtouch.setup.ssh_client import check_ssh_port
                if not await check_ssh_port(device.ip):
                    raise_coded(503, "stereo_ssh_unreachable", "SSH is unreachable", params=params)
            try:
                if await self.zones.get_zone_status(device.device_id) is not None:
                    raise_coded(409, "stereo_device_in_zone", "Remove device from zone first", params=params)
            except Exception as exc:
                from opencloudtouch.core.exceptions import CodedApiError
                if isinstance(exc, CodedApiError):
                    raise
                raise_coded(503, "device_unreachable", "Unable to check zone", params=params)
            group, reachable = await self._group_safe(device)
            if not reachable:
                raise_coded(503, "device_unreachable", "Cannot query stereo group", params=params)
            if group is not None:
                raise_coded(409, "stereo_device_in_pair", "Device already belongs to a pair", params=params)
            if await self.backend.leftover(device.ip):
                raise_coded(409, "stereo_recovery_needed", "Recovery required before pairing", params=params)
        return left, right, clean_name

    def _reserve(self, ids: list[str]) -> None:
        if any(d in self.busy for d in ids):
            raise_coded(409, "stereo_job_in_progress", "Device already has a stereo job")
        if len(self.tasks) >= 2:
            raise_coded(409, "stereo_job_limit_reached", "Too many active stereo jobs")
        self.busy.update(ids)

    def _enqueue(self, op: str, ids: list[str], name: str | None,
                 group_id: str | None, left: Any | None, right: Any | None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job: dict[str, Any] = {
            "job_id": job_id, "operation": op, "group_id": group_id, "name": name,
            "left_device_id": left.device_id if left else None,
            "right_device_id": right.device_id if right else None,
            "state": "running", "step": "backing_up", "started_at": _now(),
            "finished_at": None, "error_code": None, "failed_device_id": None,
            "rolled_back": None,
        }
        self.jobs[job_id] = job
        task = asyncio.create_task(self._execute(job, ids, left, right))
        self.tasks[job_id] = task
        task.add_done_callback(lambda _task: self._finished(job_id, ids))
        return dict(job)

    def _finished(self, job_id: str, ids: list[str]) -> None:
        self.tasks.pop(job_id, None)
        self.busy.difference_update(ids)
        while len(self.jobs) > 20:
            old = next((k for k, v in self.jobs.items() if v["state"] != "running"), None)
            if old is None:
                break
            self.jobs.pop(old)

    async def create(self, left_id: str, right_id: str, name: str) -> dict[str, Any]:
        self._reserve([left_id, right_id])
        try:
            left, right, clean = await self._eligible(left_id, right_id, name)
            existing = {p["group_id"] for p in (await self.list_pairs())["pairs"]}
            group_id = str(secrets.randbelow(9_000_000) + 1_000_000)
            while group_id in existing:
                group_id = str(secrets.randbelow(9_000_000) + 1_000_000)
            return self._enqueue("create", [left_id, right_id], clean, group_id, left, right)
        except BaseException:
            self.busy.difference_update([left_id, right_id])
            raise

    async def remove(self, group_id: str, force: bool = False) -> dict[str, Any]:
        if not group_id.isdecimal() or len(group_id) > 12:
            raise_coded(422, "stereo_group_id_invalid", "Invalid stereo group ID")
        pair = next((p for p in (await self.list_pairs())["pairs"] if p["group_id"] == group_id), None)
        if pair is None:
            raise_coded(404, "stereo_pair_not_found", "Stereo pair not found")
        ids = [pair["left_device_id"], pair["right_device_id"]]
        if any(i is None for i in ids):
            raise_coded(503, "device_unreachable", "Incomplete pair membership")
        self._reserve(ids)
        try:
            left = await self.repo.get_by_device_id(ids[0])
            right = await self.repo.get_by_device_id(ids[1])
            if not left or not right:
                raise_coded(503, "device_unreachable", "Pair member missing")
            reachable: list[Any] = []
            for device in (left, right):
                group, ok = await self._group_safe(device)
                if not ok:
                    if force:
                        continue
                    raise_coded(503, "device_unreachable", "Pair member is unreachable")
                if not group or group.group_id != group_id:
                    raise_coded(409, "stereo_pair_not_found", "Pair membership changed")
                if await self.backend.leftover(device.ip):
                    raise_coded(409, "stereo_recovery_needed", "Recovery required")
                reachable.append(device)
            if not reachable:
                raise_coded(503, "device_unreachable", "Both speakers are unreachable")
            # Keep both members in the job's identity; only operate on reachable devices.
            return self._enqueue(
                "remove", ids, pair["name"], group_id,
                left if left in reachable else None,
                right if right in reachable else None,
            )
        except BaseException:
            self.busy.difference_update(ids)
            raise

    async def recover(self, device_id: str) -> dict[str, Any]:
        self._reserve([device_id])
        try:
            device = await self.repo.get_by_device_id(device_id)
            if device is None:
                raise_coded(404, "device_not_found", "Device not found")
            if not await self.backend.leftover(device.ip):
                raise_coded(409, "stereo_nothing_to_recover", "No recovery information found")
            return self._enqueue("recover", [device_id], None, None, device, None)
        except BaseException:
            self.busy.discard(device_id)
            raise

    async def _reboot(self, devices: list[Any]) -> None:
        if self.mock_mode:
            return
        results = await asyncio.gather(
            *(self.devices.reboot_device(d.device_id) for d in devices),
            return_exceptions=True,
        )
        if any(isinstance(r, BaseException) for r in results):
            raise RuntimeError("stereo_reboot_failed")

    async def _wait_online(self, devices: list[Any]) -> None:
        if self.mock_mode:
            return

        async def wait_one(device: Any) -> None:
            # All speakers must be watched concurrently. Otherwise the second
            # speaker may have already rebooted by the time it is polled.
            for _ in range(30):
                _, online = await self._group_safe(device)
                if not online:
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError("stereo_timeout")
            for _ in range(36):
                _, online = await self._group_safe(device)
                if online:
                    return
                await asyncio.sleep(5)
            raise RuntimeError("stereo_timeout")

        await asyncio.gather(*(wait_one(d) for d in devices))

    async def _verify(self, devices: list[Any], group_id: str | None) -> None:
        for _ in range(6):
            groups = await asyncio.gather(*(self._group_safe(d) for d in devices))
            if all(ok for _g, ok in groups):
                if group_id is None and all(g is None for g, _ok in groups):
                    return
                if group_id and all(
                    g is not None and g.group_id == group_id and g.status == "GROUP_OK"
                    and len(g.roles) == 2
                    and {r.role for r in g.roles} == {"LEFT", "RIGHT"}
                    and any(r.device_id == d.device_id for r in g.roles)
                    for d, (g, _ok) in zip(devices, groups)
                ):
                    # Full-pair verification also checks master and role identities.
                    if len(devices) == 1 or all(
                        g is not None
                        and g.master_device_id == devices[0].device_id
                        and {(r.device_id, r.role) for r in g.roles} == {
                            (devices[0].device_id, "LEFT"),
                            (devices[1].device_id, "RIGHT"),
                        }
                        for g, _ok in groups
                    ):
                        return
            await asyncio.sleep(10 if not self.mock_mode else 0)
        raise RuntimeError("stereo_verify_failed")

    async def _execute(self, job: dict[str, Any], ids: list[str],
                       left: Any | None, right: Any | None) -> None:
        operation = job["operation"]
        devices = [d for d in (left, right) if d is not None]
        prepared: list[Any] = []
        committed = False
        verified = False
        try:
            if operation == "recover":
                job["step"] = "rolling_back"
                if not await self.backend.recover(left.ip):
                    raise RuntimeError("stereo_nothing_to_recover")
                job["step"] = "rebooting"
                await self._reboot(devices)
                job["step"] = "waiting_for_devices"
                await self._wait_online(devices)
            else:
                job["step"] = "backing_up"
                for index, device in enumerate(devices):
                    xml = None
                    if operation == "create":
                        xml = build_group_xml(
                            job["group_id"], job["name"], left.device_id, left.ip,
                            right.device_id, right.ip, right_file=(index == 1),
                        )
                    # Track BEFORE the prepare: journal may already exist if staging fails.
                    prepared.append(device)
                    await self.backend.prepare(device.ip, job["job_id"], operation, xml)
                job["step"] = "writing_files"
                for device in devices:
                    committed = True
                    await self.backend.commit(
                        device.ip, job["job_id"], removing=operation == "remove"
                    )
                job["step"] = "rebooting"
                await self._reboot(devices)
                job["step"] = "waiting_for_devices"
                await self._wait_online(devices)
                job["step"] = "verifying"
                await self._verify(devices, job["group_id"] if operation == "create" else None)
                for device in devices:
                    await self.backend.mark_verified(device.ip, job["job_id"])
                verified = True
                for device in devices:
                    await self.backend.cleanup(device.ip, job["job_id"])
            job["state"] = "succeeded"
            job["step"] = "done"
        except BaseException as exc:
            logger.warning("Stereo job %s failed: %s", job["job_id"], type(exc).__name__)
            job["state"] = "failed"
            job["error_code"] = str(exc) if str(exc).startswith("stereo_") else "stereo_ssh_failed"
            if verified:
                # Do not restore just one side after a successful pair verification.
                # Any remaining journal is marked verified and recovery cleans it.
                job["rolled_back"] = False
            elif prepared:
                job["step"] = "rolling_back"
                restored = True
                for device in reversed(prepared):
                    try:
                        if await self.backend.leftover(device.ip):
                            await self.backend.rollback(device.ip, job["job_id"])
                    except Exception:
                        restored = False
                        job["failed_device_id"] = device.device_id
                cancelled = isinstance(exc, asyncio.CancelledError)
                if restored and committed and not cancelled:
                    try:
                        await self._reboot(devices)
                        await self._wait_online(devices)
                        await self._verify(devices, job['group_id'] if operation == 'remove' else None)
                    except Exception:
                        restored = False
                if restored and not cancelled:
                    for device in prepared:
                        try:
                            if await self.backend.leftover(device.ip):
                                await self.backend.cleanup(device.ip, job["job_id"])
                        except Exception:
                            restored = False
                job["rolled_back"] = restored and not cancelled
                if not restored and job["error_code"] != "stereo_timeout":
                    job["error_code"] = "stereo_rollback_failed"
            if isinstance(exc, asyncio.CancelledError):
                logger.warning("Stereo job cancelled, recovery files retained if necessary")
        finally:
            job["finished_at"] = _now()

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs.get(job_id)
        if job is None:
            raise_coded(404, "stereo_job_not_found", "Stereo job not found")
        return dict(job)

    def list_jobs(self) -> dict[str, Any]:
        return {"jobs": [dict(v) for v in reversed(list(self.jobs.values()))]}
