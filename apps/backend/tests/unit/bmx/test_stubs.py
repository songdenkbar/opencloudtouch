"""Unit tests for BMX stub and resolve endpoints.

Covers:
- Stub endpoints for now-playing, reporting, tunein, favorite
- /bmx/resolve endpoint for stream URL resolution
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencloudtouch.bmx.routes import router


@pytest.fixture
def app():
    app_ = FastAPI()
    app_.include_router(router)
    return app_


@pytest.fixture
def client(app):
    return TestClient(app)


class TestNowPlayingStub:
    """Tests for the existing Orion NowPlaying endpoint."""

    def test_now_playing_without_proxy_metadata(self, client):
        response = client.get("/bmx/orion/now-playing")
        assert response.status_code == 200
        data = response.json()
        assert data["track"] == ""
        assert data["artist"] == ""
        assert data["askAgainAfter"] == 6
        assert data["_links"] == {}

    def test_now_playing_uses_proxy_metadata(self, client):
        from opencloudtouch.streaming.icy_proxy import ProxyMetadata

        metadata = ProxyMetadata(
            station_name="Test Radio",
            artist="Paper Satellites",
            track="Silver rooms",
            raw_title='"Silver rooms" von Paper Satellites',
        )
        with patch(
            "opencloudtouch.bmx.routes.get_proxy_metadata",
            return_value=metadata,
        ):
            response = client.get("/bmx/orion/now-playing/station/custom")

        assert response.status_code == 200
        data = response.json()
        assert data["track"] == "Silver rooms"
        assert data["artist"] == "Paper Satellites"
        assert data["album"] == ""


class TestReportingStub:
    """Tests for the existing Orion reporting endpoint."""

    def test_reporting_embeds_now_playing(self, client):
        from opencloudtouch.streaming.icy_proxy import ProxyMetadata

        metadata = ProxyMetadata(
            station_name="Test Radio",
            artist="Artist",
            track="Track",
            raw_title="Artist - Track",
        )
        with patch(
            "opencloudtouch.bmx.routes.get_proxy_metadata",
            return_value=metadata,
        ):
            response = client.post("/bmx/orion/reporting")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["nextReportIn"] == 6
        assert data["_embedded"]["bmx_nowplaying"]["track"] == "Track"
        assert data["_embedded"]["bmx_nowplaying"]["artist"] == "Artist"


class TestTuneInStubs:
    """Tests for TuneIn stub endpoints."""

    def test_tunein_now_playing(self, client):
        """GET /bmx/tunein/v1/now-playing/station/{id} returns 200."""
        response = client.get("/bmx/tunein/v1/now-playing/station/s345678")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "playing"
        assert data["stationId"] == "s345678"

    def test_tunein_reporting(self, client):
        """POST /bmx/tunein/v1/reporting/station/{id} returns 200."""
        response = client.post("/bmx/tunein/v1/reporting/station/s345678")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_tunein_favorite_get(self, client):
        """GET /bmx/tunein/v1/favorite/{id} returns 200."""
        response = client.get("/bmx/tunein/v1/favorite/s345678")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["isFavorite"] is False

    def test_tunein_favorite_post(self, client):
        """POST /bmx/tunein/v1/favorite/{id} returns 200."""
        response = client.post("/bmx/tunein/v1/favorite/s345678")
        assert response.status_code == 200
        assert response.json()["isFavorite"] is False


class TestProxyMetadataFallbacks:
    def test_now_playing_falls_back_to_raw_title(self, client):
        from opencloudtouch.streaming.icy_proxy import ProxyMetadata

        metadata = ProxyMetadata(
            station_name="Harbor Radio",
            artist=None,
            track=None,
            raw_title="Midnight Current",
        )
        with patch(
            "opencloudtouch.bmx.routes.get_proxy_metadata",
            return_value=metadata,
        ):
            response = client.get("/bmx/orion/now-playing")

        assert response.status_code == 200
        data = response.json()
        assert data["track"] == "Midnight Current"
        assert data["artist"] == ""

    def test_now_playing_falls_back_to_station_name(self, client):
        from opencloudtouch.streaming.icy_proxy import ProxyMetadata

        metadata = ProxyMetadata(
            station_name="Harbor Radio",
            artist=None,
            track=None,
            raw_title="",
        )
        with patch(
            "opencloudtouch.bmx.routes.get_proxy_metadata",
            return_value=metadata,
        ):
            response = client.get("/bmx/orion/now-playing")

        assert response.status_code == 200
        data = response.json()
        assert data["track"] == "Harbor Radio"
        assert data["artist"] == ""

    def test_reporting_returns_empty_metadata_after_proxy_cache_cleared(self, client):
        with patch(
            "opencloudtouch.bmx.routes.get_proxy_metadata",
            return_value=None,
        ):
            response = client.post("/bmx/orion/reporting")

        assert response.status_code == 200
        data = response.json()
        embedded = data["_embedded"]["bmx_nowplaying"]
        assert embedded["track"] == ""
        assert embedded["artist"] == ""
        assert data["nextReportIn"] == 6

    def test_debug_endpoint_serializes_proxy_snapshot(self, client):
        from opencloudtouch.streaming.icy_proxy import ProxyMetadata

        snapshot = {
            "192.0.2.80": ProxyMetadata(
                station_name="Copper Radio",
                artist="Lantern Coast",
                track="Quiet Geometry",
                raw_title="Lantern Coast - Quiet Geometry",
            )
        }
        with patch(
            "opencloudtouch.bmx.routes.get_all_proxy_metadata",
            return_value=snapshot,
        ):
            response = client.get("/debug/icy-proxy")

        assert response.status_code == 200
        assert response.json() == {
            "192.0.2.80": {
                "station_name": "Copper Radio",
                "artist": "Lantern Coast",
                "track": "Quiet Geometry",
                "raw_title": "Lantern Coast - Quiet Geometry",
            }
        }
