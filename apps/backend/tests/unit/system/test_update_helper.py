import json
import sys

import pytest

from opencloudtouch.system import update_helper


class FakeResponse:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        pass


class FakeClient:
    def __init__(self, inspect):
        self.inspect = inspect

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, path):
        assert path == "/containers/old123/json"
        return FakeResponse(self.inspect)


def old_inspect():
    return {
        "Name": "/opencloudtouch",
        "Image": "sha256:oldimage",
        "State": {
            "Running": True,
        },
        "Config": {
            "Image": "example:latest",
            "Env": [
                "OCT_PORT=7777",
                "OCT_VERSION=1.5.8",
                "OCT_BUILD_SIGNATURE=test",
                "PATH=/usr/local/bin",
            ],
            "Cmd": None,
            "Entrypoint": ["/entrypoint.sh"],
            "WorkingDir": "/app",
            "ExposedPorts": None,
            "Healthcheck": {
                "Test": ["CMD", "/entrypoint.sh", "health"],
            },
            "Labels": {},
        },
        "HostConfig": {
            "NetworkMode": "host",
            "Binds": [
                "oct-data:/data:rw",
                "/var/run/docker.sock:/var/run/docker.sock:rw",
            ],
            "RestartPolicy": {
                "Name": "unless-stopped",
                "MaximumRetryCount": 0,
            },
            "LogConfig": {},
            "ShmSize": 67108864,
        },
    }


def test_build_container_config_filters_build_metadata():
    config = update_helper.build_container_config(old_inspect())

    assert config["Image"] == "example:latest"
    assert "OCT_PORT=7777" in config["Env"]

    assert not any(
        value.startswith("OCT_VERSION=")
        for value in config["Env"]
    )
    assert not any(
        value.startswith("OCT_BUILD_SIGNATURE=")
        for value in config["Env"]
    )


@pytest.mark.asyncio
async def test_successful_update_sets_completed(
    tmp_path,
    monkeypatch,
):
    inspect = old_inspect()
    client = FakeClient(inspect)

    monkeypatch.setattr(
        update_helper.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["update_helper", "old123"],
    )

    monkeypatch.setattr(
        update_helper,
        "STATUS_PATH",
        tmp_path / "update-status.json",
    )

    removed = []
    created = []

    async def fake_remove(client, container_id):
        removed.append(container_id)

    async def fake_create(client, name, config):
        created.append((name, config))
        return "new123"

    async def fake_wait(client, container_id, attempts=45):
        assert container_id == "new123"

    monkeypatch.setattr(
        update_helper,
        "remove_container",
        fake_remove,
    )
    monkeypatch.setattr(
        update_helper,
        "create_and_start",
        fake_create,
    )
    monkeypatch.setattr(
        update_helper,
        "wait_for_health",
        fake_wait,
    )

    await update_helper.main()

    assert removed == ["old123"]
    assert created[0][1]["Image"] == "example:latest"

    status = json.loads(
        update_helper.STATUS_PATH.read_text()
    )

    assert status["status"] == "completed"
    assert status["container_id"] == "new123"


@pytest.mark.asyncio
async def test_failed_update_rolls_back_to_old_image(
    tmp_path,
    monkeypatch,
):
    inspect = old_inspect()
    client = FakeClient(inspect)

    monkeypatch.setattr(
        update_helper.httpx,
        "AsyncClient",
        lambda *args, **kwargs: client,
    )

    monkeypatch.setattr(
        sys,
        "argv",
        ["update_helper", "old123"],
    )

    monkeypatch.setattr(
        update_helper,
        "STATUS_PATH",
        tmp_path / "update-status.json",
    )

    removed = []
    created = []

    async def fake_remove(client, container_id):
        removed.append(container_id)

    async def fake_create(client, name, config):
        created.append((name, config))

        if len(created) == 1:
            return "broken123"

        return "rollback123"

    async def fake_wait(client, container_id, attempts=45):
        if container_id == "broken123":
            raise RuntimeError(
                "Updated container became unhealthy"
            )

    monkeypatch.setattr(
        update_helper,
        "remove_container",
        fake_remove,
    )
    monkeypatch.setattr(
        update_helper,
        "create_and_start",
        fake_create,
    )
    monkeypatch.setattr(
        update_helper,
        "wait_for_health",
        fake_wait,
    )

    with pytest.raises(SystemExit) as exc:
        await update_helper.main()

    assert exc.value.code == 1

    assert removed == [
        "old123",
        "broken123",
    ]

    assert created[0][1]["Image"] == "example:latest"
    assert created[1][1]["Image"] == "sha256:oldimage"

    status = json.loads(
        update_helper.STATUS_PATH.read_text()
    )

    assert status["status"] == "rolled_back"
    assert status["container_id"] == "rollback123"
    assert status["image"] == "sha256:oldimage"
    assert (
        status["error"]
        == "Updated container became unhealthy"
    )
