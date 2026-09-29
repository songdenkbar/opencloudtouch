"""BMX resolver routes for Bose SoundTouch devices.

This module implements the BMX (Bose Media eXchange) endpoints that the
SoundTouch device normally calls at bmx.bose.com. By redirecting the device
to OCT via USB configuration, these endpoints provide:

1. /bmx/registry/v1/services - Service registry (TuneIn, custom stations)
2. /bmx/tunein/v1/playback/station/{id} - TuneIn stream resolution
3. /core02/svc-bmx-adapter-orion/prod/orion/station - Custom stream playback
"""

import base64
import json
import logging
import os

from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencloudtouch.bmx.models import (
    BmxAudio,
    BmxPlaybackResponse,
    BmxService,
    BmxServiceAssets,
    BmxServiceId,
    BmxServicesResponse,
    BmxStream,
)
from opencloudtouch.bmx.stream_utils import convert_https_to_http
from opencloudtouch.bmx.tunein import get_oct_base_url, resolve_tunein_station
from opencloudtouch.streaming.icy_proxy import (
    get_all_proxy_metadata,
    get_proxy_metadata,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["bmx"])


def _maybe_proxy_stream_url(stream_url: str, station_name: str = "") -> str:
    """Wrap a custom radio stream with the optional ICY proxy.

    This is intentionally opt-in. Set OCT_STREAM_PROXY_URL to the proxy
    endpoint, for example http://<oct-host>:7789/stream.
    """
    proxy_url = os.getenv("OCT_STREAM_PROXY_URL", "").strip()
    if not proxy_url or not stream_url:
        return stream_url
    return f"{proxy_url}?{urlencode({'url': stream_url, 'name': station_name})}"


# =============================================================================
# BMX Registry Endpoint
# =============================================================================


def _proxy_now_playing_payload(request: Request) -> dict:
    """Build the Orion NowPlaying projection from live proxy metadata."""
    device_ip = request.client.host if request.client else ""
    metadata = get_proxy_metadata(device_ip)

    if metadata is None:
        return {
            "track": "",
            "album": "",
            "artist": "",
            "askAgainAfter": 6,
            "imageUrl": "",
            "_links": {},
        }

    return {
        "track": metadata.track or metadata.raw_title or metadata.station_name,
        "album": "",
        "artist": metadata.artist or "",
        "askAgainAfter": 6,
        "imageUrl": "",
        "_links": {},
    }


