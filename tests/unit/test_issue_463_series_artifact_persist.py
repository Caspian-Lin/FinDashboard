"""FactorSeriesRecord 工件模式单测(issue #463)。

覆盖:``build_artifact`` 与 ``build`` 同 key(存储模式切换不破坏内容
寻址)/ 工件行跳过行内 checksum 校验 / 工件行缺 checksum 拒绝 /
persist 全链(构建 record → 写工件 → 换工件模式 record → 惰性读回逐值
等值)—— 执行器落库前的真实变换序列。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest

from finboard_data.factor_series_store import (
    read_series_values,
    write_series_artifact,
)
from finboard_persistence import (
    FactorSeriesRecord,
    compute_content_checksum,
)


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


class TestBuildArtifact:
    def test_series_key_matches_inline_mode(self) -> None:
        inline = FactorSeriesRecord.build(
            **_kwargs(), values=_values(), quality={"nan": 0.0}, source_run_id="RR-1"
        )
        artifact = FactorSeriesRecord.build_artifact(
            **_kwargs(),
            artifact_relpath="ab/" + "a" * 62 + ".parquet",
            artifact_checksum="d" * 64,
            quality={"nan": 0.0},
            source_run_id="RR-1",
        )
        # 同一 (commit, 发布联合集, params, 窗口) 跨存储模式同 series_key
        assert artifact.series_key == inline.series_key
        assert artifact.series_id == inline.series_id
        assert artifact.artifact_relpath is not None
        assert artifact.content_checksum == "d" * 64
        assert dict(artifact.values) == {}

    def test_artifact_row_requires_checksum(self) -> None:
        with pytest.raises(ValueError, match="缺少 content_checksum"):
            FactorSeriesRecord.build_artifact(
                **_kwargs(),
                artifact_relpath="ab/x.parquet",
                artifact_checksum="",
            )

    def test_artifact_row_skips_values_checksum_verify(self) -> None:
        # 工件行不做行内 canonical json 校验(值在文件里,由 sha256 把关)
        artifact = FactorSeriesRecord.build_artifact(
            **_kwargs(),
            artifact_relpath="ab/x.parquet",
            artifact_checksum="d" * 64,
        )
        assert artifact.verify_content_checksum is True  # 默认位未被消费
        # 行内模式同样输入构造即校验:checksum 错误必须拒
        with pytest.raises(ValueError, match="content_checksum 与"):
            FactorSeriesRecord(
                **_kwargs(),
                values=_values(),
                content_checksum="bad",
                series_key=FactorSeriesRecord.build(
                    **_kwargs(), values=_values()
                ).series_key,
                series_id=FactorSeriesRecord.build(
                    **_kwargs(), values=_values()
                ).series_id,
            )
        # 行内模式 checksum 正确时 canonical 规则仍生效(#360 语义不变)
        inline = FactorSeriesRecord.build(**_kwargs(), values=_values())
        assert inline.content_checksum == compute_content_checksum(
            inline.dates, inline.values
        )


class TestPersistTransform:
    def test_write_then_artifact_record_reads_back_equal(self, tmp_path: Any) -> None:
        """执行器落库前的真实序列:inline record → 写工件 → 工件模式 record。"""
        inline = FactorSeriesRecord.build(**_kwargs(), values=_values())

        meta = write_series_artifact(tmp_path, inline.series_key, inline.values)
        kwargs = _kwargs()
        kwargs.pop("dates")
        artifact = FactorSeriesRecord.build_artifact(
            **kwargs,
            dates=inline.dates,
            quality=inline.quality,
            source_run_id=inline.source_run_id,
            artifact_relpath=meta.relpath,
            artifact_checksum=meta.checksum,
        )
        assert artifact.series_key == inline.series_key
        assert artifact.dates == inline.dates
        assert artifact.quality == inline.quality

        # 读取端(repo _record_from_row 同构):LazySeriesValues 逐值等值
        values = read_series_values(tmp_path, meta.relpath, meta.checksum)
        assert values == dict(inline.values)
