"""HTTP integration tests for the stereo router with an isolated mock backend."""

from __future__ import annotations

import asyncio

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


async def wait_for_job(client: AsyncClient, job_id: str) -> dict:
    """Poll the public API; finished jobs need not remain in service.tasks."""
    for _ in range(100):
        response = await client.get(f"/api/stereo-pairs/jobs/{job_id}")
        assert response.status_code == 200
        job = response.json()
        if job["state"] != "running":
            return job
        await asyncio.sleep(0.01)
    pytest.fail(f"Stereo job {job_id} did not finish")


@pytest.mark.asyncio
async def test_http_create_read_job_list_remove(stereo_api):
    client, _, devices = stereo_api

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

    job = await wait_for_job(client, job_id)
    assert job["state"] == "succeeded"

    pairs = (await client.get("/api/stereo-pairs")).json()["pairs"]
    assert len(pairs) == 1
    assert pairs[0]["left_device_id"] == LEFT
    assert pairs[0]["right_device_id"] == RIGHT
    assert pairs[0]["partial"] is False

    removed = await client.delete(f"/api/stereo-pairs/{pairs[0]['group_id']}")
    assert removed.status_code == 202
    removal_job = await wait_for_job(client, removed.json()["job_id"])
    assert removal_job["state"] == "succeeded"
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


@pytest.mark.asyncio
@pytest.mark.parametrize("group_id", ["１２３", "١٢٣", "²", "123٤"])
async def test_http_remove_rejects_unicode_digits(stereo_api, group_id):
    """The spec requires ASCII [0-9]{1,12}, not Unicode decimal digits."""
    client, _, _ = stereo_api
    response = await client.delete(f"/api/stereo-pairs/{group_id}")
    assert response.status_code == 422
    assert response.json()["code"] == "stereo_group_id_invalid"


@pytest.mark.asyncio
async def test_http_recover_interrupted_preparation_without_reboot(stereo_api):
    """The public recovery endpoint removes incomplete preparation artifacts."""
    from opencloudtouch.stereo.group_backend import StoredGroup

    client, _, devices = stereo_api
    mock_group_store["192.168.1.101"] = StoredGroup(
        content="<original/>",
        journal={
            "job_id": "a" * 32, "operation": "create",
            "had_file": True, "state": "preparing",
        },
        staged="<unfinished/>",
    )
    accepted = await client.post(f"/api/stereo-pairs/recover/{LEFT}")
    assert accepted.status_code == 202
    result = await wait_for_job(client, accepted.json()["job_id"])
    assert result["operation"] == "recover"
    assert result["state"] == "succeeded"
    assert mock_group_store["192.168.1.101"].content == "<original/>"
    assert mock_group_store["192.168.1.101"].journal is None
    devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_http_recover_orphan_backup_without_journal_fails_safely(stereo_api):
    """Unknown orphaned artifacts cannot be deleted as a successful recovery."""
    from opencloudtouch.stereo.group_backend import StoredGroup

    client, _, devices = stereo_api
    mock_group_store["192.168.1.101"] = StoredGroup(backup="<original/>")
    accepted = await client.post(f"/api/stereo-pairs/recover/{LEFT}")
    assert accepted.status_code == 202
    result = await wait_for_job(client, accepted.json()["job_id"])
    assert result["state"] == "failed"
    assert result["error_code"] == "stereo_nothing_to_recover"
    assert mock_group_store["192.168.1.101"].backup == "<original/>"
    devices.reboot_device.assert_not_awaited()


@pytest.mark.asyncio
async def test_openapi_stereo_responses_have_typed_schemas(stereo_api):
    """Generated OpenAPI must retain all stereo endpoints and response models."""
    client, _, _ = stereo_api
    schema = (await client.get("/openapi.json")).json()
    paths = schema["paths"]
    expected = {
        ("/api/stereo-pairs", "get"): "StereoPairList",
        ("/api/stereo-pairs", "post"): "StereoJob",
        ("/api/stereo-pairs/jobs", "get"): "StereoJobList",
        ("/api/stereo-pairs/jobs/{job_id}", "get"): "StereoJob",
        ("/api/stereo-pairs/recover/{device_id}", "post"): "StereoJob",
        ("/api/stereo-pairs/{group_id}", "delete"): "StereoJob",
    }
    for (path, method), model in expected.items():
        endpoint = paths[path][method]
        status = "202" if method in {"post", "delete"} else "200"
        actual = endpoint["responses"][status]["content"]["application/json"]["schema"]
        assert actual["$ref"] == f"#/components/schemas/{model}"
    assert {"StereoPair", "StereoPairList", "StereoJob", "StereoJobList"} <= set(
        schema["components"]["schemas"]
    )


@pytest.mark.asyncio
async def test_http_overlong_name_keeps_stable_coded_error(stereo_api):
    client, _, _ = stereo_api
    response = await client.post(
        "/api/stereo-pairs",
        json={"left_device_id": LEFT, "right_device_id": RIGHT, "name": "x" * 31},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "stereo_name_invalid"
