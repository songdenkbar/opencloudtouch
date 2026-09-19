import pytest

from opencloudtouch.system import update_service


class FakeResponse:
    def __init__(self, data=None, status_code=200):
        self._data = data
        self.status_code = status_code

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeStream:
    def __init__(self, lines):
        self.lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        pass

    async def aiter_lines(self):
        for line in self.lines:
            yield line


class FakeClient:
    def __init__(self):
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get(self, path):
        self.requests.append(("GET", path))

        if path == "/containers/json":
            return FakeResponse(
                [
                    {
                        "Id": "oct123",
                        "Names": ["/opencloudtouch"],
                    }
                ]
            )

        if path == "/containers/oct123/json":
            return FakeResponse(
                {
                    "Config": {
                        "Image": "ghcr.io/opencloudtouch/opencloudtouch:latest"
                    },
                    "Mounts": [
                        {
                            "Type": "volume",
                            "Name": "oct-data",
                            "Destination": "/data",
                        }
                    ],
                }
            )

        raise AssertionError(f"Unexpected GET {path}")

    async def post(self, path, params=None, json=None):
        self.requests.append(("POST", path, params, json))

        if path == "/containers/create":
            return FakeResponse({"Id": "helper123"})

        if path == "/containers/helper123/start":
            return FakeResponse()

        raise AssertionError(f"Unexpected POST {path}")

    def stream(self, method, path, params=None, timeout=None):
        self.requests.append((method, path, params))
        return FakeStream(
            [
                '{"status":"Pulling"}',
                '{"status":"Downloaded newer image"}',
            ]
        )


@pytest.mark.asyncio
async def test_get_oct_container(monkeypatch):
    client = FakeClient()

    async def fake_get_docker_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_get_docker_client,
    )

    container = await update_service.get_oct_container()

    assert container["Id"] == "oct123"


@pytest.mark.asyncio
async def test_pull_oct_image(monkeypatch):
    client = FakeClient()

    async def fake_get_docker_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_get_docker_client,
    )

    result = await update_service.pull_oct_image(
        "ghcr.io/opencloudtouch/opencloudtouch:latest"
    )

    assert "Downloaded newer image" in result


@pytest.mark.asyncio
async def test_launch_update_helper_mounts_data_volume(monkeypatch):
    client = FakeClient()

    async def fake_get_docker_client():
        return client

    monkeypatch.setattr(
        update_service,
        "get_docker_client",
        fake_get_docker_client,
    )

    helper_id = await update_service.launch_update_helper(
        "oct123",
        "ghcr.io/opencloudtouch/opencloudtouch:latest",
    )

    assert helper_id == "helper123"

    create_request = next(
        request
        for request in client.requests
        if request[0:2] == ("POST", "/containers/create")
    )

    helper_config = create_request[3]
    binds = helper_config["HostConfig"]["Binds"]

    assert "/var/run/docker.sock:/var/run/docker.sock" in binds
    assert "oct-data:/oct-update-data:rw" in binds
    assert (
        "OCT_UPDATE_STATUS_PATH=/oct-update-data/update-status.json"
        in helper_config["Env"]
    )
