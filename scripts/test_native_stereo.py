"""SoundTouch 10 native stereo probe; no SSH writes.

First run without --execute to inspect. For a state-changing test use --execute.
Examples:
 python scripts/test_native_stereo.py --oct http://192.168.55.45:7778 --left-ip 192.168.55.26 --right-ip 192.168.55.68
 python scripts/test_native_stereo.py --oct http://192.168.55.45:7778 --left-ip 192.168.55.26 --right-ip 192.168.55.68 --execute
 python scripts/test_native_stereo.py --oct http://192.168.55.45:7778 --left-ip 192.168.55.26 --right-ip 192.168.55.68 --remove
 python scripts/test_native_stereo.py --oct http://192.168.55.45:7778 --left-ip 192.168.55.26 --right-ip 192.168.55.68 --remove --execute
"""
import argparse
import asyncio
import ipaddress
from xml.etree import ElementTree as ET

import httpx
from defusedxml import ElementTree as SafeXML


def group_xml(name: str, left_id: str, left_ip: str, right_id: str, right_ip: str,
              *, group_id: str | None = None, sender: bool = False) -> bytes:
    root = ET.Element("group")
    if group_id:
        root.set("id", group_id)
    ET.SubElement(root, "name").text = name
    ET.SubElement(root, "masterDeviceId").text = left_id
    roles = ET.SubElement(root, "roles")
    for device_id, role, ip in ((left_id, "LEFT", left_ip), (right_id, "RIGHT", right_ip)):
        element = ET.SubElement(roles, "groupRole")
        ET.SubElement(element, "deviceId").text = device_id
        ET.SubElement(element, "role").text = role
        ET.SubElement(element, "ipAddress").text = ip
    if sender:
        ET.SubElement(root, "senderIPAddress").text = left_ip
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def inspect_existing_group(body: bytes, left_id: str, right_id: str) -> str | None:
    """Return group ID only for a stereo pair containing exactly our two devices.

    Empty groups are allowed (a partially formed pair). Never remove an
    unrelated group, an unexpected speaker, or an unrecognised group format.
    """
    root = SafeXML.fromstring(body)
    if root.tag != "group":
        raise RuntimeError("Unexpected /getGroup response")
    if not root.get("id") and not list(root):
        return None
    group_id = root.get("id")
    roles = root.findall("./roles/groupRole")
    actual_roles = {
        (r.findtext("role") or "").strip().upper():
        (r.findtext("deviceId") or "").strip().upper()
        for r in roles
    }
    master = (root.findtext("masterDeviceId") or "").strip().upper()
    if (not group_id or not group_id.isdigit() or len(roles) != 2
            or actual_roles != {"LEFT": left_id, "RIGHT": right_id}
            or master != left_id):
        raise RuntimeError("Group is not the requested LEFT/RIGHT pair; refusing removal")
    return group_id


