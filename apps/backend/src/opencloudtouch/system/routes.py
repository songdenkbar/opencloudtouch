from fastapi import APIRouter, HTTPException

from opencloudtouch.system.update_service import (
    get_container_inspect,
    get_oct_container,
    launch_update_helper,
    pull_oct_image,
)
from opencloudtouch.system.update_state import get_update_status, set_update_status

router = APIRouter(prefix="/api/system", tags=["System"])


@router.get("/update-status")
async def update_status():
    return get_update_status()


@router.post("/update")
async def update():
    try:
        set_update_status("pulling")

        container = await get_oct_container()
        if container is None:
            raise HTTPException(
                status_code=404,
                detail="OpenCloudTouch container not found",
            )

        inspect = await get_container_inspect(container["Id"])
        image = inspect["Config"]["Image"]

        await pull_oct_image(image)

        set_update_status("restarting", image=image)

        helper_id = await launch_update_helper(
            container["Id"],
            image,
        )

        return {
            "status": "update_started",
            "image": image,
            "helper_id": helper_id,
        }

    except HTTPException:
        set_update_status("failed")
        raise
    except Exception as exc:
        set_update_status("failed", error=str(exc))
        raise HTTPException(status_code=503, detail=str(exc)) from exc
