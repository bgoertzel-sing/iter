"""Provider-free fixture tool for the ProtoCosmo2 Iter round-trip test."""
from __future__ import annotations

import json
import os
from pathlib import Path


DESCRIPTION = "Read the bounded provider-free runtime status fixture."


def run():
    raw = os.environ.get("ITER_STATUS_FIXTURE")
    if not raw:
        raise RuntimeError("ITER_STATUS_FIXTURE is required")
    path = Path(raw)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise RuntimeError("status fixture must be an absolute regular non-symlink file")
    value = json.loads(path.read_text(encoding="utf-8"))
    if type(value) is not dict or set(value) != {"identity", "status"}:
        raise RuntimeError("status fixture schema mismatch")
    if value["identity"] != "ProtoCosmo2" or value["status"] != "healthy":
        raise RuntimeError("status fixture identity/state mismatch")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