@router.get("/debug/icy-proxy", include_in_schema=False)
async def debug_icy_proxy() -> JSONResponse:
    """Return the current ICY proxy metadata cache for development diagnostics."""
    snapshot = get_all_proxy_metadata()
    return JSONResponse(
        content={
            device_ip: {
                "station_name": metadata.station_name,
                "artist": metadata.artist,
                "track": metadata.track,
                "raw_title": metadata.raw_title,
            }
            for device_ip, metadata in snapshot.items()
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get("/bmx/orion/now-playing/station/{station_id}")
@router.get("/bmx/orion/now-playing")
async def bmx_now_playing(
    request: Request,
    station_id: str | None = None,
) -> JSONResponse:
    """Serve live metadata through the existing Orion NowPlaying endpoint."""
    payload = _proxy_now_playing_payload(request)
    logger.info(
        "[BMX NOW-PLAYING] Station: %s client=%s track=%r artist=%r",
        station_id or "custom",
        request.client.host if request.client else "",
        payload["track"],
        payload["artist"],
    )
    return JSONResponse(
        content=payload,
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.post("/bmx/orion/reporting/station/{station_id}")
@router.post("/bmx/orion/reporting")
async def bmx_reporting(
    request: Request,
    station_id: str | None = None,
) -> JSONResponse:
    """Return the same live metadata projection in the reporting response."""
    now_playing = _proxy_now_playing_payload(request)
    logger.info(
        "[BMX REPORTING] Station: %s client=%s",
        station_id or "custom",
        request.client.host if request.client else "",
    )
    return JSONResponse(
        content={
            "status": "ok",
            "nextReportIn": now_playing["askAgainAfter"],
            "_embedded": {"bmx_nowplaying": now_playing},
        },
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get("/bmx/tunein/v1/now-playing/station/{station_id}")
async def bmx_tunein_now_playing(station_id: str) -> JSONResponse:
    """TuneIn now-playing stub.

    Device calls this to get currently playing track info.
    """
    logger.info("[BMX TUNEIN NOW-PLAYING] Station: %s", station_id)  # NOSONAR
    return JSONResponse(
        content={"status": "playing", "stationId": station_id},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.post("/bmx/tunein/v1/reporting/station/{station_id}")
async def bmx_tunein_reporting(station_id: str) -> JSONResponse:
    """TuneIn reporting stub.

    Device calls this to report playback events.
    """
    logger.info("[BMX TUNEIN REPORTING] Station: %s", station_id)  # NOSONAR
    return JSONResponse(
        content={"status": "ok"},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get("/bmx/tunein/v1/favorite/{station_id}")
@router.post("/bmx/tunein/v1/favorite/{station_id}")
async def bmx_tunein_favorite(station_id: str) -> JSONResponse:
    """TuneIn favorite stub.

    Device calls this to mark/unmark stations as favorites.
    """
    logger.info("[BMX TUNEIN FAVORITE] Station: %s", station_id)  # NOSONAR
    return JSONResponse(
        content={"status": "ok", "isFavorite": False},
        headers={"Access-Control-Allow-Origin": "*"},
    )


@router.get("/bmx/registry/v1/services")
async def bmx_services() -> JSONResponse:
    """Return list of available BMX services.

    This endpoint is called by the device after booting to discover
    available streaming services. We provide:
    - TUNEIN: Resolved via TuneIn API
    - LOCAL_INTERNET_RADIO: Custom stations via OCT
    """
    base_url = get_oct_base_url()
    logger.debug("[BMX REGISTRY] base_url=%s", base_url)

    services = [
        BmxService(
            id=BmxServiceId(name="TUNEIN", value=25),
            baseUrl=f"{base_url}/bmx/tunein",
            assets=BmxServiceAssets(
                name="TuneIn",
                description="Internet radio stations via TuneIn",
            ),
            streamTypes=["liveRadio", "onDemand"],
        ),
        BmxService(
            id=BmxServiceId(name="LOCAL_INTERNET_RADIO", value=11),
            baseUrl=f"{base_url}/core02/svc-bmx-adapter-orion/prod/orion",
            assets=BmxServiceAssets(
                name="Custom Stations",
                description="Custom radio stations via OCT",
            ),
            streamTypes=["liveRadio"],
        ),
        BmxService(
            id=BmxServiceId(name="RADIOBROWSER", value=99),
            baseUrl=f"{base_url}/bmx/radiobrowser",
            assets=BmxServiceAssets(
                name="RadioBrowser",
                description="Community radio stations via RadioBrowser.info",
            ),
            streamTypes=["liveRadio"],
        ),
    ]

    response = BmxServicesResponse(bmx_services=services)

    logger.info("[BMX REGISTRY] Returning %d services", len(services))
    logger.debug(
        "[BMX REGISTRY] Services: %s",
        [s.id.name for s in services],
    )

    return JSONResponse(
        content=response.model_dump(by_alias=True),
        headers={
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
    )


# =============================================================================
# TuneIn Playback Endpoint
# =============================================================================


@router.get("/bmx/tunein/v1/playback/station/{station_id}")
async def bmx_tunein_playback(station_id: str) -> JSONResponse:
    """Resolve TuneIn station to stream URL.

    The device calls this endpoint with a station ID (e.g., "s158432")
    and expects a JSON response with stream URLs.
    """
    try:
        response = await resolve_tunein_station(station_id)

        # Add CORS headers
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type",
        }

        return JSONResponse(content=response.model_dump(by_alias=True), headers=headers)
    except Exception as e:
        logger.exception("[BMX TUNEIN] Playback error: %s", e)
        return JSONResponse(
            content={"error": str(e)},
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )


# =============================================================================
# Custom Station Playback (Orion Adapter)
# =============================================================================


@router.get("/core02/svc-bmx-adapter-orion/prod/orion/station")
async def custom_stream_playback(request: Request) -> JSONResponse:
    """Play custom stream URL.

    This endpoint handles LOCAL_INTERNET_RADIO sources. The data parameter
    contains base64-encoded JSON with streamUrl, imageUrl, and name.
    """
    data = request.query_params.get("data", "")

    if not data:
        return JSONResponse(
            content={"error": "Missing data parameter"},
            status_code=400,
            headers={"Access-Control-Allow-Origin": "*"},
        )

    try:
        # Decode base64 data
        json_str = base64.urlsafe_b64decode(data).decode("utf-8")
        json_obj = json.loads(json_str)
        logger.debug("[BMX ORION] Decoded data: %s", json_str[:300])

        stream_url = json_obj.get("streamUrl", "")
        tunein_id = json_obj.get("tuneinId", "")
        image_url = json_obj.get("imageUrl", "")
        name = json_obj.get("name", "Custom Station")

        # TuneIn stations: resolve stream URL dynamically via TuneIn API
        if tunein_id and not stream_url:
            logger.info("[BMX ORION] TuneIn station detected: %s (%s)", tunein_id, name)
            return await _resolve_tunein_for_orion(tunein_id)

        # Convert HTTPS to HTTP - Bose devices can't play HTTPS streams
        stream_url = convert_https_to_http(stream_url)

        # Optionally route the final audio URL through the ICY proxy.
        # The SoundTouch still uses the normal OCT/BMX playback flow; only the
        # returned audio URL changes. With the variable unset, behavior is unchanged.
        direct_stream_url = stream_url
        stream_url = _maybe_proxy_stream_url(stream_url, name)

        logger.info(
            "[BMX ORION] Custom stream: %s → %s%s",
            name,
            stream_url,
            " (ICY proxy)" if stream_url != direct_stream_url else "",
        )

        stream = BmxStream(streamUrl=stream_url)
        audio = BmxAudio(streamUrl=stream_url, streams=[stream])

        # Add critical links
        base_url = get_oct_base_url()
        links = {
            "bmx_nowplaying": {
                "href": f"{base_url}/bmx/orion/now-playing",
                "useInternalClient": "ALWAYS",
            },
            "bmx_reporting": {"href": f"{base_url}/bmx/orion/reporting"},
        }

        response = BmxPlaybackResponse(
            audio=audio,
            links=links,
            imageUrl=image_url,
            name=name,
        )

        return JSONResponse(
            content=response.model_dump(by_alias=True),
            headers={"Access-Control-Allow-Origin": "*"},
        )

    except Exception as e:
        logger.exception("[BMX ORION] Error: %s", e)
        return JSONResponse(
            content={"error": str(e)},
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )


async def _resolve_tunein_for_orion(tunein_id: str) -> JSONResponse:
    """Resolve TuneIn station dynamically for Orion playback.

    Called when a preset contains a tuneinId but no streamUrl.
    Fetches fresh stream URL from TuneIn API at playback time.
    """
    try:
        response = await resolve_tunein_station(tunein_id)
        return JSONResponse(
            content=response.model_dump(by_alias=True),
            headers={"Access-Control-Allow-Origin": "*"},
        )
    except Exception as e:
        logger.exception(
            "[BMX ORION] TuneIn resolution failed for %s: %s", tunein_id, e
        )
        return JSONResponse(
            content={"error": f"TuneIn resolution failed: {e}"},
            status_code=500,
            headers={"Access-Control-Allow-Origin": "*"},
        )
