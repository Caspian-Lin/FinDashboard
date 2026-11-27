"""Existing data schedule preferences shared by REST and MCP."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

DEFAULT_CONFIG_FILE = "data_config.json"


class DataScheduleConflictError(ValueError):
    """The stored preferences differ from the client's last read."""


def read_data_schedule(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("Data schedule configuration must be a JSON object.")
    return raw


def data_schedule_revision(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def save_data_schedule(
    path: Path,
    updates: dict[str, Any],
    *,
    expected_revision: str | None = None,
) -> dict[str, Any]:
    # Web and local MCP can update the same file from independent processes.
    with path.with_suffix(".json.lock").open("a+b") as lock:
        if sys.platform == "win32":
            import msvcrt

            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        config = read_data_schedule(path)
        if expected_revision is not None and data_schedule_revision(config) != expected_revision:
            raise DataScheduleConflictError("Data schedule configuration changed.")
        config.update(updates)
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as target:
            pending = Path(target.name)
            try:
                json.dump(config, target, ensure_ascii=False, indent=2)
                target.close()
                pending.replace(path)
            finally:
                target.close()
                pending.unlink(missing_ok=True)
        return config
