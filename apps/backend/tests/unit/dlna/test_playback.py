"""Tests for DLNA playback queue management."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from opencloudtouch.dlna.models import DlnaItem
from opencloudtouch.dlna.playback import DlnaPlaybackError, DlnaPlaybackService


def make_track(track_id: str, url: str) -> DlnaItem:
    """Create a playable DLNA test item."""
    return DlnaItem(
        id=track_id,
        parent_id="1$4",
        title=track_id,
        is_container=False,
        resource_url=url,
        media_class="object.item.audioItem.musicTrack",
    )


@pytest.mark.asyncio
async def test_play_creates_queue():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    result = await service.play(
        device_id="device-1",
        device_ip="192.0.2.10",
        server_id="server-1",
        items=[first, second],
        object_id="track-1",
    )

    assert result == first
    renderer.play_uri.assert_awaited_once_with(
        "192.0.2.10",
        "http://server/1.mp3",
    )


@pytest.mark.asyncio
async def test_next_track():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-1",
    )

    renderer.reset_mock()

    result = await service.next("device-1", "192.0.2.10")

    assert result == second
    renderer.play_uri.assert_awaited_once_with(
        "192.0.2.10",
        "http://server/2.mp3",
    )


@pytest.mark.asyncio
async def test_previous_track():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-2",
    )

    renderer.reset_mock()

    result = await service.previous("device-1", "192.0.2.10")

    assert result == first


@pytest.mark.asyncio
async def test_next_at_end_fails():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )

    with pytest.raises(DlnaPlaybackError, match="No next"):
        await service.next("device-1", "192.0.2.10")


@pytest.mark.asyncio
async def test_previous_at_start_fails():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )

    with pytest.raises(DlnaPlaybackError, match="No previous"):
        await service.previous("device-1", "192.0.2.10")


@pytest.mark.asyncio
async def test_pause_and_resume():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )

    await service.pause("device-1", "192.0.2.10")
    await service.resume("device-1", "192.0.2.10")

    renderer.pause.assert_awaited_once_with("192.0.2.10")
    renderer.resume.assert_awaited_once_with("192.0.2.10")


@pytest.mark.asyncio
async def test_unknown_playback_state():
    service = DlnaPlaybackService(renderer=AsyncMock())

    with pytest.raises(DlnaPlaybackError, match="No active"):
        await service.next("missing-device", "192.0.2.10")


@pytest.mark.asyncio
async def test_stopped_without_playing_does_not_advance():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-1",
    )
    renderer.reset_mock()

    result = await service.handle_transport_state("device-1", "STOPPED")

    assert result is None
    renderer.play_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_playing_then_stopped_advances_queue():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-1",
    )
    renderer.reset_mock()

    await service.handle_transport_state("device-1", "PLAYING")
    result = await service.handle_transport_state("device-1", "STOPPED")

    assert result is None
    await asyncio.sleep(0.5)
    assert service.current("device-1") == second
    renderer.play_uri.assert_awaited_once_with(
        "192.0.2.10",
        "http://server/2.mp3",
    )


@pytest.mark.asyncio
async def test_extra_stopped_after_advance_does_not_advance_again():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")
    third = make_track("track-3", "http://server/3.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second, third],
        "track-1",
    )
    renderer.reset_mock()

    await service.handle_transport_state("device-1", "PLAYING")
    first_result = await service.handle_transport_state("device-1", "STOPPED")
    second_result = await service.handle_transport_state("device-1", "STOPPED")

    assert first_result is None
    await asyncio.sleep(0.5)
    assert service.current("device-1") == second
    assert second_result is None
    renderer.play_uri.assert_awaited_once_with(
        "192.0.2.10",
        "http://server/2.mp3",
    )


@pytest.mark.asyncio
async def test_stopped_at_queue_end_does_not_fail():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )
    renderer.reset_mock()

    await service.handle_transport_state("device-1", "PLAYING")
    result = await service.handle_transport_state("device-1", "STOPPED")

    assert result is None
    renderer.play_uri.assert_not_awaited()


@pytest.mark.asyncio
async def test_current_returns_active_item():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )

    assert service.current("device-1") == track


@pytest.mark.asyncio
async def test_current_is_empty_after_final_track_stops():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    track = make_track("track-1", "http://server/1.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [track],
        "track-1",
    )

    await service.handle_transport_state("device-1", "PLAYING")
    await service.handle_transport_state("device-1", "STOPPED")

    assert service.current("device-1") is None


@pytest.mark.asyncio
async def test_failed_initial_play_does_not_create_active_queue():
    renderer = AsyncMock()
    renderer.play_uri.side_effect = RuntimeError("UPnP failed")
    service = DlnaPlaybackService(renderer=renderer)
    first = make_track("track-1", "http://server/1.mp3")

    with pytest.raises(RuntimeError, match="UPnP failed"):
        await service.play(
            "device-1",
            "192.0.2.10",
            "server-1",
            [first],
            "track-1",
        )

    assert service.current("device-1") is None


@pytest.mark.asyncio
async def test_failed_next_keeps_current_track():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-1",
    )

    renderer.play_uri.side_effect = RuntimeError("UPnP failed")

    with pytest.raises(RuntimeError, match="UPnP failed"):
        await service.next("device-1", "192.0.2.10")

    assert service.current("device-1") == first


@pytest.mark.asyncio
async def test_failed_previous_keeps_current_track():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        [first, second],
        "track-2",
    )

    renderer.play_uri.side_effect = RuntimeError("UPnP failed")

    with pytest.raises(RuntimeError, match="UPnP failed"):
        await service.previous("device-1", "192.0.2.10")

    assert service.current("device-1") == second


@pytest.mark.asyncio
async def test_concurrent_stopped_events_advance_only_once():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 4)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )

    await service.handle_transport_state("device-1", "PLAYING")
    renderer.reset_mock()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(device_ip, url):
        entered.set()
        await release.wait()

    renderer.play_uri.side_effect = slow_play

    first = asyncio.create_task(service.handle_transport_state("device-1", "STOPPED"))

    await asyncio.wait_for(entered.wait(), timeout=2)

    second = asyncio.create_task(service.handle_transport_state("device-1", "STOPPED"))

    release.set()
    await asyncio.sleep(0.5)
    results = await asyncio.gather(first, second)

    assert results == [None, None]
    await asyncio.sleep(0)
    assert service.current("device-1") == tracks[1]
    renderer.play_uri.assert_awaited_once_with("192.0.2.10", "http://server/2.mp3")


@pytest.mark.asyncio
async def test_manual_next_during_automatic_advance():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 4)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )
    await service.handle_transport_state("device-1", "PLAYING")

    renderer.reset_mock()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(device_ip, url):
        entered.set()
        await release.wait()

    renderer.play_uri.side_effect = slow_play

    automatic = asyncio.create_task(
        service.handle_transport_state("device-1", "STOPPED")
    )
    await asyncio.wait_for(entered.wait(), timeout=2)

    manual = asyncio.create_task(service.next("device-1", "192.0.2.10"))

    # Give the manual operation a chance to start.
    await asyncio.sleep(0)
    release.set()

    await asyncio.gather(automatic, manual)

    assert service.current("device-1") == tracks[2]

    urls = [call.args[1] for call in renderer.play_uri.await_args_list]
    assert urls == [
        "http://server/2.mp3",
        "http://server/3.mp3",
    ]


@pytest.mark.asyncio
async def test_different_devices_can_play_concurrently():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    track = make_track("track-1", "http://server/1.mp3")

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_finished = asyncio.Event()

    async def controlled_play(device_ip, url):
        if device_ip == "192.0.2.10":
            first_started.set()
            await release_first.wait()
        else:
            second_finished.set()

    renderer.play_uri.side_effect = controlled_play

    first = asyncio.create_task(
        service.play("device-1", "192.0.2.10", "server-1", [track], "track-1")
    )

    try:
        await asyncio.wait_for(first_started.wait(), timeout=2)

        second = await asyncio.wait_for(
            service.play("device-2", "192.0.2.11", "server-1", [track], "track-1"),
            timeout=2,
        )

        assert second == track
        assert second_finished.is_set()
        assert service.current("device-2") == track
    finally:
        release_first.set()
        await first


@pytest.mark.asyncio
async def test_device_lock_released_after_playback_failure():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    first = make_track("track-1", "http://server/1.mp3")
    second = make_track("track-2", "http://server/2.mp3")

    await service.play("device-1", "192.0.2.10", "server-1", [first, second], "track-1")

    renderer.play_uri.side_effect = RuntimeError("UPnP failed")

    with pytest.raises(RuntimeError, match="UPnP failed"):
        await service.next("device-1", "192.0.2.10")

    renderer.play_uri.side_effect = None

    result = await service.next("device-1", "192.0.2.10")

    assert result == second
    assert service.current("device-1") == second


@pytest.mark.asyncio
async def test_radio_source_switch_does_not_advance_dlna_queue():
    """A source switch must cancel pending automatic advancement."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 3)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )

    await service.handle_transport_state("device-1", "PLAYING")
    renderer.reset_mock()

    # Observed on SoundTouch: STOPPED followed by
    # PAUSED_PLAYBACK about 135 ms later when switching to radio.
    stopped = asyncio.create_task(service.handle_transport_state("device-1", "STOPPED"))

    await asyncio.sleep(0.15)

    await service.handle_transport_state("device-1", "PAUSED_PLAYBACK")

    await stopped
    await asyncio.sleep(0.5)

    renderer.play_uri.assert_not_awaited()
    assert service.current("device-1") is None


