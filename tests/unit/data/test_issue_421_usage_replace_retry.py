"""运营事故修复回归:tushare_usage.json 的 os.replace 在 Windows 上被另一进程
(API 预算快照端点的读句柄)短暂挡住时报 WinError 5,导致整条 dataset_sync /
bulk_download 任务失败。写入侧短退避重试收敛。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from finboard_data.tushare_budget import (
    _REPLACE_RETRY_DELAYS,
    TushareRequestBudget,
    _replace_with_retry,
)


def _install_flaky_replace(
    monkeypatch: pytest.MonkeyPatch, fail_times: int
) -> dict[str, int]:
    """前 fail_times 次 Path.replace 抛 PermissionError,之后走真实现。"""
    real = Path.replace
    state = {"calls": 0}

    def fake(self: Path, target: str | os.PathLike[str]) -> None:
        state["calls"] += 1
        if state["calls"] <= fail_times:
            raise PermissionError(5, "拒绝访问。")
        real(self, target)

    monkeypatch.setattr(Path, "replace", fake)
    return state


def test_replace_retry_succeeds_after_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "finboard_data.tushare_budget._REPLACE_RETRY_DELAYS", (0.0, 0.0, 0.0)
    )
    dst = tmp_path / "usage.json"
    dst.write_text("old", encoding="utf-8")
    src = tmp_path / "usage.json.tmp"
    src.write_text("new", encoding="utf-8")

    state = _install_flaky_replace(monkeypatch, fail_times=2)
    _replace_with_retry(src, dst)

    assert state["calls"] == 3
    assert dst.read_text(encoding="utf-8") == "new"
    assert not src.exists()


def test_replace_retry_exhausts_and_raises_fail_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "finboard_data.tushare_budget._REPLACE_RETRY_DELAYS", (0.0, 0.0)
    )
    dst = tmp_path / "usage.json"
    dst.write_text("old", encoding="utf-8")
    src = tmp_path / "usage.json.tmp"
    src.write_text("new", encoding="utf-8")

    state = _install_flaky_replace(monkeypatch, fail_times=99)
    with pytest.raises(PermissionError):
        _replace_with_retry(src, dst)
    assert state["calls"] == 3  # 2 次重试 + 最终原样抛出的一次
    assert dst.read_text(encoding="utf-8") == "old"


def test_write_usage_end_to_end_with_transient_reader_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """完整 _write_usage 路径:replace 偶发 WinError 5 后重试成功落盘。"""
    monkeypatch.setattr("finboard_data.tushare_budget._REPLACE_RETRY_DELAYS", (0.0,))
    usage = tmp_path / "usage.json"
    budget = TushareRequestBudget(
        requests_per_minute=200,
        daily_request_limit=100_000,
        usage_file=usage,
    )
    usage.write_text(
        json.dumps({"date": "2026-09-10", "requests": 41}), encoding="utf-8"
    )

    state = _install_flaky_replace(monkeypatch, fail_times=1)
    budget._write_usage("2026-09-10", 42)

    assert state["calls"] == 2
    assert json.loads(usage.read_text(encoding="utf-8")) == {
        "date": "2026-09-10",
        "requests": 42,
    }
    assert not usage.with_suffix(".json.tmp").exists()


def test_retry_delays_are_bounded() -> None:
    """退避总和有上界,不会把 200 RPM 时隙(0.3s)无限顶穿。"""
    assert sum(_REPLACE_RETRY_DELAYS) < 1.0
