"""Export OpenAPI from this checkout, not an unrelated installed OCT version."""

import os
import sys
import tomllib
from pathlib import Path

import yaml

backend = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend / "src"))

with (backend / "pyproject.toml").open("rb") as file:
    os.environ.setdefault("OCT_VERSION", tomllib.load(file)["project"]["version"])

from opencloudtouch.main import app  # noqa: E402

output = backend / "openapi.yaml"
output.write_text(
    yaml.safe_dump(app.openapi(), sort_keys=False, allow_unicode=True),
    encoding="utf-8",
)
print("OpenAPI YAML exported")
