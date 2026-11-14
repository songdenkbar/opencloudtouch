"""Persistent Bose SoundTouch 10 stereo group format (issue #549).

Pure XML generation and parsing only: no SSH writes or reboots happen here.
The left speaker is the master; only the right file has GROUP_OK status.
"""

from __future__ import annotations

from unicodedata import category
from ipaddress import ip_address
import re
from xml.sax.saxutils import escape

from defusedxml import ElementTree

from opencloudtouch.devices.client import GroupInfo, GroupRole
from defusedxml.common import DefusedXmlException

_DEVICE_ID = re.compile(r"^[A-Za-z0-9]{12}$")
_GROUP_ID = re.compile(r"^[0-9]{1,12}$")
GROUP_DIRECTORY = "/mnt/nv/BoseApp-Persistence/1"
GROUP_FILE = GROUP_DIRECTORY + "/GroupService.xml"


def validate_device(device_id: str, ip: str) -> None:
    if not _DEVICE_ID.fullmatch(device_id):
        raise ValueError("Invalid SoundTouch device ID")
    ip_address(ip)


def validate_name(name: str) -> str:
    clean = name.strip()
    if not 1 <= len(clean) <= 30 or any(category(c) == "Cc" for c in clean):
        raise ValueError("Stereo pair name must have 1-30 printable characters")
    return clean


def build_group_xml(
    group_id: str,
    name: str,
    left_device_id: str,
    left_ip: str,
    right_device_id: str,
    right_ip: str,
    *,
    right_file: bool,
) -> str:
    """Create the tested left/right file layout, validating all XML values."""
    if not _GROUP_ID.fullmatch(group_id):
        raise ValueError("Invalid stereo group ID")
    if left_device_id == right_device_id:
        raise ValueError("Stereo devices must differ")
    validate_device(left_device_id, left_ip)
    validate_device(right_device_id, right_ip)
    safe_name = escape(validate_name(name))
    status = "  <status>GROUP_OK</status>\n" if right_file else ""
    return (
        '<?xml version="1.0" encoding="UTF-8" ?>\n'
        f'<group id="{group_id}">\n'
        f"  <name>{safe_name}</name>\n"
        f"  <masterDeviceId>{left_device_id}</masterDeviceId>\n"
        "  <roles>\n"
        f"    <groupRole><deviceId>{left_device_id}</deviceId>"
        f"<role>LEFT</role><ipAddress>{left_ip}</ipAddress></groupRole>\n"
        f"    <groupRole><deviceId>{right_device_id}</deviceId>"
        f"<role>RIGHT</role><ipAddress>{right_ip}</ipAddress></groupRole>\n"
        "  </roles>\n"
        f"  <senderIPAddress>{left_ip}</senderIPAddress>\n"
        f"{status}</group>\n"
    )


def parse_group_xml(xml: str | bytes) -> GroupInfo | None:
    """Parse a /getGroup response, returning None for empty/unsafe/invalid XML."""
    try:
        root = ElementTree.fromstring(xml)
        if root.tag != "group" or not root.get("id"):
            return None
        group_id = root.attrib["id"]
        master = root.findtext("masterDeviceId")
        if not master:
            return None
        roles = []
        for node in root.findall("./roles/groupRole"):
            device_id = node.findtext("deviceId")
            role = node.findtext("role")
            if device_id and role:
                roles.append(
                    GroupRole(
                        device_id=device_id,
                        role=role,
                        ip_address=node.findtext("ipAddress"),
                    )
                )
        return GroupInfo(
            group_id=group_id,
            name=root.findtext("name"),
            master_device_id=master,
            roles=roles,
            status=root.findtext("status"),
        )
    except (ValueError, TypeError, ElementTree.ParseError, DefusedXmlException):
        return None
