"""Stereo pair service and bounded background jobs."""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone
from dataclasses import replace
from typing import Any, Awaitable, Callable
from time import monotonic

import httpx

from opencloudtouch.core.exceptions import raise_coded
from opencloudtouch.stereo.jobs import (
    CREATE_REMOVE_BUDGET_SECONDS,
    ROLLBACK_BUDGET_SECONDS,
    phase_budget,
)
from opencloudtouch.stereo.group_backend import (
    GroupBackend,
    MockGroupBackend,
    SSHGroupBackend,
    mock_group_store,
)
from opencloudtouch.stereo.group_files import (
    GroupInfo,
    build_group_xml,
    parse_group_xml,
    validate_device,
    validate_name,
)

logger = logging.getLogger(__name__)


class StereoDeviceFailure(RuntimeError):
    """A job failure tied to a particular speaker."""

    def __init__(self, code: str, device_id: str):
        super().__init__(code)
        self.device_id = device_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class StereoService:
    def __init__(
        self,
        device_repo: Any,
        device_service: Any,
        zone_service: Any,
        *,
        mock_mode: bool = False,
        backend_factory: Callable[[], GroupBackend] | None = None,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        now: Callable[[], str] | None = None,
    ):
        self.repo = device_repo
        self.devices = device_service
        self.zones = zone_service
        self.mock_mode = mock_mode
        self.backend: GroupBackend = (
            backend_factory()
            if backend_factory is not None
            else MockGroupBackend() if mock_mode else SSHGroupBackend()
        )
        self.clock = clock or monotonic
        self._sleep_hook = sleep
        self._now = now or _now
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tasks: dict[str, asyncio.Task[None]] = {}
        # Public tests and A.3 refer to the internal in-flight task registry.
        self._tasks = self.tasks
        self.busy: set[str] = set()
        self.primary_budget_seconds = CREATE_REMOVE_BUDGET_SECONDS
        self.rollback_budget_seconds = ROLLBACK_BUDGET_SECONDS

    async def _sleep(self, seconds: float) -> None:
        # Resolve asyncio.sleep at call time so existing test patches work too.
        await (getattr(self, "_sleep_hook", None) or asyncio.sleep)(seconds)

    async def _group(self, device: Any) -> GroupInfo | None:
        if self.mock_mode:
            state = mock_group_store.get(device.ip)
            group = parse_group_xml(state.content) if state and state.content else None
            return replace(group, status="GROUP_OK") if group is not None else None
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0)) as client:
            result = await client.get(f"http://{device.ip}:8090/getGroup")
            result.raise_for_status()
            parsed = parse_group_xml(result.content)
            if parsed is None:
                # An empty <group /> is safe, but malformed or incomplete XML
                # must not be treated as proof that the speaker is unpaired.
                from defusedxml import ElementTree

                root = ElementTree.fromstring(result.content)
                if (
                    root.tag != "group"
                    or root.attrib
                    or list(root)
                    or (root.text or "").strip()
                ):
                    raise ValueError("Unexpected SoundTouch group response")
            return parsed

    async def _group_safe(self, device: Any) -> tuple[GroupInfo | None, bool]:
        # Standby transitions can briefly interrupt /getGroup. Keep the safety
        # check, but do not mark a speaker unreachable after a single failure.
        for attempt in range(3):
            try:
                return await self._group(device), True
            except Exception as exc:
                logger.debug(
                    "Stereo group status unavailable for %s (attempt %d/3; %s)",
                    device.device_id,
                    attempt + 1,
                    type(exc).__name__,
                )
                if attempt < 2:
                    await self._sleep(0.5)
        return None, False

    async def list_pairs(self) -> dict[str, Any]:
        all_devices = await self.repo.get_all()
        speakers = [
            d for d in all_devices if d.model.strip().casefold() == "soundtouch 10"
        ]
        # Limit concurrent device requests as required by spec #549.
        semaphore = asyncio.Semaphore(5)

        async def query_group(device: Any) -> tuple[GroupInfo | None, bool]:
            async with semaphore:
                try:
                    return await asyncio.wait_for(self._group_safe(device), timeout=5.0)
                except TimeoutError:
                    return None, False

        responses = await asyncio.gather(*(query_group(d) for d in speakers))
        groups: dict[str, list[tuple[str, GroupInfo]]] = {}
        known_ids = {d.device_id for d in all_devices}
        missing: list[str] = []
        leftovers: list[str] = []
        for device, (group, reachable) in zip(speakers, responses):
            if not reachable:
                missing.append(device.device_id)
                continue
            if group:
                groups.setdefault(group.group_id, []).append((device.device_id, group))
            if self.mock_mode or device.ssh_permanent:
                try:
                    if await asyncio.wait_for(
                        self.backend.leftover(device.ip), timeout=5.0
                    ):
                        leftovers.append(device.device_id)
                except Exception:
                    logger.debug(
                        "Could not read recovery state for %s", device.device_id
                    )
        pairs = []
        for group_id, reports in groups.items():
            master = next(
                (g for speaker_id, g in reports if speaker_id == g.master_device_id),
                reports[0][1],
            )
            left = next((r.device_id for r in master.roles if r.role == "LEFT"), None)
            right = next((r.device_id for r in master.roles if r.role == "RIGHT"), None)
            identity = (
                master.master_device_id,
                frozenset((r.device_id, r.role, r.ip_address) for r in master.roles),
                master.name,
            )
            pairs.append(
                {
                    "group_id": group_id,
                    "name": master.name,
                    "status": (
                        "GROUP_OK"
                        if all(g.status == "GROUP_OK" for _, g in reports)
                        else next(
                            (g.status for _, g in reports if g.status != "GROUP_OK"),
                            None,
                        )
                    ),
                    "left_device_id": left,
                    "right_device_id": right,
                    "partial": len(reports) < 2
                    or left not in known_ids
                    or right not in known_ids,
                    "inconsistent": any(
                        (
                            g.master_device_id,
                            frozenset((r.device_id, r.role, r.ip_address) for r in g.roles),
                            g.name,
                        )
                        != identity
                        for _, g in reports
                    ),
                }
            )
        return {
            "pairs": pairs,
            "unreachable_device_ids": missing,
            "recovery_needed_device_ids": leftovers,
        }

    @staticmethod
    def _validate_request(left_id: str, right_id: str, name: str) -> str:
        """Reject invalid request fields before reserving device locks."""
        if left_id == right_id:
            raise_coded(
                422,
                "stereo_same_device",
                "Choose two different devices",
                field="right_device_id",
            )
        try:
            return validate_name(name)
        except ValueError:
            raise_coded(422, "stereo_name_invalid", "Invalid stereo name", field="name")

    async def _eligible_local(self, left_id: str, right_id: str) -> tuple[Any, Any]:
        left = await self.repo.get_by_device_id(left_id)
        right = await self.repo.get_by_device_id(right_id)
        if left is None or right is None:
            raise_coded(
                404,
                "device_not_found",
                "SoundTouch device not found",
                params={"device_id": left_id if left is None else right_id},
            )
        # Validate each prerequisite for both speakers in the order specified
        # by #549. Do not contact either speaker while the other still has an
        # invalid stored model or address.
        for device in (left, right):
            if device.model.strip().casefold() != "soundtouch 10":
                raise_coded(
                    422,
                    "stereo_model_unsupported",
                    "SoundTouch 10 required",
                    params={"device_id": device.device_id},
                )
        for device in (left, right):
            try:
                validate_device(device.device_id, device.ip)
            except ValueError:
                raise_coded(
                    422,
                    "stereo_device_invalid",
                    "Invalid stored device identifier",
                    params={"device_id": device.device_id},
                )
        if not self.mock_mode:
            for device in (left, right):
                if not device.ssh_permanent:
                    raise_coded(
                        409,
                        "stereo_ssh_not_available",
                        "Permanent SSH is required",
                        params={"device_id": device.device_id},
                    )
        return left, right

    async def _eligible(self, left: Any, right: Any) -> tuple[Any, Any]:
        """Check remote eligibility only after both device locks are held."""
        if not self.mock_mode:
            from opencloudtouch.setup.ssh_client import check_ssh_port

            for device in (left, right):
                try:
                    ssh_reachable = await check_ssh_port(device.ip)
                except (OSError, TimeoutError):
                    ssh_reachable = False
                if not ssh_reachable:
                    raise_coded(
                        503,
                        "stereo_ssh_unreachable",
                        "SSH is unreachable",
                        params={"device_id": device.device_id},
                    )
        from opencloudtouch.core.exceptions import CodedApiError

        for device in (left, right):
            try:
                zone = await self.zones.get_zone_status(device.device_id)
            except CodedApiError:
                raise
            except Exception:
                raise_coded(
                    503,
                    "device_unreachable",
                    "Unable to check zone",
                    params={"device_id": device.device_id},
                )
            if zone is not None:
                raise_coded(
                    409,
                    "stereo_device_in_zone",
                    "Remove device from zone first",
                    params={"device_id": device.device_id},
                )
        group_checks = []
        for device in (left, right):
            group, reachable = await self._group_safe(device)
            group_checks.append((device, group, reachable))
        # The specified validation order gives an existing pair priority over
        # an offline peer, irrespective of the LEFT/RIGHT request ordering.
        for device, group, reachable in group_checks:
            if reachable and group is not None:
                raise_coded(
                    409,
                    "stereo_device_in_pair",
                    "Device already belongs to a pair",
                    params={"device_id": device.device_id},
                )
        for device, _group, reachable in group_checks:
            if not reachable:
                raise_coded(
                    503,
                    "device_unreachable",
                    "Cannot query stereo group",
                    params={"device_id": device.device_id},
                )
        for device in (left, right):
            try:
                leftovers = await self.backend.leftover(device.ip)
            except (OSError, TimeoutError):
                raise_coded(
                    503,
                    "stereo_ssh_unreachable",
                    "Unable to inspect recovery files",
                    params={"device_id": device.device_id},
                )
            if leftovers:
                raise_coded(
                    409,
                    "stereo_recovery_needed",
                    "Recovery required before pairing",
                    params={"device_id": device.device_id},
                )
        return left, right

    def _reserve(self, ids: list[str]) -> None:
        busy_device = next(
            (device_id for device_id in ids if device_id in self.busy), None
        )
        if busy_device is not None:
            raise_coded(
                409,
                "stereo_job_in_progress",
                "Device already has a stereo job",
                params={"device_id": busy_device},
            )
        if len(self.tasks) >= 2:
            raise_coded(409, "stereo_job_limit_reached", "Too many active stereo jobs")
        self.busy.update(ids)

    def _enqueue(
        self,
        op: str,
        ids: list[str],
        name: str | None,
        group_id: str | None,
        left: Any | None,
        right: Any | None,
    ) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        job: dict[str, Any] = {
            "job_id": job_id,
            "operation": op,
            "group_id": group_id,
            "name": name,
            "left_device_id": ids[0] if op != "recover" else None,
            "right_device_id": ids[1] if op != "recover" and len(ids) > 1 else None,
            "state": "running",
            "step": "backing_up",
            "started_at": self._now(),
            "finished_at": None,
            "error_code": None,
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
            old = next(
                (k for k, v in self.jobs.items() if v["state"] != "running"), None
            )
            if old is None:
                break
            self.jobs.pop(old)

    async def create(self, left_id: str, right_id: str, name: str) -> dict[str, Any]:
        clean = self._validate_request(left_id, right_id, name)
        # Spec validation priority: unknown/unsupported/invalid stored data
        # takes precedence over a concurrent lock, without any remote access.
        left, right = await self._eligible_local(left_id, right_id)
        self._reserve([left_id, right_id])
        try:
            await self._eligible(left, right)
            existing = {p["group_id"] for p in (await self.list_pairs())["pairs"]}
            group_id = str(secrets.randbelow(9_000_000) + 1_000_000)
            while group_id in existing:
                group_id = str(secrets.randbelow(9_000_000) + 1_000_000)
            return self._enqueue(
                "create", [left_id, right_id], clean, group_id, left, right
            )
        except BaseException:
            self.busy.difference_update([left_id, right_id])
            raise

    async def remove(self, group_id: str, force: bool = False) -> dict[str, Any]:
        if not (
            1 <= len(group_id) <= 12 and group_id.isascii() and group_id.isdecimal()
        ):
            raise_coded(422, "stereo_group_id_invalid", "Invalid stereo group ID")
        pair = next(
            (
                p
                for p in (await self.list_pairs())["pairs"]
                if p["group_id"] == group_id
            ),
            None,
        )
        if pair is None:
            raise_coded(404, "stereo_pair_not_found", "Stereo pair not found")
        ids = [pair["left_device_id"], pair["right_device_id"]]
        if any(i is None for i in ids):
            raise_coded(503, "device_unreachable", "Incomplete pair membership")
        self._reserve(ids)
        try:
            left = await self.repo.get_by_device_id(ids[0])
            right = await self.repo.get_by_device_id(ids[1])
            if left is None or right is None:
                if not force or (left is None and right is None):
                    missing_id = ids[0] if left is None else ids[1]
                    raise_coded(
                        503,
                        "device_unreachable",
                        "Pair member missing",
                        params={"device_id": missing_id},
                    )
            reachable: list[Any] = []
            for device in (left, right):
                if device is None:
                    # force=true may process the surviving registered speaker.
                    continue
                group, ok = await self._group_safe(device)
                if not ok:
                    if force:
                        continue
                    raise_coded(
                        503,
                        "device_unreachable",
                        "Pair member is unreachable",
                        params={"device_id": device.device_id},
                    )
                if not group or group.group_id != group_id:
                    raise_coded(
                        409,
                        "stereo_pair_not_found",
                        "Pair membership changed",
                        params={"device_id": device.device_id},
                    )
                try:
                    leftovers = await self.backend.leftover(device.ip)
                except (OSError, TimeoutError):
                    raise_coded(
                        503,
                        "stereo_ssh_unreachable",
                        "Unable to inspect recovery files",
                        params={"device_id": device.device_id},
                    )
                if leftovers:
                    raise_coded(
                        409,
                        "stereo_recovery_needed",
                        "Recovery required",
                        params={"device_id": device.device_id},
                    )
                reachable.append(device)
            if not reachable:
                raise_coded(503, "device_unreachable", "Both speakers are unreachable")
            # Keep both members in the job's identity; only operate on reachable devices.
            return self._enqueue(
                "remove",
                ids,
                pair["name"],
                group_id,
                left if left in reachable else None,
                right if right in reachable else None,
            )
        except BaseException:
            self.busy.difference_update(ids)
            raise

    async def recover(self, device_id: str) -> dict[str, Any]:
        device = await self.repo.get_by_device_id(device_id)
        if device is None:
            raise_coded(
                404,
                "device_not_found",
                "Device not found",
                params={"device_id": device_id},
            )
        try:
            leftovers = await self.backend.leftover(device.ip)
        except (OSError, TimeoutError):
            raise_coded(
                503,
                "stereo_ssh_unreachable",
                "Unable to inspect recovery files",
                params={"device_id": device_id},
            )
        if not leftovers:
            raise_coded(
                409,
                "stereo_nothing_to_recover",
                "No recovery information found",
                params={"device_id": device_id},
            )
        record = await self.backend.journal_record(device.ip)
        if record is None:
            # A lone unfinished journal upload may be cleaned locally; the
            # backend refuses removal if any staged/backup files also exist.
            self._reserve([device_id])
            try:
                return self._enqueue("recover", [device_id], None, None, device, None)
            except BaseException:
                self.busy.discard(device_id)
                raise

        # Even a 'restored' or 'verified' journal may have a peer from the
        # same transaction that has not reached that state yet. Discover the
        # whole transaction before deciding whether cleanup can be unilateral.
        participants = []
        for candidate in await self.repo.get_all():
            if candidate.model.strip().casefold() != "soundtouch 10":
                continue
            try:
                peer_record = await self.backend.journal_record(candidate.ip)
            except Exception:
                raise_coded(
                    503, "device_unreachable", "Cannot inspect all stereo journals"
                )
            if peer_record and peer_record["job_id"] == record["job_id"]:
                participants.append((candidate, peer_record))
        if len(participants) == 1 and record["state"] in {
            "verified",
            "restored",
            "preparing",
        }:
            # An isolated preparation cannot have changed GroupService.xml.
            # For an already final decision, only artifacts remain.
            # The peer journal has already been cleaned. This device has a
            # durable final decision and only needs its own artifacts removed.
            self._reserve([device_id])
            try:
                return self._enqueue("recover", [device_id], None, None, device, None)
            except BaseException:
                self.busy.discard(device_id)
                raise
        if len(participants) != 2 or device_id not in {
            d.device_id for d, _record in participants
        }:
            raise_coded(
                409,
                "stereo_recovery_needed",
                "Both transaction journals are required for pair-wide recovery",
            )
        if any(p["operation"] != record["operation"] for _, p in participants):
            raise_coded(409, "stereo_recovery_needed", "Conflicting stereo journals")
        # Preserve the original creation order for LEFT/RIGHT verification.
        # Reboot and status verification work in either order; left identity
        # matters only when checking a successful GROUP_OK pair.
        participants.sort(key=lambda item: item[0].device_id)
        first, second = (participants[0][0], participants[1][0])
        ids = [first.device_id, second.device_id]
        self._reserve(ids)
        try:
            return self._enqueue("recover", ids, None, None, first, second)
        except BaseException:
            self.busy.difference_update(ids)
            raise

    async def _recover_pair(self, devices: list[Any]) -> None:
        """Coordinate one interrupted transaction across both persisted journals."""
        records = [await self.backend.journal_record(d.ip) for d in devices]
        if len(records) != 2:
            raise RuntimeError("stereo_recovery_needed")
        first, second = records
        if first is None or second is None:
            raise RuntimeError("stereo_recovery_needed")
        confirmed_records = (first, second)
        if (
            first["job_id"] != second["job_id"]
            or first["operation"] != second["operation"]
        ):
            raise RuntimeError("stereo_recovery_needed")
        job_id = first["job_id"]

        async def matching_final_state() -> bool:
            responses = await asyncio.gather(*(self._group_safe(d) for d in devices))
            if not all(ok for _group, ok in responses):
                return False
            groups = [group for group, _ok in responses]
            if first["operation"] == "remove":
                return all(g is None for g in groups)
            left_group, right_group = groups
            if left_group is None or right_group is None:
                return False
            return (
                left_group.group_id == right_group.group_id
                and left_group.master_device_id == right_group.master_device_id
                and left_group.name == right_group.name
                and left_group.status == right_group.status == "GROUP_OK"
                and len(left_group.roles) == 2
                and {(r.device_id, r.role) for r in left_group.roles}
                == {(r.device_id, r.role) for r in right_group.roles}
                and {r.role for r in left_group.roles} == {"LEFT", "RIGHT"}
                and {r.device_id for r in left_group.roles}
                == {d.device_id for d in devices}
            )

        if all(r["state"] == "restored" for r in confirmed_records):
            # Rollback already completed on both devices. Cleanup may have
            # deleted backup files before an SSH disconnect, so never read
            # those backups or reboot the speakers on this resume path.
            pass
        elif any(r["state"] == "verified" for r in confirmed_records):
            # A verified marker is written only after both speakers passed
            # the firmware-level check. Never undo one half of that decision.
            if any(r["state"] == "restored" for r in confirmed_records):
                raise RuntimeError("stereo_recovery_needed")
            if not await matching_final_state():
                raise RuntimeError("stereo_recovery_needed")
            for device in devices:
                await self.backend.mark_verified(device.ip, job_id)
        else:
            # Both backups must be readable before touching either speaker.
            from opencloudtouch.stereo.group_files import parse_group_xml

            previous = [
                parse_group_xml(
                    await self.backend.previous_content(d.ip) or "<group />"
                )
                for d in devices
            ]
            for device, record in zip(devices, confirmed_records):
                if record["state"] != "restored":
                    await self.backend.rollback(device.ip, job_id)
            # A write may have happened before its journal was updated; always
            # reboot after restoring original files to reload firmware state.
            await self._reboot(devices)
            await self._wait_reachable(devices)

            for attempt in range(6):
                responses = await asyncio.gather(
                    *(self._group_safe(d) for d in devices)
                )
                if all(
                    online
                    and (
                        (current is None and original is None)
                        or (
                            current is not None
                            and original is not None
                            and current.group_id == original.group_id
                            and current.master_device_id == original.master_device_id
                            and current.roles == original.roles
                            and current.name == original.name
                        )
                    )
                    for (current, online), original in zip(responses, previous)
                ):
                    break
                if attempt < 5:
                    await self._sleep(0 if self.mock_mode else 10)
            else:
                raise RuntimeError("stereo_rollback_failed")

            # Persist the rollback decision independently from successful
            # verification. A second crash must never reinterpret restored
            # original files as a successfully created stereo group.
            for device in devices:
                await self.backend.mark_restored(device.ip, job_id)

        # Both devices have the same final decision before either backup goes.
        for device in devices:
            await self.backend.cleanup(device.ip, job_id)

    async def _reboot(self, devices: list[Any]) -> None:
        if self.mock_mode:
            return
        results = await asyncio.gather(
            *(self.devices.reboot_device(d.device_id) for d in devices),
            return_exceptions=True,
        )
        for device, result in zip(devices, results):
            if isinstance(result, BaseException):
                raise StereoDeviceFailure(
                    "stereo_reboot_failed", device.device_id
                ) from result

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
                await self._sleep(2)
            else:
                raise StereoDeviceFailure("stereo_timeout", device.device_id)
            for _ in range(36):
                if await self._info_online(device):
                    return
                await self._sleep(5)
            raise StereoDeviceFailure("stereo_timeout", device.device_id)

        await asyncio.gather(*(wait_one(d) for d in devices))

    async def _verify(self, devices: list[Any], group_id: str | None) -> None:
        for _ in range(6):
            groups = await asyncio.gather(*(self._group_safe(d) for d in devices))
            if all(ok for _g, ok in groups):
                if group_id is None and all(g is None for g, _ok in groups):
                    return
                if group_id and all(
                    g is not None
                    and g.group_id == group_id
                    and g.status == "GROUP_OK"
                    and len(g.roles) == 2
                    and {r.role for r in g.roles} == {"LEFT", "RIGHT"}
                    and any(r.device_id == d.device_id for r in g.roles)
                    for d, (g, _ok) in zip(devices, groups)
                ):
                    # Full-pair verification also checks master and role identities.
                    if len(devices) == 1 or all(
                        g is not None
                        and g.master_device_id == devices[0].device_id
                        and {(r.device_id, r.role) for r in g.roles}
                        == {
                            (devices[0].device_id, "LEFT"),
                            (devices[1].device_id, "RIGHT"),
                        }
                        for g, _ok in groups
                    ):
                        return
            await self._sleep(10 if not self.mock_mode else 0)
        expected_roles = (
            {(devices[0].device_id, "LEFT"), (devices[1].device_id, "RIGHT")}
            if len(devices) == 2
            else None
        )
        for device, (group, reachable) in zip(devices, groups):
            if (
                not reachable
                or (group_id is None and group is not None)
                or (
                    group_id is not None
                    and (
                        group is None
                        or group.group_id != group_id
                        or group.status != "GROUP_OK"
                        or len(group.roles) != 2
                        or {role.role for role in group.roles} != {"LEFT", "RIGHT"}
                        or not any(
                            role.device_id == device.device_id for role in group.roles
                        )
                        or (
                            expected_roles is not None
                            and (
                                group.master_device_id != devices[0].device_id
                                or {(r.device_id, r.role) for r in group.roles}
                                != expected_roles
                            )
                        )
                    )
                )
            ):
                raise StereoDeviceFailure("stereo_verify_failed", device.device_id)
        raise RuntimeError("stereo_verify_failed")

    async def _verify_previous(
        self, devices: list[Any], previous: dict[str, GroupInfo | None]
    ) -> None:
        """Compare every speaker's actual group with its pre-job response."""
        for attempt in range(6):
            responses = await asyncio.gather(*(self._group_safe(d) for d in devices))
            if all(
                online and group == previous[d.device_id]
                for d, (group, online) in zip(devices, responses)
            ):
                return
            if attempt < 5:
                await self._sleep(0 if self.mock_mode else 10)
        for device, (group, reachable) in zip(devices, responses):
            if not reachable or group != previous[device.device_id]:
                raise StereoDeviceFailure("stereo_rollback_failed", device.device_id)
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
                await self._sleep(5)
            raise StereoDeviceFailure("stereo_timeout", device.device_id)

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
        """Restore every speaker reachable over SSH; keep unresolved journals."""
        job["step"] = "rolling_back"
        if not prepared:
            return True
        async with phase_budget(
            self.rollback_budget_seconds, "stereo_rollback_failed", clock=self.clock
        ):
            restored: list[Any] = []
            for device in reversed(prepared):
                try:
                    # HTTP may be down while SSH already works. Probe the
                    # persistence files independently on both speakers.
                    if await self.backend.leftover(device.ip) and committed:
                        await self.backend.rollback(device.ip, job["job_id"])
                    restored.append(device)
                except Exception:
                    job["failed_device_id"] = device.device_id

            if cancelled:
                # Shielded file-only restore: do not reboot or erase evidence.
                return False

            verifiable = restored
            if reboot_started:
                # Reboot only speakers that have been restored and currently
                # answer the device API. An SSH-only survivor may already have
                # its original file back but cannot yet be firmware-verified.
                responses = await asyncio.gather(
                    *(self._group_safe(d) for d in restored)
                )
                verifiable = [
                    d for d, (_group, online) in zip(restored, responses) if online
                ]
                if verifiable:
                    await self._reboot(verifiable)
                    await self._wait_reachable(verifiable)

            if verifiable:
                await self._verify_previous(verifiable, previous)

            # Only firmware-verified speakers can record a durable "restored"
            # decision. Retain every journal if any peer is still unresolved.
            for device in verifiable:
                if await self.backend.leftover(device.ip):
                    await self.backend.mark_restored(device.ip, job["job_id"])
            if len(verifiable) != len(prepared):
                if job["failed_device_id"] is None:
                    unresolved = next(
                        d for d in prepared if d not in verifiable
                    )
                    job["failed_device_id"] = unresolved.device_id
                return False

            # A complete verified rollback is the only case allowed to remove
            # both speakers' transaction backups and journals.
            for device in prepared:
                if await self.backend.leftover(device.ip):
                    await self.backend.cleanup(device.ip, job["job_id"])
        return True

    async def _execute(
        self,
        job: dict[str, Any],
        ids: list[str],
        left: Any | None,
        right: Any | None,
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
                async with phase_budget(
                    self.primary_budget_seconds, "stereo_timeout", clock=self.clock
                ):
                    job["step"] = "rolling_back"
                    if len(devices) == 2:
                        await self._recover_pair(devices)
                    else:
                        prior_state = await self.backend.journal_state(left.ip)
                        if not await self.backend.recover(left.ip):
                            raise RuntimeError("stereo_nothing_to_recover")
                        # Completed decisions and interrupted preparation need
                        # artifact cleanup only, without another reboot.
                        if prior_state not in {
                            "verified",
                            "restored",
                            None,
                            "preparing",
                        }:
                            job["step"] = "rebooting"
                            await self._reboot(devices)
                            job["step"] = "waiting_for_devices"
                            await self._wait_online(devices)
            else:
                if operation == "create" and (left is None or right is None):
                    raise RuntimeError("stereo_device_invalid")
                async with phase_budget(
                    self.primary_budget_seconds, "stereo_timeout", clock=self.clock
                ):
                    # Capture the exact old firmware state on every processed
                    # device; a rollback must restore that state, not merely
                    # pass the verification rules of the opposite operation.
                    for device in devices:
                        group, online = await self._group_safe(device)
                        if not online:
                            raise StereoDeviceFailure(
                                "device_unreachable", device.device_id
                            )
                        previous[device.device_id] = group
                    job["step"] = "backing_up"
                    for device in devices:
                        xml = None
                        if operation == "create":
                            assert left is not None and right is not None
                            xml = build_group_xml(
                                job["group_id"],
                                job["name"],
                                left.device_id,
                                left.ip,
                                right.device_id,
                                right.ip,
                                right_file=(device.device_id == right.device_id),
                            )
                        # Journal creation can succeed before prepare fails.
                        prepared.append(device)
                        try:
                            await self.backend.prepare(
                                device.ip, job["job_id"], operation, xml
                            )
                        except Exception as exc:
                            raise StereoDeviceFailure(
                                "stereo_write_failed", device.device_id
                            ) from exc
                    job["step"] = "writing_files"
                    for device in devices:
                        committed = True
                        try:
                            await self.backend.commit(
                                device.ip, job["job_id"], removing=operation == "remove"
                            )
                        except Exception as exc:
                            raise StereoDeviceFailure(
                                "stereo_write_failed", device.device_id
                            ) from exc
                    job["step"] = "rebooting"
                    reboot_started = True
                    await self._reboot(devices)
                    job["step"] = "waiting_for_devices"
                    await self._wait_online(devices)
                    job["step"] = "verifying"
                    await self._verify(
                        devices, job["group_id"] if operation == "create" else None
                    )
                    # Both firmware responses now confirm the final pair.
                    # From here onward, a journal/SSH failure must not trigger
                    # an asymmetric rollback after the first verified marker.
                    verified = True
                    for device in devices:
                        await self.backend.mark_verified(device.ip, job["job_id"])
                    for device in devices:
                        await self.backend.cleanup(device.ip, job["job_id"])
            job["state"] = "succeeded"
            job["step"] = "done"
        except BaseException as exc:
            cancelled = isinstance(exc, asyncio.CancelledError)
            logger.warning(
                "Stereo job %s failed: %s", job["job_id"], type(exc).__name__
            )
            job["state"] = "failed"
            job["error_code"] = (
                str(exc)
                if str(exc).startswith(("stereo_", "device_"))
                else "stereo_ssh_failed"
            )
            if isinstance(exc, StereoDeviceFailure):
                job["failed_device_id"] = exc.device_id
            if verified:
                # No asymmetric rollback after verification. The remaining
                # verified journal can be cleaned by explicit recovery.
                job["rolled_back"] = False
            elif prepared:
                # Own the rollback task explicitly. On shutdown, shield() by
                # itself would return immediately on cancellation and leave an
                # orphan rollback task running after the device lock is freed.
                rollback_task = asyncio.create_task(
                    self._rollback_job(
                        job,
                        prepared,
                        committed=committed,
                        reboot_started=reboot_started and not cancelled,
                        previous=previous,
                        cancelled=cancelled,
                    )
                )
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
                logger.warning(
                    "Stereo job cancelled; recovery files retained if needed"
                )
        finally:
            job["finished_at"] = self._now()

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
