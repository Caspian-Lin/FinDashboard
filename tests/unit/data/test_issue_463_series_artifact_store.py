"""factor_series parquet 工件存储单测(issue #463)。

覆盖:写读往返逐值等值(显式 null / 标的缺行)/ 确定性(同内容同
checksum)/ sha256 校验失败与文件缺失具名拒绝 / 相对路径防穿越 /
LazySeriesValues 按日惰性取数与全量物化等值 / repr 不触发读盘。
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from finboard_data.factor_series_store import (
    FactorSeriesArtifactError,
    LazySeriesValues,
    artifact_relpath_for,
    read_series_symbols,
    read_series_values,
    write_series_artifact,
)

_SERIES_KEY = "a" * 64


def _values() -> dict[str, dict[str, float | None]]:
    return {
        "2024-01-31": {"600000.SH": 1.5, "000001.SZ": None, "300750.SZ": -0.25},
        # 000001.SZ 该日缺行(与显式 None 区分)
        "2024-02-29": {"600000.SH": 2.0, "300750.SZ": 0.75},
    }


def _write(root: Path, values: dict[str, dict[str, float | None]] | None = None):
    return write_series_artifact(root, _SERIES_KEY, values or _values())


class TestWriteReadRoundTrip:
    def test_round_trip_value_equal(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        assert meta.relpath == artifact_relpath_for(_SERIES_KEY)
        assert meta.relpath == f"{_SERIES_KEY[:2]}/{_SERIES_KEY}.parquet"
        path = tmp_path / meta.relpath
        assert path.is_file()
        assert meta.byte_size == path.stat().st_size
        assert meta.row_count == 5
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert meta.checksum == digest

        values = read_series_values(tmp_path, meta.relpath, meta.checksum)
        assert values == _values()

    def test_symbols_projection(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        assert read_series_symbols(tmp_path, meta.relpath, meta.checksum) == [
            "000001.SZ",
            "300750.SZ",
            "600000.SH",
        ]

    def test_deterministic_bytes(self, tmp_path: Path) -> None:
        first = _write(tmp_path)
        second = _write(tmp_path)
        assert first.checksum == second.checksum
        assert first.relpath == second.relpath

    def test_date_object_keys_accepted(self, tmp_path: Path) -> None:
        values = {date(2024, 1, 31): {"600000.SH": 1.0}}
        meta = write_series_artifact(tmp_path, _SERIES_KEY, values)
        assert read_series_values(tmp_path, meta.relpath, meta.checksum) == {
            "2024-01-31": {"600000.SH": 1.0}
        }

    def test_empty_values_writes_empty_table(self, tmp_path: Path) -> None:
        meta = write_series_artifact(tmp_path, _SERIES_KEY, {})
        assert meta.row_count == 0
        assert read_series_values(tmp_path, meta.relpath, meta.checksum) == {}


class TestIntegrity:
    def test_tampered_file_rejected(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        path = tmp_path / meta.relpath
        path.write_bytes(path.read_bytes() + b"x")
        with pytest.raises(FactorSeriesArtifactError, match="sha256 校验失败"):
            read_series_values(tmp_path, meta.relpath, meta.checksum)

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FactorSeriesArtifactError, match="缺失"):
            read_series_values(tmp_path, artifact_relpath_for(_SERIES_KEY), "d" * 64)

    def test_empty_checksum_rejected(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        with pytest.raises(FactorSeriesArtifactError, match="缺少 content_checksum"):
            read_series_values(tmp_path, meta.relpath, "")

    def test_relpath_traversal_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(FactorSeriesArtifactError, match="相对路径非法"):
            read_series_values(tmp_path, "../escape.parquet", "d" * 64)


class TestLazySeriesValues:
    def test_per_day_get_value_equal(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        lazy = LazySeriesValues(tmp_path, meta.relpath, meta.checksum)
        assert lazy.get("2024-01-31") == _values()["2024-01-31"]
        assert lazy["2024-02-29"] == _values()["2024-02-29"]
        # 缺日语义与 dict.get 一致:None(default),缺行标的自然不在映射里
        assert lazy.get("2024-03-15") is None
        with pytest.raises(KeyError):
            lazy["2024-03-15"]
        assert "2024-02-29" in lazy
        assert "2024-03-15" not in lazy
        assert len(lazy) == 2

    def test_full_iteration_equals_roundtrip(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)
        lazy = LazySeriesValues(tmp_path, meta.relpath, meta.checksum)
        assert dict(lazy.items()) == _values()
        assert list(lazy.values()) == [_values()["2024-01-31"], _values()["2024-02-29"]]
        assert list(iter(lazy)) == ["2024-01-31", "2024-02-29"]

    def test_compact_arrays_replace_arrow_table(self, tmp_path: Path) -> None:
        """首访后紧凑 numpy 底座就位,arrow 表不再常驻(#470 后半场)。"""
        meta = _write(tmp_path)
        lazy = LazySeriesValues(tmp_path, meta.relpath, meta.checksum)
        assert "loaded=False" in repr(lazy)
        assert lazy.get("2024-01-31") == _values()["2024-01-31"]
        # 首访后:紧凑数组形态加载完成(arrow 表已丢弃,不再有 _table 属性),
        # 逐值语义与整表物化逐值等值(显式 None 保留、缺行标的区分)。
        assert "loaded=True" in repr(lazy)
        assert lazy._loaded is True
        assert lazy._dates is not None
        assert lazy._symbol_codes.shape == lazy._dates.shape
        assert lazy._value_values.shape == lazy._dates.shape
        assert dict(lazy.items()) == read_series_values(
            tmp_path, meta.relpath, meta.checksum
        )
        # 显式 null 单元 → None(不与数值 NaN 混同)。
        assert lazy["2024-01-31"]["000001.SZ"] is None

    def test_repr_does_not_read_file(self, tmp_path: Path) -> None:
        # 校验和故意错误:repr 若触发读盘会抛错,以此锁定 repr 零 IO
        lazy = LazySeriesValues(tmp_path, artifact_relpath_for(_SERIES_KEY), "d" * 64)
        assert "loaded=False" in repr(lazy)

    def test_bad_checksum_surfaces_on_access(self, tmp_path: Path) -> None:
        meta = _write(tmp_path)  # 文件存在,校验和故意给错
        lazy: Any = LazySeriesValues(tmp_path, meta.relpath, "d" * 64)
        with pytest.raises(FactorSeriesArtifactError, match="sha256 校验失败"):
            lazy.get("2024-01-31")
