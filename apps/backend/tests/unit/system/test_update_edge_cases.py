import pytest
from fastapi import HTTPException

from opencloudtouch.system import routes
from opencloudtouch.system import update_helper
from opencloudtouch.system import update_service


class Response:
    def __init__(self, data=None, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HealthClient:
    def __init__(self, states):
        self.states = iter(states)

    async def get(self, path):
        return Response({"State": next(self.states)})


@pytest.mark.asyncio
async def test_wait_for_health_accepts_healthy(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(update_helper.asyncio, "sleep", no_sleep)

    client = HealthClient(
        [{"Running": True, "Health": {"Status": "healthy"}}]
    )

    await update_helper.wait_for_health(client, "abc", attempts=1)


@pytest.mark.asyncio
async def test_wait_for_health_rejects_stopped_container(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(update_helper.asyncio, "sleep", no_sleep)

    client = HealthClient([{"Running": False}])

    with pytest.raises(RuntimeError, match="stopped unexpectedly"):
        await update_helper.wait_for_health(client, "abc", attempts=1)


@pytest.mark.asyncio
async def test_wait_for_health_rejects_unhealthy(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(update_helper.asyncio, "sleep", no_sleep)

    client = HealthClient(
        [{"Running": True, "Health": {"Status": "unhealthy"}}]
    )

    with pytest.raises(RuntimeError, match="became unhealthy"):
        await update_helper.wait_for_health(client, "abc", attempts=1)


@pytest.mark.asyncio
async def test_wait_for_health_times_out(monkeypatch):
    async def no_sleep(_):
        pass

    monkeypatch.setattr(update_helper.asyncio, "sleep", no_sleep)

    client = HealthClient(
        [{"Running": True, "Health": {"Status": "starting"}}]
    )

    with pytest.raises(RuntimeError, match="Timed out"):
        await update_helper.wait_for_health(client, "abc", attempts=1)


def test_helper_read_status_handles_invalid_json(tmp_path, monkeypatch):
    path = tmp_path / "update-status.json"
    path.write_text("{broken", encoding="utf-8")

    monkeypatch.setattr(update_helper, "STATUS_PATH", path)

    assert update_helper.read_status() == {}


def test_split_image_reference_rejects_digest():
    with pytest.raises(ValueError, match="Digest"):
        update_service.split_image_reference(
            "ghcr.io/opencloudtouch/opencloudtouch@sha256:abc"
        )


@pytest.mark.asyncio
async def test_launch_helper_supports_bind_mount(monkeypatch):
    created = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            return Response(
                {
                    "Image": "sha256:old",
                    "Mounts": [
                        {
                            "Type": "bind",
                            "Source": "/srv/oct-data",
                            "Destination": "/data",
                        }
                    ],
                }
            )

        async def post(self, path, params=None, json=None):
            if path == "/containers/create":
                created["config"] = json
                return Response({"Id": "helper123"})
            return Response()

    async def fake_client():
        return Client()

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_client,
    )

    result = await update_service.launch_update_helper(
        "old123",
        "ghcr.io/opencloudtouch/opencloudtouch:1.5.9",
    )

    assert result == "helper123"
    assert (
        "/srv/oct-data:/oct-update-data:rw"
        in created["config"]["HostConfig"]["Binds"]
    )


@pytest.mark.asyncio
async def test_launch_helper_requires_data_mount(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            return Response(
                {
                    "Image": "sha256:old",
                    "Mounts": [],
                }
            )

    async def fake_client():
        return Client()

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_client,
    )

    with pytest.raises(RuntimeError, match="/data mount not found"):
        await update_service.launch_update_helper(
            "old123",
            "ghcr.io/opencloudtouch/opencloudtouch:1.5.9",
        )


@pytest.mark.asyncio
async def test_check_update_maps_release_failure_to_503(monkeypatch):
    async def fail():
        raise update_service.UpdateCheckError("GitHub unavailable")

    monkeypatch.setattr(routes, "check_for_update", fail)

    with pytest.raises(HTTPException) as exc:
        await routes.check_update()

    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_update_returns_404_without_container(monkeypatch):
    monkeypatch.setattr(
        routes,
        "get_update_status",
        lambda: {"phase": "idle"},
    )

    async def check():
        return {
            "current": "1.5.8",
            "latest": "1.5.9",
            "update_available": True,
            "release_url": "https://example.test",
        }

    async def no_container():
        return None

    monkeypatch.setattr(routes, "check_for_update", check)
    monkeypatch.setattr(routes, "get_oct_container", no_container)

    with pytest.raises(HTTPException) as exc:
        await routes.update()

    assert exc.value.status_code == 404
