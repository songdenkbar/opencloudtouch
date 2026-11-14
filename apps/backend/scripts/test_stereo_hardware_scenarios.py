"""Guided, opt-in hardware checks for stereo spec #549.

Read-only by default. The only device-changing actions use OCT's stereo REST
API and require --execute. Power disconnection and OCT restarts are manual.
Never run this against speakers whose original GroupService.xml was not backed up.

Examples (from apps/backend):
    PYTHONPATH=src python scripts/test_stereo_hardware_scenarios.py \
        --api http://127.0.0.1:7778 \
        --left-id LEFT_ID --left-ip LEFT_IP \
        --right-id RIGHT_ID --right-ip RIGHT_IP \
        --left-original absent --right-original absent \
        --phase baseline

    # Explicitly start creation, then monitor the job:
    ... --phase create --execute
    ... --phase audit --job-id JOB_ID
    ... --phase remove --group-id GROUP_ID --execute

    # For outage: start creation with --phase create --execute while ready
    # to manually disconnect RIGHT when the rebooting step is announced.
    # Next run --phase audit; restart OCT manually; rerun --phase audit.
    # Only invoke --phase recover --execute after inspecting BOTH journals.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException

from opencloudtouch.setup.ssh_client import SoundTouchSSHClient
from opencloudtouch.stereo.group_backend import SSHGroupBackend
from opencloudtouch.stereo.group_files import GROUP_FILE, parse_group_xml, validate_device

STEPS = {
    "validating", "backing_up", "writing_files", "rebooting",
    "waiting_for_devices", "verifying", "rolling_back", "done",
}


async def get_json(client: httpx.AsyncClient, path: str) -> dict[str, Any]:
    response = await client.get(path)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError("Expected a JSON object from OCT")
    return data


async def read_group_file(ip: str) -> bytes | None:
    ssh = SoundTouchSSHClient(ip)
    try:
        connected = await ssh.connect()
        if not connected.success:
            raise OSError("SSH unavailable")
        exists = await ssh.execute(
            f"if test -f {GROUP_FILE}; then echo yes; else echo no; fi"
        )
        if not exists.success or exists.output.strip() not in {"yes", "no"}:
            raise OSError("Cannot inspect GroupService.xml")
        if exists.output.strip() == "no":
            return None
        data = await ssh.execute(f"base64 {GROUP_FILE}")
        if not data.success:
            raise OSError("Cannot read GroupService.xml")
        return base64.b64decode("".join(data.output.split()), validate=True)
    finally:
        await ssh.close()


async def device_group(ip: str) -> tuple[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=2)) as client:
        try:
            response = await client.get(f"http://{ip}:8090/getGroup")
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            if root.tag != "group":
                return "malformed", None
            if not root.attrib and not list(root) and not (root.text or "").strip():
                return "empty", None
            group = parse_group_xml(response.content)
            if group is None:
                return "malformed", None
            return "paired", group
        except (httpx.HTTPError, OSError):
            return "unreachable", None
        except (ValueError, DefusedXmlException, ElementTree.ParseError):
            return "malformed", None


async def device_zone(ip: str) -> str:
    async with httpx.AsyncClient(timeout=httpx.Timeout(6, connect=2)) as client:
        try:
            response = await client.get(f"http://{ip}:8090/getZone")
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
            if root.tag != "zone":
                return "malformed"
            return "empty" if not root.attrib and not list(root) else "occupied"
        except (httpx.HTTPError, OSError):
            return "unreachable"
        except (ValueError, DefusedXmlException, ElementTree.ParseError):
            return "malformed"


def expected_file(value: str) -> bytes | None:
    if value == "absent":
        return None
    file = Path(value).expanduser()
    if not file.is_file():
        raise ValueError(f"Expected backup file not found: {file}")
    data = file.read_bytes()
    if not data:
        raise ValueError(f"Expected backup is empty: {file}")
    return data


async def snapshot(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any]]:
    result: dict[str, Any] = {}
    backend = SSHGroupBackend()
    for side in ("left", "right"):
        ip = getattr(args, side + "_ip")
        group_state, group = await device_group(ip)
        zone_state = await device_zone(ip)
        info: dict[str, Any] = {
            "device_id": getattr(args, side + "_id"),
            "group_http": group_state,
            "zone_http": zone_state,
            "group_id": group.group_id if group else None,
            "master_device_id": group.master_device_id if group else None,
            "name": group.name if group else None,
            "roles": [[r.device_id, r.role, r.ip_address] for r in group.roles] if group else [],
            "status": group.status if group else None,
        }
        try:
            current = await read_group_file(ip)
            info["file_state"] = "absent" if current is None else "present"
            import hashlib
            info["file_sha256"] = hashlib.sha256(current).hexdigest() if current is not None else None
            info["matches_original"] = current == expected_file(
                getattr(args, side + "_original")
            )
            info["leftovers"] = await backend.leftover(ip)
            info["journal"] = await backend.journal_record(ip) if info["leftovers"] else None
        except Exception as exc:
            info["ssh_error"] = type(exc).__name__
        result[side] = info

    async with httpx.AsyncClient(base_url=args.api, timeout=15) as client:
        try:
            oct_state = await get_json(client, "/api/stereo-pairs")
        except (httpx.HTTPError, ValueError) as exc:
            oct_state = {"error": type(exc).__name__}
        result["oct"] = oct_state
        job = None
        if args.job_id:
            try:
                job = await get_json(client, f"/api/stereo-pairs/jobs/{args.job_id}")
            except (httpx.HTTPError, ValueError) as exc:
                job = {"error": type(exc).__name__}
    return result, job or {}


def print_snapshot(data: dict[str, Any], job: dict[str, Any]) -> None:
    for side in ("left", "right"):
        d = data[side]
        print(
            f"{side.upper()}: /getGroup={d['group_http']}, "
            f"/getZone={d['zone_http']}, file={d.get('file_state', 'unknown')}, "
            f"original={d.get('matches_original', 'unknown')}, "
            f"leftovers={d.get('leftovers', 'unknown')}, "
            f"journal_state={(d.get('journal') or {}).get('state')}"
        )
        if d.get("ssh_error"):
            print(f"  SSH inspection unavailable: {d['ssh_error']}")
        if d["group_id"]:
            print(f"  group={d['group_id']} master={d['master_device_id']} status={d['status']}")
    oct_state = data["oct"]
    if "error" in oct_state:
        print("OCT API unavailable:", oct_state["error"])
    else:
        print("OCT pair count:", len(oct_state.get("pairs", [])))
        print("OCT unreachable ids:", oct_state.get("unreachable_device_ids", []))
        print("OCT recovery-needed ids:", oct_state.get("recovery_needed_device_ids", []))
        for pair in oct_state.get("pairs", []):
            print("  pair:", json.dumps(pair, ensure_ascii=False))
    if job:
        print("Job:", json.dumps(job, ensure_ascii=False))


def check_phase(args: argparse.Namespace, data: dict[str, Any]) -> bool:
    left, right = data["left"], data["right"]
    if args.phase in {"baseline", "final"}:
        ok = all(
            d.get("matches_original") is True
            and d["group_http"] == "empty"
            and d["zone_http"] == "empty"
            and d.get("leftovers") is False
            for d in (left, right)
        )
        print("Baseline/final original-state check:", "PASS" if ok else "NOT VERIFIED")
        return ok

    if args.phase == "paired":
        reports = (left, right)
        expected_roles = {
            (args.left_id, "LEFT"), (args.right_id, "RIGHT")
        }
        ids = {d["group_id"] for d in reports}
        ok = (
            all(d["group_http"] == "paired" and d["status"] == "GROUP_OK" for d in reports)
            and len(ids) == 1 and None not in ids
            and all(d["master_device_id"] == args.left_id for d in reports)
            and all({(r[0], r[1]) for r in d["roles"]} == expected_roles for d in reports)
            and all(d.get("leftovers") is False for d in reports)
        )
        print("Both speakers agree on stereo pair:", "PASS" if ok else "NOT VERIFIED")
        print("Audible LEFT/RIGHT separation still requires a listening test.")
        return ok

    if args.phase in {"audit", "after-outage", "after-restart"}:
        print("READ-ONLY AUDIT: inspect the two journals and job status together.")
        print("Do not run another create/remove while either speaker has leftovers.")
        return True

    return True


async def wait_job(client: httpx.AsyncClient, job_id: str, limit: int) -> dict[str, Any]:
    deadline = time.monotonic() + limit
    last_step = None
    while time.monotonic() < deadline:
        try:
            job = await get_json(client, f"/api/stereo-pairs/jobs/{job_id}")
        except httpx.HTTPError as exc:
            print("OCT became unreachable during job:", type(exc).__name__)
            raise RuntimeError("Job polling interrupted; restart OCT and audit state") from exc
        step = job.get("step")
        if step not in STEPS:
            raise RuntimeError("Unexpected job step; aborting polling")
        if step != last_step:
            print(f"Job {job_id}: {job.get('state')} / {step}", flush=True)
            if step == "rebooting":
                print(
                    "OUTAGE SCENARIO ONLY: manually disconnect RIGHT NOW "
                    "if this is the deliberately planned fault-injection run.",
                    flush=True,
                )
            last_step = step
        if job.get("state") != "running":
            print("Job result:", json.dumps(job, ensure_ascii=False))
            return job
        await asyncio.sleep(0.5)
    raise RuntimeError("Polling deadline reached; job may still be running")


async def mutate(args: argparse.Namespace) -> None:
    if not args.execute:
        raise ValueError("API write actions require explicit --execute")
    if args.phase == "create":
        pre, _ = await snapshot(args)
        if not check_baseline(pre):
            raise RuntimeError("Preflight refused: speakers are not in known original state")
        method, path = "POST", "/api/stereo-pairs"
        payload = {
            "left_device_id": args.left_id,
            "right_device_id": args.right_id,
            "name": args.name,
        }
    elif args.phase == "remove":
        if not args.group_id:
            raise ValueError("--group-id required for remove")
        method, path, payload = "DELETE", f"/api/stereo-pairs/{args.group_id}", None
    else:
        if not args.recover_id or args.recover_id not in {args.left_id, args.right_id}:
            raise ValueError("--recover-id must match one of the two speakers")
        method, path, payload = (
            "POST", f"/api/stereo-pairs/recover/{args.recover_id}", None
        )
        before, _ = await snapshot(args)
        print_snapshot(before, {})
        print("Manual confirmation needed: BOTH journals and matching backups reviewed.")
        if not args.confirm_recovery:
            raise ValueError("Recovery also requires --confirm-recovery")
    async with httpx.AsyncClient(base_url=args.api, timeout=20) as client:
        response = await client.request(method, path, json=payload)
        response.raise_for_status()
        job = response.json()
        if response.status_code != 202 or not isinstance(job, dict) or not job.get("job_id"):
            raise RuntimeError("OCT did not return a stereo job")
        print("Created job id:", job["job_id"], flush=True)
        await wait_job(client, job["job_id"], args.poll_seconds)
    after, job = await snapshot(args)
    print_snapshot(after, job)


def check_baseline(data: dict[str, Any]) -> bool:
    return all(
        d.get("matches_original") is True
        and d["group_http"] == "empty"
        and d["zone_http"] == "empty"
        and d.get("leftovers") is False
        for d in (data["left"], data["right"])
    )


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:7778")
    for side in ("left", "right"):
        parser.add_argument(f"--{side}-id", required=True)
        parser.add_argument(f"--{side}-ip", required=True)
        parser.add_argument(f"--{side}-original", required=True)
    parser.add_argument("--phase", required=True, choices=[
        "baseline", "paired", "audit", "after-outage", "after-restart",
        "final", "create", "remove", "recover",
    ])
    parser.add_argument("--job-id")
    parser.add_argument("--group-id")
    parser.add_argument("--recover-id")
    parser.add_argument("--confirm-recovery", action="store_true")
    parser.add_argument("--name", default="OCT Stereo Hardware Test")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=650)
    args = parser.parse_args()
    for side in ("left", "right"):
        validate_device(getattr(args, side + "_id"), getattr(args, side + "_ip"))
        expected_file(getattr(args, side + "_original"))
    if args.left_id == args.right_id or args.left_ip == args.right_ip:
        parser.error("LEFT and RIGHT must be distinct devices and addresses")
    if args.phase in {"create", "remove", "recover"}:
        await mutate(args)
        return 0
    data, job = await snapshot(args)
    print_snapshot(data, job)
    return 0 if check_phase(args, data) else 2


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        print("STOP:", exc, file=sys.stderr)
        sys.exit(2)
