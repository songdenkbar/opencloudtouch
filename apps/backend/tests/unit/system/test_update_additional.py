import json
import sys

import httpx
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


class AsyncClient:
    def __init__(self, response=None):
        self.response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, path):
        return self.response


@pytest.mark.asyncio
async def test_get_latest_release_success(monkeypatch):
    client = AsyncClient(
        Response(
            {
                "tag_name": "v1.5.9",
                "html_url": "https://example.test/v1.5.9",
            }
        )
    )

    monkeypatch.setattr(
        update_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client,
    )

    result = await update_service.get_latest_release()

    assert result == {
        "latest": "1.5.9",
        "release_url": "https://example.test/v1.5.9",
    }


@pytest.mark.asyncio
async def test_get_latest_release_rejects_missing_tag(monkeypatch):
    client = AsyncClient(
        Response(
            {
                "html_url": "https://example.test/release",
            }
        )
    )

    monkeypatch.setattr(
        update_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client,
    )

    with pytest.raises(
        update_service.UpdateCheckError,
        match="tag_name",
    ):
        await update_service.get_latest_release()


@pytest.mark.asyncio
async def test_get_latest_release_handles_network_error(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            raise httpx.ConnectError("network down")

    monkeypatch.setattr(
        update_service.httpx,
        "AsyncClient",
        lambda *args, **kwargs: Client(),
    )

    with pytest.raises(
        update_service.UpdateCheckError,
        match="Failed to check",
    ):
        await update_service.get_latest_release()


@pytest.mark.asyncio
async def test_get_oct_container_returns_none(monkeypatch):
    client = AsyncClient(
        Response(
            [
                {
                    "Id": "other123",
                    "Names": ["/something-else"],
                }
            ]
        )
    )

    async def fake_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_client,
    )

    assert await update_service.get_oct_container() is None


@pytest.mark.asyncio
async def test_get_container_inspect(monkeypatch):
    expected = {
        "Id": "abc123",
        "Image": "sha256:test",
    }

    client = AsyncClient(Response(expected))

    async def fake_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_client,
    )

    assert (
        await update_service.get_container_inspect("abc123")
        == expected
    )


@pytest.mark.asyncio
async def test_launch_helper_rejects_unsupported_mount(monkeypatch):
    client = AsyncClient(
        Response(
            {
                "Image": "sha256:old",
                "Mounts": [
                    {
                        "Type": "tmpfs",
                        "Destination": "/data",
                    }
                ],
            }
        )
    )

    async def fake_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_client,
    )

    with pytest.raises(
        RuntimeError,
        match="Unsupported /data mount type",
    ):
        await update_service.launch_update_helper(
            "old123",
            "ghcr.io/opencloudtouch/opencloudtouch:1.5.9",
        )


@pytest.mark.asyncio
async def test_update_docker_failure_returns_503(monkeypatch):
    statuses = []

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

    async def fail():
        raise RuntimeError("Docker unavailable")

    monkeypatch.setattr(routes, "check_for_update", check)
    monkeypatch.setattr(routes, "get_oct_container", fail)

    monkeypatch.setattr(
        routes,
        "set_update_status",
        lambda phase, **kwargs: statuses.append(
            (phase, kwargs)
        ),
    )

    with pytest.raises(HTTPException) as exc:
        await routes.update()

    assert exc.value.status_code == 503
    assert statuses[-1][0] == "failed"
    assert statuses[-1][1]["error"] == "Docker unavailable"


@pytest.mark.asyncio
async def test_remove_container_ignores_missing_container():
    class Client:
        async def get(self, path):
            return Response(status_code=404)

    await update_helper.remove_container(
        Client(),
        "missing123",
    )


@pytest.mark.asyncio
async def test_remove_running_container():
    calls = []

    class Client:
        async def get(self, path):
            return Response(
                {
                    "State": {
                        "Running": True,
                    }
                }
            )

        async def post(self, path, params=None):
            calls.append(("POST", path))
            return Response()

        async def delete(self, path, params=None):
            calls.append(("DELETE", path))
            return Response()

    await update_helper.remove_container(
        Client(),
        "abc123",
    )

    assert (
        "POST",
        "/containers/abc123/stop",
    ) in calls

    assert (
        "DELETE",
        "/containers/abc123",
    ) in calls


@pytest.mark.asyncio
async def test_create_and_start_container():
    calls = []

    class Client:
        async def post(self, path, params=None, json=None):
            calls.append(path)

            if path == "/containers/create":
                return Response({"Id": "new123"})

            return Response()

    result = await update_helper.create_and_start(
        Client(),
        "opencloudtouch",
        {"Image": "example:latest"},
    )

    assert result == "new123"
    assert calls == [
        "/containers/create",
        "/containers/new123/start",
    ]


