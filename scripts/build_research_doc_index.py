"""Record the exact research documents packaged in a distribution image."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path


def write_index(root: Path, output: Path, revision: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Expected the Git checkout SHA")
    files = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.md"))
        if path.is_file() and path.resolve().is_relative_to(root.resolve())
    }
    output.write_text(
        json.dumps({"schema": "finboard-research-docs-v1", "revision": revision, "files": files}),
        encoding="utf-8",
    )


if __name__ == "__main__":
    write_index(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3])
