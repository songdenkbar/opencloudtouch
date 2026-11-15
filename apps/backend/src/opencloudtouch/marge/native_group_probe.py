"""Opt-in in-memory Marge group endpoints for native ST10 pairing research.

Enabled only with OCT_NATIVE_STEREO_PROBE=1. One process, no persistence.
Never writes files to speakers. Do not expose this to untrusted networks.
"""
import logging
import secrets
from dataclasses import dataclass
from xml.etree import ElementTree as XML

from defusedxml import ElementTree as SafeXML
from fastapi import APIRouter, HTTPException, Request, Response

logger = logging.getLogger(__name__)
router = APIRouter(tags=["experimental-stereo"])


@dataclass
class StoredGroup:
    account: str
    group_id: str
    master: str
    devices: frozenset[str]
    body: bytes


_groups: dict[tuple[str, str], StoredGroup] = {}


def _parse_group(body: bytes) -> tuple[XML.Element, str, frozenset[str]]:
    if len(body) > 8192:
        raise HTTPException(413, "Group payload too large")
    try:
        root = SafeXML.fromstring(body)
    except (SafeXML.ParseError, ValueError) as exc:
        raise HTTPException(422, "Invalid group XML") from exc
    if root.tag != "group":
        raise HTTPException(422, "Expected group XML")
    master = (root.findtext("masterDeviceId") or "").strip().upper()
    roles = root.findall("./roles/groupRole")
    role_map = {(r.findtext("role") or "").strip().upper():
                (r.findtext("deviceId") or "").strip().upper() for r in roles}
    if len(roles) != 2 or set(role_map) != {"LEFT", "RIGHT"} or not master or role_map["LEFT"] != master:
        raise HTTPException(422, "Expected one LEFT master and one RIGHT device")
    devices = frozenset(role_map.values())
    if len(devices) != 2 or any(
        len(d) != 12 or any(c not in "0123456789ABCDEF" for c in d)
        for d in devices
    ):
        raise HTTPException(422, "Invalid group device IDs")
    return root, master, devices


@router.post("/streaming/account/{account}/group/")
@router.post("/streaming/account/{account}/group", include_in_schema=False)
async def create_marge_group(account: str, request: Request) -> Response:
    if not account.isdigit() or not 1 <= len(account) <= 20:
        raise HTTPException(422, "Invalid account")
    root, master, devices = _parse_group(await request.body())
    # Reuse the group ID if the master forwards an existing creation request.
    for group in _groups.values():
        if group.account == account and group.master == master and group.devices == devices:
            logger.info("[NATIVE-STEREO-PROBE] Reusing group %s", group.group_id)
            return Response(group.body, media_type="application/vnd.bose.streaming-v1.2+xml")
    if any(group.account == account and group.devices & devices for group in _groups.values()):
        raise HTTPException(409, "Speaker is already in a probe group")
    gid = str(secrets.randbelow(9_000_000) + 1_000_000)
    while (account, gid) in _groups:
        gid = str(secrets.randbelow(9_000_000) + 1_000_000)
    root.set("id", gid)
    for tag in ("senderIPAddress", "status"):
        for el in root.findall(tag):
            root.remove(el)
    payload = XML.tostring(root, encoding="utf-8", xml_declaration=True)
    _groups[(account, gid)] = StoredGroup(account, gid, master, devices, payload)
    logger.info("[NATIVE-STEREO-PROBE] Created group %s for master %s", gid, master)
    return Response(payload, media_type="application/vnd.bose.streaming-v1.2+xml")


@router.get("/streaming/account/{account}/device/{device}/group")
async def get_marge_group(account: str, device: str) -> Response:
    for group in _groups.values():
        if group.account == account and device.upper() in group.devices:
            return Response(group.body, media_type="application/vnd.bose.streaming-v1.2+xml")
    return Response(b"<group />", media_type="application/vnd.bose.streaming-v1.2+xml")


@router.delete("/streaming/account/{account}/group/{gid}")
async def delete_marge_group(account: str, gid: str) -> Response:
    _groups.pop((account, gid), None)
    logger.info("[NATIVE-STEREO-PROBE] Deleted server group %s", gid)
    return Response(b"<status>GROUP_OK</status>", media_type="application/xml")


@router.get("/api/experimental/native-stereo/groups")
async def inspect_probe_groups() -> dict:
    return {"groups": [
        {"account": g.account, "id": g.group_id, "master": g.master, "devices": sorted(g.devices)}
        for g in _groups.values()
    ]}
