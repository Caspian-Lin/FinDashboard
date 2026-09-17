"""MCP ``finboard_factor_series_get`` 工件感知载荷单测(issue #463)。

summary:工件行走 symbol 列投影(标的集/工件路径与字节数),不物化
全量 values;detail:经 sha256 校验后物化逐日截面,与行内模式同构。
行内旧行路径不变(回归)。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from finboard_data.factor_series_store import write_series_artifact
from finboard_mcp.tools.factor_series import _series_payload
from finboard_persistence import FactorSeriesRecord


def _values() -> dict[str, dict[str, float | None]]:
    return {
        "2024-01-31": {"600000.SH": 1.5, "000001.SZ": None},
        "2024-02-29": {"600000.SH": 2.0, "000001.SZ": 0.5},
    }


def _kwargs() -> dict[str, Any]:
    return {
        "code_artifact": "return_21d",
        "code_commit": "c" * 40,
        "kind": "predefined_factor",
        "release_id": "DR-bars-1",
        "dataset_release_ids": ("DR-daily-1",),
        "params": {},
        "window_start": date(2024, 1, 1),
        "window_end": date(2024, 3, 31),
        "dates": (date(2024, 1, 31), date(2024, 2, 29)),
    }


def _artifact_record(tmp_path: Path) -> tuple[FactorSeriesRecord, Path]:
    record = FactorSeriesRecord.build(**_kwargs(), values=_values())
    meta = write_series_artifact(tmp_path, record.series_key, record.values)
    kwargs = _kwargs()
    kwargs.pop("dates")
    artifact = FactorSeriesRecord.build_artifact(
        **kwargs,
        dates=record.dates,
        quality={"nan_ratio": 0.0},
        artifact_relpath=meta.relpath,
        artifact_checksum=meta.checksum,
    )
    return artifact, tmp_path


class TestSeriesPayloadArtifact:
    def test_summary_uses_symbol_projection(self, tmp_path: Path) -> None:
        record, root = _artifact_record(tmp_path)
        data = _series_payload(record, "summary", root)
        assert data["symbol_count"] == 2
        assert data["date_count"] == 2
        assert data["artifact"] == {
            "relpath": record.artifact_relpath,
            "bytes": (root / str(record.artifact_relpath)).stat().st_size,
        }
        assert "values" not in data

    def test_detail_materialises_values(self, tmp_path: Path) -> None:
        record, root = _artifact_record(tmp_path)
        data = _series_payload(record, "detail", root)
        assert data["values"] == _values()
        assert data["dates"] == ["2024-01-31", "2024-02-29"]

    def test_inline_row_summary_unchanged(self, tmp_path: Path) -> None:
        inline = FactorSeriesRecord.build(**_kwargs(), values=_values())
        data = _series_payload(inline, "summary", tmp_path)
        assert data["symbol_count"] == 2
        assert data["artifact"] is None
