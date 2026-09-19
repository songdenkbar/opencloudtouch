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


async def get_container_inspect(container_id: str):
    async with await get_docker_client() as client:
        response = await client.get(f"/containers/{container_id}/json")
        response.raise_for_status()
        return response.json()


def build_recreate_config(inspect: dict) -> dict:
    return {
        "Image": inspect["Config"]["Image"],
        "Env": inspect["Config"]["Env"],
        "Entrypoint": inspect["Config"]["Entrypoint"],
        "WorkingDir": inspect["Config"]["WorkingDir"],
        "HostConfig": {
            "NetworkMode": inspect["HostConfig"]["NetworkMode"],
            "Binds": inspect["HostConfig"]["Binds"],
            "RestartPolicy": inspect["HostConfig"]["RestartPolicy"],
        },
    }


async def create_container(name: str, config: dict):
    async with await get_docker_client() as client:
        response = await client.post(
            "/containers/create",
            params={"name": name},
            json=config,
        )
        response.raise_for_status()
        return response.json()


async def stop_container(container_id: str):
    async with await get_docker_client() as client:
        response = await client.post(f"/containers/{container_id}/stop")
        response.raise_for_status()


async def remove_container(container_id: str):
    async with await get_docker_client() as client:
        response = await client.delete(f"/containers/{container_id}")
        response.raise_for_status()


async def start_container(container_id: str):
    async with await get_docker_client() as client:
        response = await client.post(f"/containers/{container_id}/start")
        response.raise_for_status()


async def launch_update_helper(target_container_id: str, image: str):
    helper_config = {
        "Image": image,
        "Entrypoint": ["python"],
        "Cmd": [
            "/app/opencloudtouch/system/update_helper.py",
            target_container_id,
        ],
        "HostConfig": {
            "Binds": [
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
            "AutoRemove": True,
        },
    }

    async with await get_docker_client() as client:
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
