"""Unit tests for the experimental ICY stream proxy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from opencloudtouch.streaming import icy_proxy
from opencloudtouch.streaming.icy_proxy import (
    ProxyMetadata,
    _deinterleave_icy,
    _end_proxy_session,
    _open_upstream,
    _passthrough,
    _set_proxy_metadata,
    _start_proxy_session,
    get_all_proxy_metadata,
    get_proxy_metadata,
)


class _RawResponse:
    """Minimal async raw-stream response used by proxy tests."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def aiter_raw(self):
        for chunk in self._chunks:
            yield chunk


@pytest.fixture(autouse=True)
def clear_proxy_state():
    icy_proxy._metadata_by_device.clear()
    icy_proxy._active_session_by_device.clear()
    yield
    icy_proxy._metadata_by_device.clear()
    icy_proxy._active_session_by_device.clear()


def _icy_block(title: str) -> bytes:
    raw = f"StreamTitle='{title}';".encode("utf-8")
    padded_len = ((len(raw) + 15) // 16) * 16
    return bytes([padded_len // 16]) + raw.ljust(padded_len, b"\x00")


@pytest.mark.asyncio
async def test_deinterleave_strips_metadata_and_preserves_audio():
    metaint = 8
    audio_a = b"ABCDEFGH"
    audio_b = b"IJKLMNOP"
    stream = audio_a + _icy_block("Nova Finch - Paper Skies") + audio_b + b"\x00"

    response = _RawResponse([stream])
    session = _start_proxy_session("192.0.2.10", "Test Radio")

    chunks = [
        chunk
        async for chunk in _deinterleave_icy(
            response,
            metaint,
            device_ip="192.0.2.10",
            station_name="Test Radio",
            session=session,
        )
    ]

    assert b"".join(chunks) == audio_a + audio_b
    metadata = get_proxy_metadata("192.0.2.10")
    assert metadata is not None
    assert metadata.artist == "Nova Finch"
    assert metadata.track == "Paper Skies"
    assert metadata.raw_title == "Nova Finch - Paper Skies"


@pytest.mark.asyncio
async def test_deinterleave_handles_metadata_split_across_chunks():
    metaint = 4
    block = _icy_block("Velvet North - Quiet Lantern")
    stream = b"ABCD" + block + b"EFGH" + b"\x00"

    response = _RawResponse(
        [
            stream[:7],
            stream[7:19],
            stream[19:],
        ]
    )
    session = _start_proxy_session("192.0.2.11", "Split Radio")

    chunks = [
        chunk
        async for chunk in _deinterleave_icy(
            response,
            metaint,
            device_ip="192.0.2.11",
            station_name="Split Radio",
            session=session,
        )
    ]

    assert b"".join(chunks) == b"ABCDEFGH"
    metadata = get_proxy_metadata("192.0.2.11")
    assert metadata is not None
    assert metadata.artist == "Velvet North"
    assert metadata.track == "Quiet Lantern"


@pytest.mark.asyncio
async def test_deinterleave_falls_back_to_latin1():
    metaint = 4
    title = "Märchenwald - Übermorgen"
    raw = f"StreamTitle='{title}';".encode("latin-1")
    padded_len = ((len(raw) + 15) // 16) * 16
    block = bytes([padded_len // 16]) + raw.ljust(padded_len, b"\x00")
    response = _RawResponse([b"ABCD" + block])
    session = _start_proxy_session("192.0.2.12", "Latin Radio")

    chunks = [
        chunk
        async for chunk in _deinterleave_icy(
            response,
            metaint,
            device_ip="192.0.2.12",
            station_name="Latin Radio",
            session=session,
        )
    ]

    assert b"".join(chunks) == b"ABCD"
    metadata = get_proxy_metadata("192.0.2.12")
    assert metadata is not None
    assert metadata.artist == "Märchenwald"
    assert metadata.track == "Übermorgen"


@pytest.mark.asyncio
async def test_passthrough_yields_raw_audio_unchanged():
    chunks = [b"abc", b"def", b"ghi"]
    response = _RawResponse(chunks)

    result = [chunk async for chunk in _passthrough(response)]

    assert result == chunks


def test_proxy_metadata_is_separate_per_device():
    session_a = _start_proxy_session("192.0.2.20", "Moon Radio")
    session_b = _start_proxy_session("192.0.2.21", "River Radio")

    _set_proxy_metadata(
        "192.0.2.20",
        ProxyMetadata(
            station_name="Moon Radio",
            artist="Copper Avenue",
            track="Blue Window",
            raw_title="Copper Avenue - Blue Window",
        ),
        session_a,
    )
    _set_proxy_metadata(
        "192.0.2.21",
        ProxyMetadata(
            station_name="River Radio",
            artist="Lumen Parade",
            track="Soft Current",
            raw_title="Lumen Parade - Soft Current",
        ),
        session_b,
    )

    snapshot = get_all_proxy_metadata()
    assert snapshot["192.0.2.20"].track == "Blue Window"
    assert snapshot["192.0.2.21"].track == "Soft Current"


def test_current_session_removes_cache_when_stream_ends():
    session = _start_proxy_session("192.0.2.30", "Evening Radio")
    assert get_proxy_metadata("192.0.2.30") is not None

    _end_proxy_session("192.0.2.30", session)

    assert get_proxy_metadata("192.0.2.30") is None


def test_old_session_cannot_delete_newer_session_cache():
    old_session = _start_proxy_session("192.0.2.40", "Old Radio")
    new_session = _start_proxy_session("192.0.2.40", "New Radio")

    _set_proxy_metadata(
        "192.0.2.40",
        ProxyMetadata(
            station_name="New Radio",
            artist="Solar Thread",
            track="Afterglow Avenue",
            raw_title="Solar Thread - Afterglow Avenue",
        ),
        new_session,
    )

    _end_proxy_session("192.0.2.40", old_session)

    metadata = get_proxy_metadata("192.0.2.40")
    assert metadata is not None
    assert metadata.station_name == "New Radio"
    assert metadata.track == "Afterglow Avenue"


def test_old_session_cannot_overwrite_newer_session_metadata():
    old_session = _start_proxy_session("192.0.2.50", "Old Radio")
    new_session = _start_proxy_session("192.0.2.50", "New Radio")

    _set_proxy_metadata(
        "192.0.2.50",
        ProxyMetadata(
            station_name="New Radio",
            artist="Amber Relay",
            track="Glass Horizon",
            raw_title="Amber Relay - Glass Horizon",
        ),
        new_session,
    )
    _set_proxy_metadata(
        "192.0.2.50",
        ProxyMetadata(
            station_name="Old Radio",
            artist="Stale Artist",
            track="Stale Track",
            raw_title="Stale Artist - Stale Track",
        ),
        old_session,
    )

    metadata = get_proxy_metadata("192.0.2.50")
    assert metadata is not None
    assert metadata.station_name == "New Radio"
    assert metadata.artist == "Amber Relay"
    assert metadata.track == "Glass Horizon"


def test_proxy_metadata_snapshot_is_a_copy():
    session = _start_proxy_session("192.0.2.60", "Snapshot Radio")
    _set_proxy_metadata(
        "192.0.2.60",
        ProxyMetadata(
            station_name="Snapshot Radio",
            artist="Ivory Field",
            track="Northbound",
            raw_title="Ivory Field - Northbound",
        ),
        session,
    )

    snapshot = get_all_proxy_metadata()
    snapshot.clear()

    assert get_proxy_metadata("192.0.2.60") is not None


class _FakeReader:
    def __init__(self, request: bytes) -> None:
        self._request = request

    async def readuntil(self, separator: bytes) -> bytes:
        assert separator == b"\r\n\r\n"
        return self._request


class _FakeWriter:
    def __init__(self, peer=("192.0.2.70", 50000)) -> None:
        self.peer = peer
        self.data = bytearray()
        self.closed = False

    def get_extra_info(self, name: str):
        if name == "peername":
            return self.peer
        return None

    def write(self, data: bytes) -> None:
        self.data.extend(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


@pytest.mark.asyncio
async def test_handle_client_rejects_non_get_method():
    reader = _FakeReader(b"POST /stream HTTP/1.1\r\nHost: test\r\n\r\n")
    writer = _FakeWriter()

    await icy_proxy._handle_client(reader, writer)

    assert b"405 Method Not Allowed" in writer.data
    assert writer.closed is True


@pytest.mark.asyncio
async def test_handle_client_rejects_unknown_path():
    reader = _FakeReader(b"GET /wrong HTTP/1.1\r\nHost: test\r\n\r\n")
    writer = _FakeWriter()

    await icy_proxy._handle_client(reader, writer)

    assert b"404 Not Found" in writer.data
    assert writer.closed is True


@pytest.mark.asyncio
async def test_handle_client_rejects_missing_stream_url():
    reader = _FakeReader(b"GET /stream?name=Test+Radio HTTP/1.1\r\nHost: test\r\n\r\n")
    writer = _FakeWriter()

    await icy_proxy._handle_client(reader, writer)

    assert b"400 Bad Request" in writer.data
    assert writer.closed is True


@pytest.mark.asyncio
async def test_handle_client_cleans_cache_when_upstream_open_fails():
    reader = _FakeReader(
        b"GET /stream?url=http%3A%2F%2Fexample.test%2Fradio&name=Test+Radio "
        b"HTTP/1.1\r\nHost: test\r\n\r\n"
    )
    writer = _FakeWriter(peer=("192.0.2.71", 50001))

    with patch(
        "opencloudtouch.streaming.icy_proxy._open_upstream",
        new=AsyncMock(side_effect=RuntimeError("upstream failed")),
    ):
        await icy_proxy._handle_client(reader, writer)

    assert get_proxy_metadata("192.0.2.71") is None
    assert writer.closed is True


@pytest.mark.asyncio
async def test_handle_client_passthrough_without_icy_metaint():
    reader = _FakeReader(
        b"GET /stream?url=http%3A%2F%2Fexample.test%2Fradio&name=Quiet+Radio "
        b"HTTP/1.1\r\nHost: test\r\n\r\n"
    )
    writer = _FakeWriter(peer=("192.0.2.72", 50002))

    upstream = MagicMock(spec=httpx.Response)
    upstream.headers = httpx.Headers({"content-type": "audio/aac", "icy-name": "quiet"})
    upstream.aclose = AsyncMock()

    async def aiter_raw():
        yield b"audio-one"
        yield b"audio-two"

    upstream.aiter_raw = aiter_raw
    client = AsyncMock(spec=httpx.AsyncClient)
    client.aclose = AsyncMock()

    with patch(
        "opencloudtouch.streaming.icy_proxy._open_upstream",
        new=AsyncMock(return_value=(client, upstream, 0)),
    ):
        await icy_proxy._handle_client(reader, writer)

    body = bytes(writer.data).split(b"\r\n\r\n", 1)[1]
    assert body == b"audio-oneaudio-two"
    assert b"Content-Type: audio/aac" in writer.data
    assert b"icy-name: quiet" in writer.data
    assert get_proxy_metadata("192.0.2.72") is None
    upstream.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_upstream_requests_icy_metadata_and_reads_metaint():
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = httpx.Headers({"icy-metaint": "16384"})
    response.aclose = AsyncMock()

    client = MagicMock(spec=httpx.AsyncClient)
    request = httpx.Request("GET", "http://radio.example.test/stream")
    client.build_request.return_value = request
    client.send = AsyncMock(return_value=response)
    client.aclose = AsyncMock()

    with patch(
        "opencloudtouch.streaming.icy_proxy.httpx.AsyncClient",
        return_value=client,
    ) as client_cls:
        returned_client, returned_response, metaint = await _open_upstream(
            "http://radio.example.test/stream"
        )

    assert returned_client is client
    assert returned_response is response
    assert metaint == 16384
    headers = client_cls.call_args.kwargs["headers"]
    assert headers["Icy-MetaData"] == "1"
    assert headers["Accept-Encoding"] == "identity"
    client.send.assert_awaited_once_with(request, stream=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("headers", "expected_metaint"),
    [
        ({}, 0),
        ({"icy-metaint": "not-a-number"}, 0),
    ],
)
async def test_open_upstream_handles_missing_or_invalid_metaint(
    headers,
    expected_metaint,
):
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.headers = httpx.Headers(headers)
    response.aclose = AsyncMock()

    client = MagicMock(spec=httpx.AsyncClient)
    client.build_request.return_value = httpx.Request(
        "GET", "https://radio.example.test/live"
    )
    client.send = AsyncMock(return_value=response)
    client.aclose = AsyncMock()

    with patch(
        "opencloudtouch.streaming.icy_proxy.httpx.AsyncClient",
        return_value=client,
    ):
        _, _, metaint = await _open_upstream("https://radio.example.test/live")

    assert metaint == expected_metaint


@pytest.mark.asyncio
async def test_open_upstream_closes_resources_on_http_error():
    response = MagicMock(spec=httpx.Response)
    response.status_code = 503
    response.headers = httpx.Headers()
    response.aclose = AsyncMock()

    client = MagicMock(spec=httpx.AsyncClient)
    client.build_request.return_value = httpx.Request(
        "GET", "http://radio.example.test/live"
    )
    client.send = AsyncMock(return_value=response)
    client.aclose = AsyncMock()

    with patch(
        "opencloudtouch.streaming.icy_proxy.httpx.AsyncClient",
        return_value=client,
    ):
        with pytest.raises(RuntimeError, match="HTTP 503"):
            await _open_upstream("http://radio.example.test/live")

    response.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_upstream_closes_client_on_connection_error():
    client = MagicMock(spec=httpx.AsyncClient)
    client.build_request.return_value = httpx.Request(
        "GET", "http://radio.example.test/live"
    )
    client.send = AsyncMock(side_effect=httpx.ConnectError("failed"))
    client.aclose = AsyncMock()

    with patch(
        "opencloudtouch.streaming.icy_proxy.httpx.AsyncClient",
        return_value=client,
    ):
        with pytest.raises(httpx.ConnectError):
            await _open_upstream("http://radio.example.test/live")

    client.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_upstream_rejects_unsupported_scheme_before_connecting():
    with patch(
        "opencloudtouch.streaming.icy_proxy.httpx.AsyncClient",
    ) as client_cls:
        with pytest.raises(ValueError, match="http or https"):
            await _open_upstream("file:///tmp/audio")

    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_handle_client_rejects_unsupported_stream_scheme():
    reader = _FakeReader(
        b"GET /stream?url=file%3A%2F%2F%2Ftmp%2Faudio&name=Test+Radio "
        b"HTTP/1.1\r\nHost: test\r\n\r\n"
    )
    writer = _FakeWriter()

    await icy_proxy._handle_client(reader, writer)

    assert b"400 Bad Request" in writer.data
    assert writer.closed is True


@pytest.mark.asyncio
async def test_deinterleave_accepts_stream_title_without_semicolon():
    metaint = 4
    raw = b"StreamTitle='Silver Compass'"
    padded_len = ((len(raw) + 15) // 16) * 16
    block = bytes([padded_len // 16]) + raw.ljust(padded_len, b"\x00")
    response = _RawResponse([b"ABCD" + block])
    session = _start_proxy_session("192.0.2.90", "No Semicolon Radio")

    chunks = [
        chunk
        async for chunk in _deinterleave_icy(
            response,
            metaint,
            device_ip="192.0.2.90",
            station_name="No Semicolon Radio",
            session=session,
        )
    ]

    assert b"".join(chunks) == b"ABCD"
    metadata = get_proxy_metadata("192.0.2.90")
    assert metadata is not None
    assert metadata.track == "Silver Compass"


@pytest.mark.asyncio
async def test_failed_reconnect_does_not_replace_existing_session_metadata():
    device_ip = "192.0.2.91"
    active_session = _start_proxy_session(device_ip, "Existing Radio")
    _set_proxy_metadata(
        device_ip,
        ProxyMetadata(
            station_name="Existing Radio",
            artist="Quiet Harbor",
            track="Northern Glass",
            raw_title="Quiet Harbor - Northern Glass",
        ),
        active_session,
    )

    reader = _FakeReader(
        b"GET /stream?url=http%3A%2F%2Fradio.example.test%2Fnew&name=New+Radio "
        b"HTTP/1.1\r\nHost: test\r\n\r\n"
    )
    writer = _FakeWriter(peer=(device_ip, 50003))

    with patch(
        "opencloudtouch.streaming.icy_proxy._open_upstream",
        new=AsyncMock(side_effect=RuntimeError("upstream failed")),
    ):
        await icy_proxy._handle_client(reader, writer)

    metadata = get_proxy_metadata(device_ip)
    assert metadata is not None
    assert metadata.station_name == "Existing Radio"
    assert metadata.artist == "Quiet Harbor"
    assert metadata.track == "Northern Glass"
