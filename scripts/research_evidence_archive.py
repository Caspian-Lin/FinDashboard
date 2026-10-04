"""原始轮次证据写仓库外本地存档,仓库只保留人工报告与精简索引。"""

from __future__ import annotations

import os
from pathlib import Path


def local_round_dir(round_name: str) -> Path:
    root = Path(__file__).resolve().parents[1]
    archive_root = Path(os.environ.get(
        "FINBOARD_RESEARCH_ARCHIVE_ROOT", str(root.parent / "FinDashboard-research-archives")
    )).expanduser().resolve()
    if archive_root == root or root in archive_root.parents:
        raise ValueError("research_archive_must_be_outside_repository")
    if Path(round_name).name != round_name or round_name in {"", ".", ".."}:
        raise ValueError("invalid_research_round_name")
    return archive_root / round_name
