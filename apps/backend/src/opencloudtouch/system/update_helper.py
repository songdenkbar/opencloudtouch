"""External helper for replacing a Docker container."""

import asyncio
import sys

import httpx

DOCKER_SOCKET = "/var/run/docker.sock"


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
            "Cmd": inspect["Config"]["Cmd"],
            "Entrypoint": inspect["Config"]["Entrypoint"],
            "WorkingDir": inspect["Config"]["WorkingDir"],
            "ExposedPorts": inspect["Config"].get("ExposedPorts"),
            "Healthcheck": inspect["Config"].get("Healthcheck"),
            "Labels": inspect["Config"].get("Labels"),
            "HostConfig": {
                "NetworkMode": inspect["HostConfig"]["NetworkMode"],
                "Binds": inspect["HostConfig"]["Binds"],
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

        print(f"Replaced {name}: {container_id[:12]} -> {new_id[:12]}")


if __name__ == "__main__":
    asyncio.run(main())
