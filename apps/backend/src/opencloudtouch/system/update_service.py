import httpx

DOCKER_SOCKET = "/var/run/docker.sock"
DOCKER_BASE_URL = "http://docker"


async def get_docker_client():
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET)
    return httpx.AsyncClient(
        transport=transport,
        base_url=DOCKER_BASE_URL,
    )


async def get_oct_container():
    async with await get_docker_client() as client:
        response = await client.get("/containers/json")
        response.raise_for_status()

        for container in response.json():
            if "/opencloudtouch" in container.get("Names", []):
                return container

    return None


async def get_container_inspect(container_id: str):
    async with await get_docker_client() as client:
        response = await client.get(f"/containers/{container_id}/json")
        response.raise_for_status()
        return response.json()


async def pull_oct_image(image: str):
    repository, tag = image.rsplit(":", 1)

    async with await get_docker_client() as client:
        async with client.stream(
            "POST",
            "/images/create",
            params={"fromImage": repository, "tag": tag},
            timeout=None,
        ) as response:
            response.raise_for_status()

            last_status = None
            async for line in response.aiter_lines():
                if line:
                    last_status = line

            return last_status


async def launch_update_helper(target_container_id: str, image: str):
    async with await get_docker_client() as client:
        inspect_response = await client.get(
            f"/containers/{target_container_id}/json"
        )
        inspect_response.raise_for_status()
        inspect = inspect_response.json()

        data_mount = next(
            (
                mount
                for mount in inspect.get("Mounts", [])
                if mount.get("Destination") == "/data"
            ),
            None,
        )

        if data_mount is None:
            raise RuntimeError("OpenCloudTouch /data mount not found")

        if data_mount["Type"] == "volume":
            data_bind = f'{data_mount["Name"]}:/oct-update-data:rw'
        elif data_mount["Type"] == "bind":
            data_bind = f'{data_mount["Source"]}:/oct-update-data:rw'
        else:
            raise RuntimeError(
                f'Unsupported /data mount type: {data_mount["Type"]}'
            )

        helper_config = {
            "Image": image,
            "Entrypoint": ["python"],
            "Cmd": [
                "-m",
                "opencloudtouch.system.update_helper",
                target_container_id,
            ],
            "Env": [
                "OCT_UPDATE_STATUS_PATH=/oct-update-data/update-status.json",
            ],
            "HostConfig": {
                "Binds": [
                    "/var/run/docker.sock:/var/run/docker.sock",
                    data_bind,
                ],
                "AutoRemove": True,
            },
        }

        response = await client.post(
            "/containers/create",
            params={"name": "oct-update-helper"},
            json=helper_config,
        )
        response.raise_for_status()
        helper_id = response.json()["Id"]

        response = await client.post(f"/containers/{helper_id}/start")
        response.raise_for_status()

        return helper_id
