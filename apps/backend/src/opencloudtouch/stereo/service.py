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
from opencloudtouch.stereo.jobs import (
    CREATE_REMOVE_BUDGET_SECONDS,
    ROLLBACK_BUDGET_SECONDS,
    phase_budget,
)
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
        self.primary_budget_seconds = CREATE_REMOVE_BUDGET_SECONDS
        self.rollback_budget_seconds = ROLLBACK_BUDGET_SECONDS

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
        # Standby transitions can briefly interrupt /getGroup. Keep the safety
        # check, but do not mark a speaker unreachable after a single failure.
        for attempt in range(3):
            try:
                return await self._group(device), True
            except Exception as exc:
                logger.debug(
                    "Stereo group status unavailable for %s (attempt %d/3): %s",
                    device.device_id, attempt + 1, exc,
                )
                if attempt < 2:
                    await asyncio.sleep(0.5)
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
            "left_device_id": ids[0] if op != "recover" else None,
            "right_device_id": ids[1] if op != "recover" and len(ids) > 1 else None,
            "state": "running", "step": "backing_up", "started_at": _now(),
            "finished_at": None, "error_code": None,
            "failed_device_id": ids[0] if op == "recover" else None,
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
            state = await self.backend.journal_state(device.ip)
            # A prepared/committed transaction may involve two speakers.
            # Rolling back only this speaker could split a functioning pair.
            # Keep both journals intact pending coordinated pair recovery.
            if state in {"prepared", "committed"}:
                raise_coded(
                    409, "stereo_recovery_needed",
                    "Pair-wide recovery required; single-speaker rollback is unsafe",
                )
            if state is None:
                # A lone initial .tmp journal can be discarded safely by
                # backend.recover; orphaned backup/stage files are not safe.
                pass
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

    async def _info_online(self, device: Any) -> bool:
        """Poll the existing SoundTouch /info API for reboot transitions."""
        if self.mock_mode:
            return True
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(5.0, connect=2.0)
            ) as client:
                response = await client.get(f"http://{device.ip}:8090/info")
                response.raise_for_status()
                return True
        except (httpx.HTTPError, OSError):
            return False

    async def _wait_online(self, devices: list[Any]) -> None:
        if self.mock_mode:
            return

        async def wait_one(device: Any) -> None:
            # Observe both speakers concurrently. A missed offline transition
            # must not be mistaken for a successful reboot.
            for _ in range(30):
                if not await self._info_online(device):
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError("stereo_timeout")
            for _ in range(36):
                if await self._info_online(device):
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

    async def _verify_previous(
        self, devices: list[Any], previous: dict[str, GroupInfo | None]
    ) -> None:
        """Compare every speaker's actual group with its pre-job response."""
        for attempt in range(6):
            responses = await asyncio.gather(*(self._group_safe(d) for d in devices))
            if all(online and group == previous[d.device_id]
                   for d, (group, online) in zip(devices, responses)):
                return
            if attempt < 5:
                await asyncio.sleep(0 if self.mock_mode else 10)
        raise RuntimeError("stereo_rollback_failed")

    async def _wait_reachable(self, devices: list[Any]) -> None:
        """Rollback may reissue a reboot to a speaker already rebooting.

        Unlike the initial wait, do not require observing a second offline
        transition: the speaker may already be down when the command is sent.
        """
        if self.mock_mode:
            return

        async def wait_one(device: Any) -> None:
            for _ in range(36):
                if await self._info_online(device):
                    return
                await asyncio.sleep(5)
            raise RuntimeError("stereo_timeout")

        await asyncio.gather(*(wait_one(d) for d in devices))

    async def _rollback_job(
        self,
        job: dict[str, Any],
        prepared: list[Any],
        *,
        committed: bool,
        reboot_started: bool,
        previous: dict[str, GroupInfo | None],
        cancelled: bool,
    ) -> bool:
        """Keep recovery artifacts unless restoration and verification succeed."""
        job["step"] = "rolling_back"
        if not prepared:
            return True
        complete = True
        async with phase_budget(self.rollback_budget_seconds, "stereo_rollback_failed"):
            for device in reversed(prepared):
                try:
                    if await self.backend.leftover(device.ip):
                        # Before the first commit all original files are intact.
                        # Cleanup of a staged transaction is enough.
                        if committed:
                            await self.backend.rollback(device.ip, job["job_id"])
                        elif cancelled:
                            # Shutdown leaves the journal as evidence even after
                            # best-effort file-only restoration.
                            continue
                except Exception:
                    complete = False
                    job["failed_device_id"] = device.device_id
            if not complete or cancelled:
                return False
            if reboot_started:
                # A failed reboot command may have rebooted one speaker anyway.
                # Only request another reboot on currently reachable speakers.
                online = await asyncio.gather(*(self._group_safe(d) for d in prepared))
                available = [d for d, (_group, reachable) in zip(prepared, online) if reachable]
                if available:
                    await self._reboot(available)
                await self._wait_reachable(prepared)
            await self._verify_previous(prepared, previous)
            for device in prepared:
                if await self.backend.leftover(device.ip):
                    await self.backend.cleanup(device.ip, job["job_id"])
        return True

    async def _execute(
        self, job: dict[str, Any], ids: list[str],
        left: Any | None, right: Any | None,
    ) -> None:
        operation = job["operation"]
        devices = [d for d in (left, right) if d is not None]
        prepared: list[Any] = []
        previous: dict[str, GroupInfo | None] = {}
        committed = False
        reboot_started = False
        verified = False
        try:
            if operation == "recover":
                if left is None:
                    raise RuntimeError("stereo_device_invalid")
                async with phase_budget(self.primary_budget_seconds, "stereo_timeout"):
                    job["step"] = "rolling_back"
                    if not await self.backend.recover(left.ip):
                        raise RuntimeError("stereo_nothing_to_recover")
                    job["step"] = "rebooting"
                    await self._reboot(devices)
                    job["step"] = "waiting_for_devices"
                    await self._wait_online(devices)
            else:
                if operation == "create" and (left is None or right is None):
                    raise RuntimeError("stereo_device_invalid")
                async with phase_budget(self.primary_budget_seconds, "stereo_timeout"):
                    # Capture the exact old firmware state on every processed
                    # device; a rollback must restore that state, not merely
                    # pass the verification rules of the opposite operation.
                    for device in devices:
                        group, online = await self._group_safe(device)
                        if not online:
                            raise RuntimeError("device_unreachable")
                        previous[device.device_id] = group
                    job["step"] = "backing_up"
                    for device in devices:
                        xml = None
                        if operation == "create":
                            assert left is not None and right is not None
                            xml = build_group_xml(
                                job["group_id"], job["name"], left.device_id,
                                left.ip, right.device_id, right.ip,
                                right_file=(device.device_id == right.device_id),
                            )
                        # Journal creation can succeed before prepare fails.
                        prepared.append(device)
                        await self.backend.prepare(device.ip, job["job_id"], operation, xml)
                    job["step"] = "writing_files"
                    for device in devices:
                        committed = True
                        await self.backend.commit(
                            device.ip, job["job_id"], removing=operation == "remove"
                        )
                    job["step"] = "rebooting"
                    reboot_started = True
                    await self._reboot(devices)
                    job["step"] = "waiting_for_devices"
                    await self._wait_online(devices)
                    job["step"] = "verifying"
                    await self._verify(
                        devices, job["group_id"] if operation == "create" else None
                    )
                    for device in devices:
                        await self.backend.mark_verified(device.ip, job["job_id"])
                    verified = True
                    for device in devices:
                        await self.backend.cleanup(device.ip, job["job_id"])
            job["state"] = "succeeded"
            job["step"] = "done"
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            logger.warning("Stereo job %s failed: %s", job["job_id"], type(exc).__name__)
            job["state"] = "failed"
            job["error_code"] = (
                str(exc) if str(exc).startswith(("stereo_", "device_"))
                else "stereo_ssh_failed"
            )
            if verified:
                # No asymmetric rollback after verification. The remaining
                # verified journal can be cleaned by explicit recovery.
                job["rolled_back"] = False
            elif prepared:
                # Own the rollback task explicitly. On shutdown, shield() by
                # itself would return immediately on cancellation and leave an
                # orphan rollback task running after the device lock is freed.
                rollback_task = asyncio.create_task(self._rollback_job(
                    job, prepared, committed=committed,
                    reboot_started=reboot_started and not cancelled,
                    previous=previous, cancelled=cancelled,
                ))
                try:
                    restored = await asyncio.shield(rollback_task)
                except asyncio.CancelledError:
                    # Finish the bounded file-only rollback before the parent
                    # task becomes done and its lock is released.
                    try:
                        restored = await rollback_task
                    except BaseException:
                        restored = False
                except BaseException:
                    restored = False
                job["rolled_back"] = restored
                if not restored and job["error_code"] != "stereo_timeout":
                    job["error_code"] = "stereo_rollback_failed"
            if cancelled:
                logger.warning("Stereo job cancelled; recovery files retained if needed")
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