@pytest.mark.asyncio
async def test_manual_next_cancels_pending_automatic_advance():
    """Manual next must prevent an additional delayed track change."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 4)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )
    await service.handle_transport_state("device-1", "PLAYING")
    renderer.reset_mock()

    await service.handle_transport_state("device-1", "STOPPED")

    # Manual next occurs before the 400 ms automatic delay expires.
    result = await service.next("device-1", "192.0.2.10")
    assert result == tracks[1]

    await asyncio.sleep(0.5)

    assert service.current("device-1") == tracks[1]
    renderer.play_uri.assert_awaited_once_with("192.0.2.10", "http://server/2.mp3")


@pytest.mark.asyncio
async def test_close_cancels_pending_automatic_advance():
    """Shutdown must prevent scheduled playback from starting."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 3)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )
    await service.handle_transport_state("device-1", "PLAYING")
    renderer.reset_mock()

    await service.handle_transport_state("device-1", "STOPPED")

    assert len(service._advance_tasks) == 1

    await service.close()
    await asyncio.sleep(0.5)

    renderer.play_uri.assert_not_awaited()
    assert service._advance_tasks == {}


@pytest.mark.asyncio
async def test_manual_next_during_inflight_delayed_advance():
    """Manual next during an active automatic call must remain ordered."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)

    tracks = [make_track(f"track-{i}", f"http://server/{i}.mp3") for i in range(1, 4)]

    await service.play(
        "device-1",
        "192.0.2.10",
        "server-1",
        tracks,
        "track-1",
    )
    await service.handle_transport_state("device-1", "PLAYING")
    renderer.reset_mock()

    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(device_ip, url):
        entered.set()
        await release.wait()

    renderer.play_uri.side_effect = slow_play

    await service.handle_transport_state("device-1", "STOPPED")

    # Wait until the delayed automatic advance reaches the renderer.
    await asyncio.wait_for(entered.wait(), timeout=2)

    manual = asyncio.create_task(service.next("device-1", "192.0.2.10"))

    await asyncio.sleep(0)
    release.set()

    result = await asyncio.wait_for(manual, timeout=2)
    assert result == tracks[2]
    assert service.current("device-1") == tracks[2]

    assert renderer.play_uri.await_count == 2


@pytest.mark.asyncio
async def test_automatic_advance_network_error_is_logged(caplog):
    """A failed delayed UPnP command must be logged without changing index."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    service._advance_delay = 0
    tracks = [
        make_track("track-1", "http://server/1.mp3"),
        make_track("track-2", "http://server/2.mp3"),
    ]
    await service.play("device-1", "192.0.2.10", "server-1", tracks, "track-1")
    await service.handle_transport_state("device-1", "PLAYING")
    renderer.play_uri.side_effect = RuntimeError("renderer offline")
    await service.handle_transport_state("device-1", "STOPPED")
    task = service._advance_tasks["device-1"]
    await task
    assert service._states["device-1"].index == 0
    assert service.current("device-1") is None
    assert "DLNA automatic track advance failed" in caplog.text
    assert "renderer offline" in caplog.text
    assert service._advance_tasks == {}


