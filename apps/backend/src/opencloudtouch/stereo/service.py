"""Service for managing SoundTouch stereo pairs."""

import asyncio
import base64
import logging
import secrets
from xml.sax.saxutils import escape
from typing import TYPE_CHECKING, Callable

from opencloudtouch.core.exceptions import DeviceNotFoundError
from opencloudtouch.devices.repository import DeviceRepository
from opencloudtouch.discovery import SOUNDTOUCH_HTTP_PORT
from opencloudtouch.stereo.models import StereoPairStatus, StereoVerifyResult

if TYPE_CHECKING:
    from opencloudtouch.devices.client import DeviceClient

logger = logging.getLogger(__name__)

DeviceClientFactory = Callable[[str], "DeviceClient"]

STEREO_SUPPORTED_MODEL = "SoundTouch 10"
GROUP_SERVICE_PATH = "/mnt/nv/BoseApp-Persistence/1/GroupService.xml"


class StereoService:
    """Service for managing SoundTouch stereo pairs."""

    def __init__(
        self,
        device_repo: DeviceRepository,
        client_factory: DeviceClientFactory | None = None,
    ) -> None:
        self.device_repo = device_repo
        self._client_factory = client_factory

    def _get_client(self, ip: str) -> "DeviceClient":
        """Get a device client for the given IP."""
        if self._client_factory is None:
            from opencloudtouch.devices.adapter import get_device_client

            return get_device_client(f"http://{ip}:{SOUNDTOUCH_HTTP_PORT}")  # NOSONAR

        return self._client_factory(f"http://{ip}:{SOUNDTOUCH_HTTP_PORT}")  # NOSONAR

    async def _get_device_or_raise(self, device_id: str):
        """Get device from repository or raise DeviceNotFoundError."""
        device = await self.device_repo.get_by_device_id(device_id)
        if not device:
            raise DeviceNotFoundError(device_id)
        return device

    async def validate_pair(
        self,
        master_device_id: str,
        slave_device_id: str,
    ) -> tuple:
        """Validate devices selected for a stereo pair."""
        if master_device_id == slave_device_id:
            raise ValueError("Master and slave must be different devices")

        master = await self._get_device_or_raise(master_device_id)
        slave = await self._get_device_or_raise(slave_device_id)

        if master.model != STEREO_SUPPORTED_MODEL:
            raise ValueError(
                f"Device {master_device_id} is not a {STEREO_SUPPORTED_MODEL}"
            )

        if slave.model != STEREO_SUPPORTED_MODEL:
            raise ValueError(
                f"Device {slave_device_id} is not a {STEREO_SUPPORTED_MODEL}"
            )

        master_client = self._get_client(master.ip)
        slave_client = self._get_client(slave.ip)

        master_zone = await master_client.get_zone_status()
        if master_zone is not None:
            raise ValueError(
                f"Device {master_device_id} is currently part of a multi-room zone"
            )

        slave_zone = await slave_client.get_zone_status()
        if slave_zone is not None:
            raise ValueError(
                f"Device {slave_device_id} is currently part of a multi-room zone"
            )

        master_group = await master_client.get_group_status()
        if master_group is not None:
            raise ValueError(f"Device {master_device_id} is currently part of a group")

        slave_group = await slave_client.get_group_status()
        if slave_group is not None:
            raise ValueError(f"Device {slave_device_id} is currently part of a group")

        return master, slave

    @staticmethod
    def _build_group_xml(
        group_id: str,
        name: str,
        master_device_id: str,
        master_ip: str,
        slave_device_id: str,
        slave_ip: str,
        include_status: bool,
    ) -> str:
        """Build persistent SoundTouch group XML."""
        safe_name = escape(name)
        status = "  <status>GROUP_OK</status>\n" if include_status else ""

        return (
            '<?xml version="1.0" encoding="UTF-8" ?>\n'
            f'<group id="{group_id}">\n'
            f"  <name>{safe_name}</name>\n"
            f"  <masterDeviceId>{master_device_id}</masterDeviceId>\n"
            "  <roles>\n"
            "    <groupRole>"
            f"<deviceId>{master_device_id}</deviceId>"
            "<role>LEFT</role>"
            f"<ipAddress>{master_ip}</ipAddress>"
            "</groupRole>\n"
            "    <groupRole>"
            f"<deviceId>{slave_device_id}</deviceId>"
            "<role>RIGHT</role>"
            f"<ipAddress>{slave_ip}</ipAddress>"
            "</groupRole>\n"
            "  </roles>\n"
            f"  <senderIPAddress>{master_ip}</senderIPAddress>\n"
            f"{status}"
            "</group>\n"
        )

    async def _write_group_file(self, ip: str, content: str) -> bool:
        """Back up and atomically write GroupService.xml via SSH."""
        from opencloudtouch.setup.ssh_client import SoundTouchSSHClient

        ssh = SoundTouchSSHClient(ip)
        connection = await ssh.connect()

        if not connection.success:
            raise RuntimeError(
                f"SSH connection to {ip} failed: {connection.error or 'unknown error'}"
            )

        try:
            mkdir = await ssh.execute("mkdir -p /mnt/nv/BoseApp-Persistence/1")
            if not mkdir.success:
                raise RuntimeError(f"Failed to prepare persistence directory on {ip}")

            exists = await ssh.execute(
                f"test -f {GROUP_SERVICE_PATH} && echo exists || echo missing"
            )
            had_existing = "exists" in (exists.output or "")

            if had_existing:
                backup = await ssh.execute(
                    f"cp {GROUP_SERVICE_PATH} {GROUP_SERVICE_PATH}.oct-backup"
                )
                if not backup.success:
                    raise RuntimeError(f"Failed to back up GroupService.xml on {ip}")

            encoded = base64.b64encode(content.encode()).decode()
            write = await ssh.execute(
                f"echo '{encoded}' | base64 -d > /tmp/groupservice.new "
                f"&& mv /tmp/groupservice.new {GROUP_SERVICE_PATH}"
            )
            if not write.success:
                raise RuntimeError(
                    f"Failed to write GroupService.xml on {ip}: "
                    f"{write.error or write.output}"
                )

            return had_existing
        finally:
            await ssh.close()

    async def create_pair(
        self,
        master_device_id: str,
        slave_device_id: str,
        name: str | None = None,
    ) -> StereoPairStatus:
        """Create and verify a persistent SoundTouch 10 stereo pair."""
        master, slave = await self.validate_pair(
            master_device_id,
            slave_device_id,
        )

        group_id = str(secrets.randbelow(9_000_000) + 1_000_000)
        pair_name = name or f"{master.name} Stereo"

        master_xml = self._build_group_xml(
            group_id=group_id,
            name=pair_name,
            master_device_id=master.device_id,
            master_ip=master.ip,
            slave_device_id=slave.device_id,
            slave_ip=slave.ip,
            include_status=False,
        )

        slave_xml = self._build_group_xml(
            group_id=group_id,
            name=pair_name,
            master_device_id=master.device_id,
            master_ip=master.ip,
            slave_device_id=slave.device_id,
            slave_ip=slave.ip,
            include_status=True,
        )

        logger.info(
            "Creating stereo pair group=%s master=%s slave=%s",
            group_id,
            master.device_id,
            slave.device_id,
        )

        master_had_existing = False
        slave_had_existing = False
        master_written = False
        slave_written = False

        try:
            master_had_existing = await self._write_group_file(
                master.ip,
                master_xml,
            )
            master_written = True

            slave_had_existing = await self._write_group_file(
                slave.ip,
                slave_xml,
            )
            slave_written = True
        except Exception:
            logger.exception(
                "Stereo pair write failed; restoring previous device state"
            )

            if slave_written:
                try:
                    await self._restore_group_file(
                        slave.ip,
                        slave_had_existing,
                    )
                except Exception:
                    logger.exception(
                        "Failed to roll back slave %s",
                        slave.device_id,
                    )

            if master_written:
                try:
                    await self._restore_group_file(
                        master.ip,
                        master_had_existing,
                    )
                except Exception:
                    logger.exception(
                        "Failed to roll back master %s",
                        master.device_id,
                    )

            raise

        master_client = self._get_client(master.ip)
        slave_client = self._get_client(slave.ip)

        await asyncio.gather(
            master_client.reboot(),
            slave_client.reboot(),
        )

        # SoundTouch devices take about one minute to complete a reboot.
        await asyncio.sleep(60)

        verification = await self.verify_pair(group_id)
        if not verification.verified:
            raise RuntimeError(
                f"Stereo pair verification failed: {verification.reason}"
            )

        return StereoPairStatus(
            group_id=group_id,
            name=pair_name,
            master_device_id=master.device_id,
            slave_device_id=slave.device_id,
            master_ip=master.ip,
            slave_ip=slave.ip,
            status="GROUP_OK",
        )

    async def get_all_pairs(self) -> list[StereoPairStatus]:
        """Get active stereo pairs directly from SoundTouch devices."""
        devices = await self.device_repo.get_all()
        devices_by_id = {device.device_id: device for device in devices}
        pairs: dict[str, StereoPairStatus] = {}

        for device in devices:
            if device.model != STEREO_SUPPORTED_MODEL:
                continue

            client = self._get_client(device.ip)
            group = await client.get_group_status()

            if group is None or not group.group_id:
                continue

            if group.group_id in pairs:
                continue

            left_role = next(
                (role for role in group.roles if role.role.upper() == "LEFT"),
                None,
            )
            right_role = next(
                (role for role in group.roles if role.role.upper() == "RIGHT"),
                None,
            )

            if left_role is None or right_role is None:
                logger.debug(
                    "Ignoring non-stereo group %s on device %s",
                    group.group_id,
                    device.device_id,
                )
                continue

            master = devices_by_id.get(left_role.device_id)
            slave = devices_by_id.get(right_role.device_id)

            pairs[group.group_id] = StereoPairStatus(
                group_id=group.group_id,
                name=group.name,
                master_device_id=left_role.device_id,
                slave_device_id=right_role.device_id,
                master_ip=master.ip if master else left_role.ip_address,
                slave_ip=slave.ip if slave else right_role.ip_address,
                status=group.status,
            )

        return list(pairs.values())

    async def _remove_group_file(self, ip: str) -> None:
        """Back up and remove GroupService.xml via SSH."""
        from opencloudtouch.setup.ssh_client import SoundTouchSSHClient

        ssh = SoundTouchSSHClient(ip)
        connection = await ssh.connect()

        if not connection.success:
            raise RuntimeError(
                f"SSH connection to {ip} failed: {connection.error or 'unknown error'}"
            )

        try:
            exists = await ssh.execute(
                f"test -f {GROUP_SERVICE_PATH} && echo exists || echo missing"
            )
            had_existing = "exists" in (exists.output or "")

            if not had_existing:
                return

            backup = await ssh.execute(
                f"cp {GROUP_SERVICE_PATH} {GROUP_SERVICE_PATH}.oct-backup"
            )
            if not backup.success:
                raise RuntimeError(f"Failed to back up GroupService.xml on {ip}")

            remove = await ssh.execute(f"rm -f {GROUP_SERVICE_PATH}")
            if not remove.success:
                raise RuntimeError(
                    f"Failed to remove GroupService.xml on {ip}: "
                    f"{remove.error or remove.output}"
                )
        finally:
            await ssh.close()

    async def remove_pair(self, group_id: str) -> None:
        """Remove a persistent stereo pair."""
        pairs = await self.get_all_pairs()
        pair = next((item for item in pairs if item.group_id == group_id), None)

        if pair is None:
            raise ValueError(f"Stereo pair {group_id} was not found")

        master = await self._get_device_or_raise(pair.master_device_id)
        slave = await self._get_device_or_raise(pair.slave_device_id)

        logger.info(
            "Removing stereo pair group=%s master=%s slave=%s",
            group_id,
            master.device_id,
            slave.device_id,
        )

        await self._remove_group_file(master.ip)
        await self._remove_group_file(slave.ip)

        master_client = self._get_client(master.ip)
        slave_client = self._get_client(slave.ip)

        await asyncio.gather(
            master_client.reboot(),
            slave_client.reboot(),
        )

        await asyncio.sleep(60)

        master_group = await master_client.get_group_status()
        slave_group = await slave_client.get_group_status()

        if master_group is not None or slave_group is not None:
            raise RuntimeError("Stereo pair is still active after reboot")

    async def _restore_group_file(self, ip: str, had_existing: bool) -> None:
        """Restore GroupService.xml to its state before a stereo change."""
        from opencloudtouch.setup.ssh_client import SoundTouchSSHClient

        ssh = SoundTouchSSHClient(ip)
        connection = await ssh.connect()

        if not connection.success:
            raise RuntimeError(
                f"SSH connection to {ip} failed during rollback: "
                f"{connection.error or 'unknown error'}"
            )

        try:
            if had_existing:
                restore = await ssh.execute(
                    f"cp {GROUP_SERVICE_PATH}.oct-backup {GROUP_SERVICE_PATH}"
                )
                if not restore.success:
                    raise RuntimeError(f"Failed to restore GroupService.xml on {ip}")
            else:
                remove = await ssh.execute(f"rm -f {GROUP_SERVICE_PATH}")
                if not remove.success:
                    raise RuntimeError(
                        f"Failed to remove GroupService.xml during rollback on {ip}"
                    )
        finally:
            await ssh.close()

    async def verify_pair(self, group_id: str) -> StereoVerifyResult:
        """Verify a stereo pair against the current device group state."""
        pairs = await self.get_all_pairs()
        pair = next((item for item in pairs if item.group_id == group_id), None)

        if pair is None:
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                reason="group_not_found",
            )

        master = await self._get_device_or_raise(pair.master_device_id)
        slave = await self._get_device_or_raise(pair.slave_device_id)

        master_client = self._get_client(master.ip)
        slave_client = self._get_client(slave.ip)

        master_group, slave_group = await asyncio.gather(
            master_client.get_group_status(),
            slave_client.get_group_status(),
        )

        if master_group is None:
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                reason="master_group_missing",
            )

        if slave_group is None:
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                reason="slave_group_missing",
            )

        if master_group.group_id != group_id or slave_group.group_id != group_id:
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                reason="group_mismatch",
            )

        expected_roles = {
            (pair.master_device_id, "LEFT"),
            (pair.slave_device_id, "RIGHT"),
        }

        master_roles = {
            (role.device_id, role.role.upper()) for role in master_group.roles
        }
        slave_roles = {
            (role.device_id, role.role.upper()) for role in slave_group.roles
        }

        if not expected_roles.issubset(master_roles) or not expected_roles.issubset(
            slave_roles
        ):
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                reason="roles_mismatch",
            )

        if master_group.status != "GROUP_OK" or slave_group.status != "GROUP_OK":
            return StereoVerifyResult(
                group_id=group_id,
                verified=False,
                status=master_group.status or slave_group.status,
                reason="status_not_ok",
            )

        return StereoVerifyResult(
            group_id=group_id,
            verified=True,
            status="GROUP_OK",
        )
