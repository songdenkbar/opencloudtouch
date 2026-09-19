import httpx
from fastapi import APIRouter, HTTPException

from opencloudtouch.system.update_service import get_oct_container

router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("/docker-status")
async def docker_status():
    try:
        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
        ) as client:
            response = await client.get("/version")
            response.raise_for_status()
            return response.json()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/update-container")
async def update_container():
    try:
        container = await get_oct_container()
        if container is None:
            raise HTTPException(status_code=404, detail="OpenCloudTouch container not found")
        return container
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/pull-update")
async def pull_update():
    try:
        container = await get_oct_container()
        if container is None:
            raise HTTPException(status_code=404, detail="OpenCloudTouch container not found")

        from opencloudtouch.system.update_service import pull_oct_image

        container_id = container["Id"]

        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
        ) as client:
            inspect = await client.get(f"/containers/{container_id}/json")
            inspect.raise_for_status()
            image = inspect.json()["Config"]["Image"]

        result = await pull_oct_image(image)

        return {"status": "pulled", "image": image, "result": result}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/recreate-config")
async def recreate_config():
    try:
        from opencloudtouch.system.update_service import (
            build_recreate_config,
            get_container_inspect,
        )

        container = await get_oct_container()
        if container is None:
            raise HTTPException(status_code=404, detail="OpenCloudTouch container not found")

        inspect = await get_container_inspect(container["Id"])
        return build_recreate_config(inspect)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/create-recreate-test")
async def create_recreate_test():
    try:
        from opencloudtouch.system.update_service import (
            build_recreate_config,
            create_container,
            get_container_inspect,
        )

        container = await get_oct_container()
        if container is None:
            raise HTTPException(status_code=404, detail="OpenCloudTouch container not found")

        inspect = await get_container_inspect(container["Id"])
        config = build_recreate_config(inspect)
        config["HostConfig"]["RestartPolicy"] = {"Name": "no"}

        return await create_container("oct-recreate-test", config)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/test-container-lifecycle")
async def test_container_lifecycle():
    try:
        from opencloudtouch.system.update_service import (
            create_container,
            remove_container,
            start_container,
            stop_container,
        )

        config = {"Image": "hello-world:latest"}
        created = await create_container("oct-lifecycle-test", config)
        container_id = created["Id"]

        await start_container(container_id)

        # hello-world exits on its own; stop may therefore return 304.
        try:
            await stop_container(container_id)
        except Exception:
            pass

        await remove_container(container_id)

        return {"status": "ok", "container_id": container_id}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/launch-helper-test")
async def launch_helper_test():
    try:
        from opencloudtouch.system.update_service import launch_update_helper

        transport = httpx.AsyncHTTPTransport(uds="/var/run/docker.sock")
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://docker",
        ) as client:
            response = await client.get("/containers/oct-swap-test/json")
            response.raise_for_status()
            target_id = response.json()["Id"]

        helper_id = await launch_update_helper(target_id)
        return {"status": "started", "helper_id": helper_id}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