@pytest.mark.asyncio
async def test_pause_event_does_not_cancel_inflight_network_command():
    """An external pause must not abort an already-started UPnP command."""
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    service._advance_delay = 0
    tracks = [
        make_track("track-1", "http://server/1.mp3"),
        make_track("track-2", "http://server/2.mp3"),
    ]
    await service.play("device-1", "192.0.2.10", "server-1", tracks, "track-1")
    await service.handle_transport_state("device-1", "PLAYING")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow_play(device_ip, url):
        entered.set()
        await release.wait()

    renderer.play_uri.side_effect = slow_play
    await service.handle_transport_state("device-1", "STOPPED")
    await asyncio.wait_for(entered.wait(), timeout=2)
    pause = asyncio.create_task(
        service.handle_transport_state("device-1", "PAUSED_PLAYBACK")
    )
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(pause, timeout=2)
    assert service._states["device-1"].index == 1
    assert service.current("device-1") is None
    assert service._advance_tasks == {}


@pytest.mark.asyncio
async def test_events_after_shutdown_do_not_restart_queue():
    renderer = AsyncMock()
    service = DlnaPlaybackService(renderer=renderer)
    tracks = [
        make_track("track-1", "http://server/1.mp3"),
        make_track("track-2", "http://server/2.mp3"),
    ]
    await service.play("device-1", "192.0.2.10", "server-1", tracks, "track-1")
    await service.close()
    renderer.reset_mock()
    await service.handle_transport_state("device-1", "PLAYING")
    await service.handle_transport_state("device-1", "STOPPED")
    assert renderer.play_uri.await_count == 0
    assert service._advance_tasks == {}
