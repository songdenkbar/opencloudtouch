"""External helper for replacing the running OCT container."""

import asyncio
import copy
import json
import os
import sys
from pathlib import Path

import httpx


DOCKER_SOCKET = "/var/run/docker.sock"
STATUS_PATH = Path(
    os.getenv(
        "OCT_UPDATE_STATUS_PATH",
        "/oct-update-data/update-status.json",
    )
)


def set_status(status: str, **extra) -> None:
    data = {"status": status, **extra}
    tmp = STATUS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(STATUS_PATH)


async def wait_for_health(
    client: httpx.AsyncClient,
    container_id: str,
    attempts: int = 45,
) -> None:
    for _ in range(attempts):
        await asyncio.sleep(2)

        response = await client.get(
            f"/containers/{container_id}/json"
        )
        response.raise_for_status()

        inspect = response.json()
        state = inspect["State"]

        if not state.get("Running"):
            raise RuntimeError(
                "Updated container stopped unexpectedly"
            )

        health = state.get("Health", {}).get("Status")

        if health == "healthy":
            return

        if health == "unhealthy":
            raise RuntimeError(
                "Updated container became unhealthy"
            )

    raise RuntimeError(
        "Timed out waiting for updated container health"
    )


def build_container_config(inspect: dict) -> dict:
    env = [
        value
        for value in inspect["Config"].get("Env", [])
        if value.startswith("OCT_")
        and not value.startswith("OCT_BUILD_SIGNATURE=")
        and not value.startswith("OCT_VERSION=")
    ]

    return {
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


async def remove_container(
    client: httpx.AsyncClient,
    container_id: str,
) -> None:
    inspect_response = await client.get(
        f"/containers/{container_id}/json"
    )

    if inspect_response.status_code == 404:
        return

    inspect_response.raise_for_status()
    inspect = inspect_response.json()

    if inspect["State"].get("Running"):
        response = await client.post(
            f"/containers/{container_id}/stop",
            params={"t": 10},
        )
        response.raise_for_status()

    response = await client.delete(
        f"/containers/{container_id}",
        params={"force": "true"},
    )
    response.raise_for_status()


async def create_and_start(
    client: httpx.AsyncClient,
    name: str,
    config: dict,
) -> str:
    response = await client.post(
        "/containers/create",
        params={"name": name},
        json=config,
    )
    response.raise_for_status()

    container_id = response.json()["Id"]

    response = await client.post(
        f"/containers/{container_id}/start"
    )
    response.raise_for_status()

    return container_id


async def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(
            "Usage: python -m opencloudtouch.system.update_helper "
            "<container_id>"
        )

    container_id = sys.argv[1]

    transport = httpx.AsyncHTTPTransport(
        uds=DOCKER_SOCKET
    )

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://docker",
        timeout=httpx.Timeout(
            connect=5.0,
            read=120.0,
            write=30.0,
            pool=5.0,
        ),
    ) as client:
        response = await client.get(
            f"/containers/{container_id}/json"
        )
        response.raise_for_status()
        inspect = response.json()

        name = inspect["Name"].lstrip("/")

        # Immutable image ID of the currently running version.
        # This remains usable even after the image tag is updated.
        old_image_id = inspect["Image"]

        new_config = build_container_config(inspect)

        # Keep a complete rollback configuration, but point it
        # explicitly at the immutable old image.
        rollback_config = copy.deepcopy(new_config)
        rollback_config["Image"] = old_image_id

        new_container_id = None

        try:
            await remove_container(client, container_id)

            new_container_id = await create_and_start(
                client,
                name,
                new_config,
            )

            await wait_for_health(
                client,
                new_container_id,
            )

            set_status(
                "completed",
                container_id=new_container_id,
                image=new_config["Image"],
            )

            print(
                f"Replaced {name}: "
                f"{container_id} -> {new_container_id}"
            )

        except Exception as update_error:
            set_status(
                "rolling_back",
                error=str(update_error),
            )

            try:
                if new_container_id is not None:
                    await remove_container(
                        client,
                        new_container_id,
                    )

                rollback_container_id = await create_and_start(
                    client,
                    name,
                    rollback_config,
                )

                await wait_for_health(
                    client,
                    rollback_container_id,
                )

                set_status(
                    "rolled_back",
                    container_id=rollback_container_id,
                    image=old_image_id,
                    error=str(update_error),
                )

                print(
                    f"Rollback completed for {name}: "
                    f"{rollback_container_id}"
                )

            except Exception as rollback_error:
                set_status(
                    "failed",
                    error=str(update_error),
                    rollback_error=str(rollback_error),
                )
                raise

            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
