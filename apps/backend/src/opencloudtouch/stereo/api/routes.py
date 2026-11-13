"""Stereo-pair REST endpoints (backend-only implementation)."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

router = APIRouter(prefix="/api/stereo-pairs", tags=["Stereo"])


class CreateStereoPair(BaseModel):
    left_device_id: str
    right_device_id: str
    name: str


def service(request: Request):
    return request.app.state.stereo_service


@router.get("")
async def list_pairs(stereo=Depends(service)):
    return await stereo.list_pairs()


@router.post("", status_code=202)
async def create_pair(body: CreateStereoPair, stereo=Depends(service)):
    return await stereo.create(body.left_device_id, body.right_device_id, body.name)


@router.get("/jobs")
async def list_jobs(stereo=Depends(service)):
    return stereo.list_jobs()


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, stereo=Depends(service)):
    return stereo.job(job_id)


@router.post("/recover/{device_id}", status_code=202)
async def recover(device_id: str, stereo=Depends(service)):
    return await stereo.recover(device_id)


@router.delete("/{group_id}", status_code=202)
async def remove_pair(group_id: str, force: bool = False, stereo=Depends(service)):
    return await stereo.remove(group_id, force)
