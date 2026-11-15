"""Opt-in in-memory Marge group endpoints for native ST10 pairing research.

Enabled only with OCT_NATIVE_STEREO_PROBE=1. One process, no persistence.
Never writes files to speakers. Do not expose this to untrusted networks.
"""
import logging
from datetime import timezone
from typing import Any

from opencloudtouch.core.source_types import BASE_SOURCES
from opencloudtouch.marge.xml_builder import _build_orion_location
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


def _probe_source_xml(source: Any) -> XML.Element:
    """Represent an OCT source in the SoundCork-compatible Marge account format."""
    elem = XML.Element("source", {"id": source.streaming_id, "type": "Audio"})
    XML.SubElement(elem, "createdOn").text = source.created_on
    XML.SubElement(elem, "credential", {"type": source.secret_type})
    XML.SubElement(elem, "name").text = source.account
    XML.SubElement(elem, "sourceproviderid").text = source.streaming_id
    XML.SubElement(elem, "sourcename").text = source.display_name
    XML.SubElement(elem, "sourceSettings")
    XML.SubElement(elem, "updatedOn").text = source.created_on
    XML.SubElement(elem, "username").text = source.account
    return elem


def _probe_preset_xml(preset: Any) -> XML.Element:
    """Convert an OCT radio preset into the Marge account-level wire format."""
    src = next(
        (src for src in BASE_SOURCES if src.source_type == (preset.source or "LOCAL_INTERNET_RADIO")),
        None,
    )
    if src is None:
        raise ValueError(f"Unsupported probe preset source: {preset.source!r}")
    location = preset.station_url or ""
    if src.source_type != "TUNEIN":
        location = _build_orion_location(location, preset.station_name, preset.station_favicon or "")
    elem = XML.Element("preset", {"buttonNumber": str(preset.preset_number)})
    XML.SubElement(elem, "containerArt").text = preset.station_favicon or ""
    XML.SubElement(elem, "contentItemType").text = "stationurl"
    XML.SubElement(elem, "createdOn").text = preset.created_at.astimezone(timezone.utc).isoformat()
    XML.SubElement(elem, "location").text = location
    XML.SubElement(elem, "name").text = preset.station_name
    XML.SubElement(elem, "username").text = preset.station_name
    elem.append(_probe_source_xml(src))
    XML.SubElement(elem, "updatedOn").text = preset.updated_at.astimezone(timezone.utc).isoformat()
    return elem


def probe_full_account_xml(
    account: str,
    *,
    device: Any | None = None,
    presets: list[Any] | None = None,
) -> XML.Element:
    """Build the ST10 Marge full-account reply following SoundCork's nesting.

    No synthetic device is emitted when device is unknown; callers must
    resolve the account first rather than synchronize an empty device list.
    """
    if not account.isdigit() or not 1 <= len(account) <= 20:
        raise HTTPException(422, "Invalid account")
    root = XML.Element("account", {"id": account})
    XML.SubElement(root, "accountStatus").text = "OK"
    devices = XML.SubElement(root, "devices")
    if device is not None:
        element = XML.SubElement(devices, "device", {"deviceid": device.device_id})
        product = XML.SubElement(element, "attachedProduct", {"product_code": device.model})
        XML.SubElement(product, "components")
        XML.SubElement(product, "productlabel").text = device.model
        XML.SubElement(element, "firmwareVersion").text = device.firmware_version
        XML.SubElement(element, "ipaddress").text = device.ip
        XML.SubElement(element, "name").text = device.name
        preset_list = XML.SubElement(element, "presets")
        for preset in presets or []:
            preset_list.append(_probe_preset_xml(preset))
        XML.SubElement(element, "recents")
    XML.SubElement(root, "mode").text = "global"
    XML.SubElement(root, "preferredLanguage").text = "en"
    settings = XML.SubElement(root, "providerSettings")
    setting = XML.SubElement(settings, "providerSetting")
    XML.SubElement(setting, "boseId").text = account
    XML.SubElement(setting, "keyName").text = "ELIGIBLE_FOR_TRIAL"
    XML.SubElement(setting, "value").text = "true"
    XML.SubElement(setting, "providerId").text = "14"
    sources = XML.SubElement(root, "sources")
    for src in BASE_SOURCES:
        sources.append(_probe_source_xml(src))
    return root

def _created_group_response(account: str, group: StoredGroup) -> Response:
    """Return a firmware-compatible ST10 Marge group creation response."""
    return Response(
        group.body,
        status_code=201,
        media_type="application/vnd.bose.streaming-v1.2+xml",
        headers={"Location": f"/streaming/account/{account}/group/{group.group_id}"},
    )


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
            return _created_group_response(account, group)
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
    return _created_group_response(account, _groups[(account, gid)])


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
