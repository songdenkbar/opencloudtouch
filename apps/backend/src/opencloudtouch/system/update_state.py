import json
import os
from pathlib import Path
from typing import Any

DB_PATH = Path(os.getenv("OCT_DB_PATH", "/data/oct.db"))
STATUS_PATH = DB_PATH.parent / "update-status.json"


def set_update_status(status: str, **extra: Any) -> None:
    data = {"status": status, **extra}

    tmp_path = STATUS_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(data), encoding="utf-8")
    tmp_path.replace(STATUS_PATH)


def get_update_status() -> dict[str, Any]:
    if not STATUS_PATH.exists():
        return {"status": "idle"}

    try:
        return json.loads(STATUS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"status": "unknown"}