async def remove_pair(client: httpx.AsyncClient, left: str, right: str,
                      left_id: str, right_id: str, oct_url: str,
                      account: str, execute: bool) -> None:
    """Use native /removeGroup; only delete the server record after both clear."""
    group_ids = []
    for ip in (left, right):
        response = (await client.get(f"http://{ip}:8090/getGroup")).raise_for_status()
        gid = inspect_existing_group(response.content, left_id, right_id)
        print(f"{ip}: group={gid or 'empty'}")
        group_ids.append(gid)
    ids = {g for g in group_ids if g is not None}
    if len(ids) > 1:
        raise RuntimeError("Speakers report different group IDs; refusing removal")
    if not ids:
        print("Both speakers already unpaired; nothing to remove.")
        return
    gid = ids.pop()
    print(f"Native remove target: group {gid}, LEFT={left}, RIGHT={right}")
    if not execute:
        print("DRY RUN: No changes. Add --execute to remove this pair.")
        return
    # Normal Bose workflow: master propagates removal to the slave.
    # Only retry directly on the slave when its group survives that request.
    for ip in (left, right):
        if ip == right:
            current = (await client.get(f"http://{right}:8090/getGroup")).raise_for_status()
            if inspect_existing_group(current.content, left_id, right_id) is None:
                print("RIGHT already unpaired by master; no separate removal needed.")
                break
        response = await client.get(f"http://{ip}:8090/removeGroup", timeout=25)
        print(f"{ip} /removeGroup: HTTP {response.status_code} {response.text[:700]}")
        response.raise_for_status()
    remaining = []
    for ip in (left, right):
        response = (await client.get(f"http://{ip}:8090/getGroup")).raise_for_status()
        current = inspect_existing_group(response.content, left_id, right_id)
        print(f"{ip} /getGroup after remove: {response.text[:700]}")
        if current is not None:
            remaining.append(ip)
    if remaining:
        print(f"WARNING: Group remains on {remaining}; server entry retained.")
        return
    # Delete only the matching entry from this opt-in, in-memory OCT probe.
    try:
        probe = (await client.get(
            f"{oct_url.rstrip('/')}/api/experimental/native-stereo/groups"
        )).raise_for_status().json()
        match = [g for g in probe["groups"] if g["id"] == gid and g["account"] == account
                 and set(g["devices"]) == {left_id, right_id}]
        if match:
            delete_url = f"{oct_url.rstrip('/')}/streaming/account/{account}/group/{gid}"
            deleted = (await client.delete(delete_url)).raise_for_status()
            print(f"Probe server group deleted: HTTP {deleted.status_code}")
        else:
            print("No matching group in the in-memory probe (possibly after a server restart).")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        print(f"WARNING: Probe server cleanup was not checked: {exc}")
        print("If a group was created by this probe, inspect its server state separately.")
    print("Both speakers are unpaired; removal verified.")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--oct", required=True, help="Local dev backend URL")
    ap.add_argument("--left-ip", required=True)
    ap.add_argument("--right-ip", required=True)
    ap.add_argument("--account", help="Optional LEFT margeAccountUUID override")
    ap.add_argument("--name", default="Native Stereo Probe")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--remove", action="store_true", help="Remove only this verified LEFT/RIGHT pair")
    args = ap.parse_args()
    left, right = (str(ipaddress.ip_address(ip)) for ip in (args.left_ip, args.right_ip))
    if left == right or (args.account is not None and not args.account.isdigit()):
        ap.error("Different IPs required; account override must be numeric")
    async with httpx.AsyncClient(timeout=12, trust_env=False) as client:
        device_ids = []
        for ip in (left, right):
            base = f"http://{ip}:8090"
            response = (await client.get(f"{base}/info")).raise_for_status()
            root = SafeXML.fromstring(response.content)
            device_id = root.get("deviceID") or root.findtext("deviceID")
            model = root.findtext("type")
            account = root.findtext("margeAccountUUID")
            print(f"{ip}: ID={device_id}, model={model}, margeAccountUUID={account}")
            if model != "SoundTouch 10" or not device_id or len(device_id) != 12:
                raise RuntimeError(f"Device {ip} is not an identified SoundTouch 10")
            if ip == left:
                if args.account and account and account != args.account:
                    raise RuntimeError("LEFT account override differs from speaker /info")
                args.account = args.account or account
                if not args.account or not args.account.isdigit():
                    raise RuntimeError("LEFT speaker missing numeric margeAccountUUID")
            group = (await client.get(f"{base}/getGroup")).raise_for_status()
            print(f"{ip}: /getGroup before = {group.text[:500]}")
            parsed = SafeXML.fromstring(group.content)
            if parsed.tag != "group":
                raise RuntimeError(f"Unexpected /getGroup response from {ip}")
            if not args.remove and (parsed.get("id") or list(parsed)):
                raise RuntimeError(f"Device {ip} already belongs to a group")
            device_ids.append(device_id.upper())
        if args.remove:
            await remove_pair(client, left, right, device_ids[0], device_ids[1],
                              args.oct, args.account, args.execute)
            return
        xml = group_xml(args.name, device_ids[0], left, device_ids[1], right)
        print(f"Preflight OK. Proposed XML:\n{xml.decode()}")
        if not args.execute:
            print("DRY RUN: No changes. Use --execute for actual pairing.")
            return
        endpoint = f"{args.oct.rstrip('/')}/streaming/account/{args.account}/group/"
        prepared = (await client.post(endpoint, content=xml, headers={
            "Content-Type": "application/xml"})).raise_for_status()
        group_id = SafeXML.fromstring(prepared.content).get("id")
        if not group_id or not group_id.isdigit():
            raise RuntimeError(f"Server did not return a group ID: {prepared.text[:400]}")
        print(f"Server group created first: {group_id}")
        # Master: no senderIPAddress; slave: master IP, per ST10 logs.
        master_xml = group_xml(args.name, device_ids[0], left, device_ids[1], right, group_id=group_id)
        slave_xml = group_xml(args.name, device_ids[0], left, device_ids[1], right, group_id=group_id, sender=True)

        async def send(ip: str, xml_body: bytes) -> str:
            try:
                result = await client.post(f"http://{ip}:8090/addGroup", content=xml_body,
                                           headers={"Content-Type": "application/xml"}, timeout=45)
                return f"{ip}: HTTP {result.status_code} {result.text[:750]}"
            except httpx.HTTPError as exc:
                return f"{ip}: HTTP client failure: {exc}"

        for value in await asyncio.gather(send(left, master_xml), send(right, slave_xml)):
            print(value)
        await asyncio.sleep(3)
        for ip in (left, right):
            try:
                result = await client.get(f"http://{ip}:8090/getGroup")
                print(f"{ip} /getGroup after: {result.text[:900]}")
            except httpx.HTTPError as exc:
                print(f"{ip} /getGroup failed: {exc}")
        print("No automatic removal. Inspect both speakers before any further test.")


if __name__ == "__main__":
    asyncio.run(main())
