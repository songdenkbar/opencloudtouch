"""Stereo-pair REST endpoints (backend-only implementation)."""

from __future__ import annotations

from fastapi import APIRouter
from opencloudtouch.core.dependencies import StereoServiceDep
from opencloudtouch.core.exceptions import ErrorDetail
from opencloudtouch.stereo.models import (
    CreateStereoPair,
    StereoJob,
    StereoJobList,
    StereoPairList,
)

router = APIRouter(prefix="/api/stereo-pairs", tags=["Stereo"])


@router.get("", response_model=StereoPairList)
async def list_pairs(stereo: StereoServiceDep):
    return await stereo.list_pairs()


@router.post(
    "",
    status_code=202,
    response_model=StereoJob,
    responses={
        404: {"model": ErrorDetail, "description": "device_not_found"},
        409: {
            "model": ErrorDetail,
            "description": (
                "stereo_ssh_not_available, stereo_device_in_zone, "
                "stereo_device_in_pair, stereo_recovery_needed, "
                "stereo_job_in_progress, stereo_job_limit_reached"
            ),
        },
        422: {
            "model": ErrorDetail,
            "description": (
                "stereo_same_device, stereo_name_invalid, "
                "stereo_model_unsupported, stereo_device_invalid"
            ),
        },
        503: {
            "model": ErrorDetail,
            "description": ("stereo_ssh_unreachable, device_unreachable"),
        },
    },
)
async def create_pair(body: CreateStereoPair, stereo: StereoServiceDep):
    return await stereo.create(body.left_device_id, body.right_device_id, body.name)


@router.get("/jobs", response_model=StereoJobList)
async def list_jobs(stereo: StereoServiceDep):
    return stereo.list_jobs()


@router.get(
    "/jobs/{job_id}",
    response_model=StereoJob,
    responses={404: {"model": ErrorDetail, "description": "stereo_job_not_found"}},
)
async def get_job(job_id: str, stereo: StereoServiceDep):
    return stereo.job(job_id)


@router.post(
    "/recover/{device_id}",
    status_code=202,
    response_model=StereoJob,
    responses={
        404: {"model": ErrorDetail, "description": "device_not_found"},
        409: {
            "model": ErrorDetail,
            "description": (
                "stereo_nothing_to_recover, stereo_recovery_needed, "
                "stereo_job_in_progress, stereo_job_limit_reached"
            ),
        },
        503: {
            "model": ErrorDetail,
            "description": ("device_unreachable, stereo_ssh_unreachable"),
        },
    },
)
async def recover(device_id: str, stereo: StereoServiceDep):
    return await stereo.recover(device_id)


@router.delete(
    "/{group_id}",
    status_code=202,
    response_model=StereoJob,
    responses={
        404: {"model": ErrorDetail, "description": "stereo_pair_not_found"},
        409: {
            "model": ErrorDetail,
            "description": (
                "stereo_recovery_needed, stereo_job_in_progress, "
                "stereo_job_limit_reached, stereo_pair_not_found"
            ),
        },
        422: {"model": ErrorDetail, "description": "stereo_group_id_invalid"},
        503: {
            "model": ErrorDetail,
            "description": ("device_unreachable, stereo_ssh_unreachable"),
        },
    },
)
async def remove_pair(group_id: str, stereo: StereoServiceDep, force: bool = False):
    return await stereo.remove(group_id, force)
