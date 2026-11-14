"""HTTP integration tests for the stereo router with an isolated mock backend."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from opencloudtouch.core.exception_handlers import register_exception_handlers
from opencloudtouch.stereo.api.routes import router as stereo_router
from opencloudtouch.stereo.group_backend import mock_group_store
from opencloudtouch.stereo.service import StereoService


LEFT = "DDEEFF445566"
RIGHT = "A1B2C3D4E5F6"


@pytest.fixture
async def stereo_api():
    left = SimpleNamespace(
        device_id=LEFT, ip="192.168.1.101", model="SoundTouch 10", ssh_permanent=False
    )
    right = SimpleNamespace(
        device_id=RIGHT, ip="192.168.1.103", model="SoundTouch 10", ssh_permanent=False
    )
    repo = SimpleNamespace(
        get_all=AsyncMock(return_value=[left, right]),
        get_by_device_id=AsyncMock(
            side_effect=lambda key: {LEFT: left, RIGHT: right}.get(key)
        ),
    )
    zone = SimpleNamespace(get_zone_status=AsyncMock(return_value=None))
    devices = SimpleNamespace(reboot_device=AsyncMock())
    service = StereoService(repo, devices, zone, mock_mode=True)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(stereo_router)
    app.state.stereo_service = service

    mock_group_store.clear()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://stereo-test"
        ) as client:
            yield client, service, devices
    finally:
        await service.shutdown()
        mock_group_store.clear()


@pytest.mark.asyncio
async def test_http_create_read_job_list_remove(stereo_api):
    client, service, devices = stereo_api

    initial = await client.get("/api/stereo-pairs")
    assert initial.status_code == 200
    assert initial.json()["pairs"] == []

    created = await client.post(
        "/api/stereo-pairs",
        json={
            "left_device_id": LEFT,
            "right_device_id": RIGHT,
            "name": "Study & Kitchen",
        },
    )
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert job_id
    assert (await client.get("/api/stereo-pairs/jobs")).status_code == 200

    await service.tasks[job_id]
    job = await client.get(f"/api/stereo-pairs/jobs/{job_id}")
    assert job.status_code == 200
    assert job.json()["state"] == "succeeded"

    pairs = (await client.get("/api/stereo-pairs")).json()["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["left_device_id"] == LEFT
    assert pairs[0]["right_device_id"] == RIGHT
    assert pairs[0]["partial"] is False

    removed = await client.delete(f"/api/stereo-pairs/{pairs[0]['group_id']}")
    assert removed.status_code == 202
    await service.tasks[removed.json()["job_id"]]
    assert (await client.get("/api/stereo-pairs")).json()["pairs"] == []

    # No real network operations: the mock implementation short-circuits reboot.
    devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_validation_and_unknown_job(stereo_api):
    client, _, _ = stereo_api
    invalid = await client.post(
        "/api/stereo-pairs",
        json={
            "left_device_id": LEFT,
            "right_device_id": LEFT,
            "name": "Bad pair",
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["code"] == "stereo_same_device"

    missing = await client.get("/api/stereo-pairs/jobs/nonexistent")
    assert missing.status_code == 404
    assert missing.json()["code"] == "stereo_job_not_found"

    missing_group = await client.delete("/api/stereo-pairs/123456")
    assert missing_group.status_code == 404
    assert missing_group.json()["code"] == "stereo_pair_not_found"
