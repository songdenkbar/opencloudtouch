import pytest
from fastapi import HTTPException

from opencloudtouch.system import routes


@pytest.mark.asyncio
async def test_update_status_returns_current_status(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_update_status",
        lambda: {"status": "completed"},
    )

    result = await routes.update_status()

    assert result == {"status": "completed"}


@pytest.mark.asyncio
async def test_update_starts_helper(monkeypatch):
    monkeypatch.setattr(
        routes,
        "set_update_status",
        lambda *args, **kwargs: None,
    )

    async def fake_get_oct_container():
        return {"Id": "oct123"}

    async def fake_get_container_inspect(container_id):
        assert container_id == "oct123"
        return {
            "Config": {
                "Image": "example:latest",
            }
        }

    async def fake_pull_oct_image(image):
        assert image == "example:latest"

    async def fake_launch_update_helper(container_id, image):
        assert container_id == "oct123"
        assert image == "example:latest"
        return "helper123"

    monkeypatch.setattr(
        routes,
        "get_oct_container",
        fake_get_oct_container,
    )
    monkeypatch.setattr(
        routes,
        "get_container_inspect",
        fake_get_container_inspect,
    )
    monkeypatch.setattr(
        routes,
        "pull_oct_image",
        fake_pull_oct_image,
    )
    monkeypatch.setattr(
        routes,
        "launch_update_helper",
        fake_launch_update_helper,
    )

    result = await routes.update()

    assert result == {
        "status": "update_started",
        "image": "example:latest",
        "helper_id": "helper123",
    }


@pytest.mark.asyncio
async def test_update_returns_404_without_oct_container(monkeypatch):
    statuses = []

    def fake_set_update_status(status, **kwargs):
        statuses.append((status, kwargs))

    async def fake_get_oct_container():
        return None

    monkeypatch.setattr(
        routes,
        "set_update_status",
        fake_set_update_status,
    )
    monkeypatch.setattr(
        routes,
        "get_oct_container",
        fake_get_oct_container,
    )

    with pytest.raises(HTTPException) as exc:
        await routes.update()

    assert exc.value.status_code == 404
    assert statuses[-1][0] == "failed"


@pytest.mark.asyncio
async def test_update_returns_503_on_docker_error(monkeypatch):
    statuses = []

    def fake_set_update_status(status, **kwargs):
        statuses.append((status, kwargs))

    async def fake_get_oct_container():
        raise OSError("Docker unavailable")

    monkeypatch.setattr(
        routes,
        "set_update_status",
        fake_set_update_status,
    )
    monkeypatch.setattr(
        routes,
        "get_oct_container",
        fake_get_oct_container,
    )

    with pytest.raises(HTTPException) as exc:
        await routes.update()

    assert exc.value.status_code == 503
    assert "Docker unavailable" in exc.value.detail
    assert statuses[-1] == (
        "failed",
        {"error": "Docker unavailable"},
    )
