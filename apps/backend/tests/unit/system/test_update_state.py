import json

from opencloudtouch.system import update_state


def test_get_update_status_defaults_to_idle(tmp_path, monkeypatch):
    status_path = tmp_path / "update-status.json"
    monkeypatch.setattr(update_state, "STATUS_PATH", status_path)

    assert update_state.get_update_status() == {"status": "idle"}


def test_set_and_get_update_status(tmp_path, monkeypatch):
    status_path = tmp_path / "update-status.json"
    monkeypatch.setattr(update_state, "STATUS_PATH", status_path)

    update_state.set_update_status(
        "completed",
        container_id="abc123",
        image="example:latest",
    )

    assert update_state.get_update_status() == {
        "status": "completed",
        "container_id": "abc123",
        "image": "example:latest",
    }


def test_get_update_status_handles_invalid_json(tmp_path, monkeypatch):
    status_path = tmp_path / "update-status.json"
    status_path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(update_state, "STATUS_PATH", status_path)

    assert update_state.get_update_status() == {"status": "unknown"}


def test_set_update_status_writes_valid_json(tmp_path, monkeypatch):
    status_path = tmp_path / "update-status.json"
    monkeypatch.setattr(update_state, "STATUS_PATH", status_path)

    update_state.set_update_status("pulling")

    data = json.loads(status_path.read_text(encoding="utf-8"))

    assert data == {"status": "pulling"}
