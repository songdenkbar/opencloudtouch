"""External helper for replacing the running OCT container."""

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

DOCKER_SOCKET = "/var/run/docker.sock"
STATUS_PATH = Path(
    os.getenv("OCT_UPDATE_STATUS_PATH", "/oct-update-data/update-status.json")
)


def set_status(status: str, **extra) -> None:
    data = {"status": status, **extra}
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(STATUS_PATH)


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: update_helper.py <container-id>")

    container_id = sys.argv[1]

    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
    ) as client:
        response = await client.get(f"/containers/{container_id}/json")
        response.raise_for_status()
        inspect = response.json()

        name = inspect["Name"].lstrip("/")

        env = [
            value
            for value in inspect["Config"].get("Env", [])
            if value.startswith("OCT_")
            and not value.startswith("OCT_BUILD_SIGNATURE=")
            and not value.startswith("OCT_VERSION=")
        ]

        config = {
            "Image": inspect["Config"]["Image"],
            "Env": env,
            "Cmd": inspect["Config"].get("Cmd"),
            "Entrypoint": inspect["Config"].get("Entrypoint"),
            "WorkingDir": inspect["Config"].get("WorkingDir"),
            "ExposedPorts": inspect["Config"].get("ExposedPorts"),
            "Healthcheck": inspect["Config"].get("Healthcheck"),
            "Labels": inspect["Config"].get("Labels"),
            "HostConfig": {
                "NetworkMode": inspect["HostConfig"]["NetworkMode"],
                "Binds": inspect["HostConfig"].get("Binds"),
                "RestartPolicy": inspect["HostConfig"]["RestartPolicy"],
                "LogConfig": inspect["HostConfig"].get("LogConfig"),
                "ShmSize": inspect["HostConfig"].get("ShmSize"),
            },
        }

        if inspect["State"]["Running"]:
            response = await client.post(f"/containers/{container_id}/stop")
            response.raise_for_status()

        response = await client.delete(f"/containers/{container_id}")
        response.raise_for_status()

        response = await client.post(
            "/containers/create",
            params={"name": name},
            json=config,
        )
        response.raise_for_status()
        new_id = response.json()["Id"]

        response = await client.post(f"/containers/{new_id}/start")
        response.raise_for_status()

        # Wait until the replacement container becomes healthy.
        for _ in range(45):
            await asyncio.sleep(2)

            response = await client.get(f"/containers/{new_id}/json")
            response.raise_for_status()
            new_inspect = response.json()

            health = new_inspect["State"].get("Health", {}).get("Status")

            if health == "healthy":
                set_status(
                    "completed",
                    container_id=new_id,
                    image=config["Image"],
                )
                print(f"Replaced {name}: {container_id[:12]} -> {new_id[:12]}")
                return

            if health == "unhealthy":
                set_status(
                    "failed",
                    error="Updated container became unhealthy",
                    container_id=new_id,
                )
                raise RuntimeError("Updated container became unhealthy")

        set_status(
            "failed",
            error="Timed out waiting for updated container health",
            container_id=new_id,
        )
        raise RuntimeError("Timed out waiting for updated container health")


if __name__ == "__main__":
    asyncio.run(main())
