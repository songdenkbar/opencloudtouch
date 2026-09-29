"""Experimental non-chunked ICY stream proxy for SoundTouch devices.

The proxy opens the upstream radio stream with Icy-MetaData: 1, strips
interleaved ICY metadata blocks, and forwards clean audio over a plain
HTTP/1.1 connection without Transfer-Encoding: chunked.

This module is intentionally small and opt-in.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx

from opencloudtouch.streaming.icy_metadata import parse_stream_title

logger = logging.getLogger(__name__)

_STREAM_TITLE_RE = re.compile(r"StreamTitle='(.*?)'(?:;|$)", re.DOTALL)
_SUPPORTED_STREAM_SCHEMES = {"http", "https"}


def _validate_upstream_url(url: str) -> None:
    """Reject unsupported or malformed upstream stream URLs."""
    parsed = urlparse(url)
    if parsed.scheme not in _SUPPORTED_STREAM_SCHEMES or not parsed.netloc:
        raise ValueError("upstream URL must use http or https")


@dataclass(frozen=True, slots=True)
class ProxyMetadata:
    """Latest ICY metadata observed for one SoundTouch client."""

    station_name: str
    artist: str | None = None
    track: str | None = None
    raw_title: str = ""


_metadata_by_device: dict[str, ProxyMetadata] = {}
_active_session_by_device: dict[str, object] = {}


def get_proxy_metadata(device_ip: str) -> ProxyMetadata | None:
    """Return latest proxy metadata for a SoundTouch client IP."""
    return _metadata_by_device.get(device_ip)


def get_all_proxy_metadata() -> dict[str, ProxyMetadata]:
    """Return a snapshot of the latest proxy metadata by SoundTouch client IP."""
    return dict(_metadata_by_device)


def _start_proxy_session(device_ip: str, station_name: str) -> object:
    """Register a new active proxy stream for one SoundTouch client."""
    session = object()
    if device_ip:
        _active_session_by_device[device_ip] = session
        _metadata_by_device[device_ip] = ProxyMetadata(station_name=station_name)
    return session


def _set_proxy_metadata(
    device_ip: str,
    metadata: ProxyMetadata,
    session: object,
) -> None:
    """Store metadata only while this proxy session is still current."""
    if device_ip and _active_session_by_device.get(device_ip) is session:
        _metadata_by_device[device_ip] = metadata


def _end_proxy_session(device_ip: str, session: object) -> None:
    """Remove cache state when the current proxy stream ends."""
    if device_ip and _active_session_by_device.get(device_ip) is session:
        _active_session_by_device.pop(device_ip, None)
        _metadata_by_device.pop(device_ip, None)


async def _open_upstream(
    url: str,
) -> tuple[httpx.AsyncClient, httpx.Response, int]:
    _validate_upstream_url(url)
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(None, connect=10.0),
        follow_redirects=True,
        headers={
            "Icy-MetaData": "1",
            "User-Agent": "OpenCloudTouch-ICY-Proxy/0.1",
            "Accept-Encoding": "identity",
        },
    )
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
    except Exception:
        await client.aclose()
        raise

    if response.status_code != 200:
        status = response.status_code
        await response.aclose()
        await client.aclose()
        raise RuntimeError(f"upstream returned HTTP {status}")

    raw_metaint = response.headers.get("icy-metaint")
    try:
        metaint = int(raw_metaint) if raw_metaint else 0
    except ValueError:
        metaint = 0

    return client, response, metaint


async def _passthrough(response: httpx.Response) -> AsyncIterator[bytes]:
    async for chunk in response.aiter_raw():
        yield chunk


async def _deinterleave_icy(
    response: httpx.Response,
    metaint: int,
    *,
    device_ip: str,
    station_name: str,
    session: object,
) -> AsyncIterator[bytes]:
    """Yield only audio bytes while consuming/logging ICY metadata blocks."""
    audio_remaining = metaint
    metadata_remaining = 0
    metadata_buffer = bytearray()
    waiting_for_length = False

    async for chunk in response.aiter_raw():
        view = memoryview(chunk)
        pos = 0

        while pos < len(view):
            if metadata_remaining > 0:
                take = min(metadata_remaining, len(view) - pos)
                metadata_buffer.extend(view[pos : pos + take])
                pos += take
                metadata_remaining -= take

                if metadata_remaining == 0:
                    raw = bytes(metadata_buffer).rstrip(b"\x00")
                    metadata_buffer.clear()
                    if raw:
                        try:
                            decoded = raw.decode("utf-8")
                        except UnicodeDecodeError:
                            decoded = raw.decode("latin-1", errors="replace")
                        match = _STREAM_TITLE_RE.search(decoded)
                        if match:
                            raw_title = match.group(1)
                            parsed = parse_stream_title(raw_title, station_name)
                            _set_proxy_metadata(
                                device_ip,
                                ProxyMetadata(
                                    station_name=station_name,
                                    artist=parsed.artist,
                                    track=parsed.track,
                                    raw_title=raw_title,
                                ),
                                session,
                            )
                            logger.info(
                                "[ICY PROXY] StreamTitle=%r artist=%r track=%r",
                                raw_title,
                                parsed.artist,
                                parsed.track,
                            )
                    audio_remaining = metaint
                continue

            if waiting_for_length:
                metadata_remaining = int(view[pos]) * 16
                pos += 1
                waiting_for_length = False
                if metadata_remaining == 0:
                    audio_remaining = metaint
                continue

            take = min(audio_remaining, len(view) - pos)
            if take:
                yield bytes(view[pos : pos + take])
                pos += take
                audio_remaining -= take

            if audio_remaining == 0:
                waiting_for_length = True


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> None:
    peer = writer.get_extra_info("peername")
    client: httpx.AsyncClient | None = None
    upstream: httpx.Response | None = None
    device_ip = ""
    session: object | None = None

    try:
        request_head = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"),
            timeout=5,
        )
        request_line = request_head.split(b"\r\n", 1)[0].decode("latin-1")
        method, target, _version = request_line.split(" ", 2)

        if method != "GET":
            writer.write(
                b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            return

        parsed = urlparse(target)
        if parsed.path != "/stream":
            writer.write(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        query = parse_qs(parsed.query)
        url = query.get("url", [""])[0]
        station_name = query.get("name", [""])[0]
        if not url:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        try:
            _validate_upstream_url(url)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return

        device_ip = peer[0] if peer else ""
        client, upstream, metaint = await _open_upstream(url)
        session = _start_proxy_session(device_ip, station_name)
        content_type = upstream.headers.get("content-type", "audio/mpeg")

        headers = [
            "HTTP/1.1 200 OK",
            f"Content-Type: {content_type}",
            "Connection: close",
            "Cache-Control: no-cache",
        ]
        for name in (
            "icy-br",
            "ice-audio-info",
            "icy-description",
            "icy-genre",
            "icy-name",
            "icy-pub",
            "icy-url",
        ):
            value = upstream.headers.get(name)
            if value:
                headers.append(f"{name}: {value}")
        headers.extend(["", ""])

        writer.write("\r\n".join(headers).encode("latin-1", errors="replace"))
        await writer.drain()

        logger.info(
            "[ICY PROXY] Started peer=%s url=%s content_type=%s icy_metaint=%s",
            peer,
            url,
            content_type,
            metaint or None,
        )

        source = (
            _deinterleave_icy(
                upstream,
                metaint,
                device_ip=device_ip,
                station_name=station_name,
                session=session,
            )
            if metaint > 0
            else _passthrough(upstream)
        )
        async for audio in source:
            writer.write(audio)
            await writer.drain()

    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass
    except asyncio.TimeoutError:
        logger.debug("[ICY PROXY] Request timeout from %s", peer)
    except Exception:
        logger.exception("[ICY PROXY] Failed for %s", peer)
    finally:
        if session is not None:
            _end_proxy_session(device_ip, session)
        if upstream is not None:
            await upstream.aclose()
        if client is not None:
            await client.aclose()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            logger.debug("[ICY PROXY] Error while closing peer=%s", peer, exc_info=True)
        logger.info("[ICY PROXY] Stopped peer=%s", peer)


async def start_icy_proxy(
    host: str = "0.0.0.0",  # nosec B104 - LAN devices must reach the proxy
    port: int = 7789,
) -> asyncio.AbstractServer:
    """Start the experimental non-chunked ICY proxy server."""
    server = await asyncio.start_server(_handle_client, host, port)
    logger.info("[ICY PROXY] Listening on %s:%d", host, port)
    return server