def helper_inspect():
    return {
        "Name": "/opencloudtouch",
        "Image": "sha256:oldimage",
        "State": {"Running": True},
        "Config": {
            "Image": "example:1.5.8",
            "Env": ["OCT_PORT=7777"],
            "Cmd": None,
            "Entrypoint": ["/entrypoint.sh"],
            "WorkingDir": "/app",
            "ExposedPorts": None,
            "Healthcheck": {
                "Test": [
                    "CMD",
                    "/entrypoint.sh",
                    "health",
                ]
            },
            "Labels": {},
        },
        "HostConfig": {
            "NetworkMode": "host",
            "Binds": ["oct-data:/data:rw"],
            "RestartPolicy": {
                "Name": "unless-stopped",
                "MaximumRetryCount": 0,
            },
            "LogConfig": {},
            "ShmSize": 67108864,
        },
    }


@pytest.mark.asyncio
async def test_failed_rollback_sets_failed_status(
    tmp_path,
    monkeypatch,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            return Response(helper_inspect())

    monkeypatch.setattr(
        update_helper.httpx,
        "AsyncClient",
        lambda *args, **kwargs: Client(),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_helper",
            "old123",
            "example:1.5.9",
        ],
    )

    monkeypatch.setattr(
        update_helper,
        "STATUS_PATH",
        tmp_path / "update-status.json",
    )

    update_helper.STATUS_PATH.write_text(
        json.dumps(
            {
                "phase": "restarting",
                "current_version": "1.5.8",
                "target_version": "1.5.9",
            }
        ),
        encoding="utf-8",
    )

    async def remove(*args, **kwargs):
        pass

    create_count = 0

    async def create(*args, **kwargs):
        nonlocal create_count
        create_count += 1

        if create_count == 1:
            return "broken123"

        raise RuntimeError("rollback create failed")

    async def wait(*args, **kwargs):
        raise RuntimeError(
            "Updated container became unhealthy"
        )

    monkeypatch.setattr(
        update_helper,
        "remove_container",
        remove,
    )
    monkeypatch.setattr(
        update_helper,
        "create_and_start",
        create,
    )
    monkeypatch.setattr(
        update_helper,
        "wait_for_health",
        wait,
    )

    with pytest.raises(
        RuntimeError,
        match="rollback create failed",
    ):
        await update_helper.main()

    status = json.loads(
        update_helper.STATUS_PATH.read_text(
            encoding="utf-8"
        )
    )

    assert status["phase"] == "failed"
    assert (
        status["rollback_error"]
        == "rollback create failed"
    )



@pytest.mark.asyncio
async def test_helper_preparation_failure_sets_failed(
    tmp_path,
    monkeypatch,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            raise RuntimeError("inspect failed")

    monkeypatch.setattr(
        update_helper.httpx,
        "AsyncClient",
        lambda *args, **kwargs: Client(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_helper",
            "old123",
            "example:1.5.9",
        ],
    )
    monkeypatch.setattr(
        update_helper,
        "STATUS_PATH",
        tmp_path / "update-status.json",
    )

    with pytest.raises(
        RuntimeError,
        match="inspect failed",
    ):
        await update_helper.main()

    status = json.loads(
        update_helper.STATUS_PATH.read_text(
            encoding="utf-8"
        )
    )

    assert status["phase"] == "failed"
    assert status["message"] == (
        "Update helper preparation failed"
    )

@pytest.mark.asyncio
async def test_start_failure_rolls_back_by_container_name(
    tmp_path,
    monkeypatch,
):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, path):
            return Response(helper_inspect())

    monkeypatch.setattr(
        update_helper.httpx,
        "AsyncClient",
        lambda *args, **kwargs: Client(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "update_helper",
            "old123",
            "example:1.5.9",
        ],
    )
    monkeypatch.setattr(
        update_helper,
        "STATUS_PATH",
        tmp_path / "update-status.json",
    )

    update_helper.STATUS_PATH.write_text(
        json.dumps(
            {
                "phase": "restarting",
                "current_version": "1.5.8",
                "target_version": "1.5.9",
            }
        ),
        encoding="utf-8",
    )

    removed = []
    create_count = 0

    async def remove(client, container_id):
        removed.append(container_id)

    async def create(client, name, config):
        nonlocal create_count
        create_count += 1

        if create_count == 1:
            raise RuntimeError("container start failed")

        return "rollback123"

    async def wait(client, container_id, attempts=45):
        pass

    monkeypatch.setattr(
        update_helper,
        "remove_container",
        remove,
    )
    monkeypatch.setattr(
        update_helper,
        "create_and_start",
        create,
    )
    monkeypatch.setattr(
        update_helper,
        "wait_for_health",
        wait,
    )

    with pytest.raises(SystemExit):
        await update_helper.main()

    assert removed == [
        "old123",
        "opencloudtouch",
    ]
    assert create_count == 2

    status = json.loads(
        update_helper.STATUS_PATH.read_text(
            encoding="utf-8"
        )
    )

    assert status["phase"] == "rolled_back"
    assert status["current_version"] == "1.5.8"
    assert status["error"] == "container start failed"
