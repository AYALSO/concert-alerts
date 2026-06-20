from __future__ import annotations

import json
import os
from typing import Any

DATA_DIR = os.environ.get("DATA_DIR", "data")


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def load(name: str, default: Any) -> Any:
    p = _path(name)
    if not os.path.exists(p):
        return default
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return default


def save(name: str, data: Any) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    p = _path(name)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, p)
