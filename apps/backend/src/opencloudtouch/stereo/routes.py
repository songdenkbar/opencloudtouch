"""Stereo pair API routes."""

import logging

from fastapi import APIRouter, HTTPException

from opencloudtouch.core.dependencies import StereoServiceDep
from opencloudtouch.stereo.models import (
    CreateStereoPairRequest,
    StereoPairStatus,
    StereoVerifyResult,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stereo-pairs", tags=["Stereo"])


@router.get("", response_model=list[StereoPairStatus])
async def get_stereo_pairs(
    stereo_service: StereoServiceDep,
):
    """Get active stereo pairs."""
    try:
        return await stereo_service.get_all_pairs()
    except Exception as e:
        logger.exception("Failed to get stereo pairs")
        raise HTTPException(
            status_code=500,
            detail="Failed to get stereo pairs",
        ) from e


@router.post("", response_model=StereoPairStatus, status_code=201)
async def create_stereo_pair(
    request: CreateStereoPairRequest,
    stereo_service: StereoServiceDep,
):
    """Create a persistent SoundTouch stereo pair."""
    try:
        return await stereo_service.create_pair(
            request.master_device_id,
            request.slave_device_id,
            request.name,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to create stereo pair")
        raise HTTPException(
            status_code=500,
            detail="Failed to create stereo pair",
        ) from e


@router.delete("/{group_id}", status_code=204)
async def remove_stereo_pair(
    group_id: str,
    stereo_service: StereoServiceDep,
):
    """Remove a persistent SoundTouch stereo pair."""
    try:
        await stereo_service.remove_pair(group_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except Exception as e:
        logger.exception("Failed to remove stereo pair")
        raise HTTPException(
            status_code=500,
            detail="Failed to remove stereo pair",
        ) from e


@router.post("/{group_id}/verify", response_model=StereoVerifyResult)
async def verify_stereo_pair(
    group_id: str,
    stereo_service: StereoServiceDep,
):
    """Verify the current state of a stereo pair."""
    try:
        return await stereo_service.verify_pair(group_id)
    except Exception as e:
        logger.exception("Failed to verify stereo pair")
        raise HTTPException(
            status_code=500,
            detail="Failed to verify stereo pair",
        ) from e
