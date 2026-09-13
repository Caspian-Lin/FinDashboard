"""数据预览端点(只读)的单元测试:缓存预览 + 冻结发布预览。

数据页可观测性:GET /api/data/cache/preview 与
GET /api/instruments/datasets/releases/{id}/preview。
纯读路径;pyarrow 尾部行采样,不走 Bar 对象构造。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException


def _fake_session() -> Any:
    """handlers 对 session 零依赖(repo 已被 monkeypatch),MagicMock 足够。"""

    from unittest.mock import MagicMock

    return MagicMock()


def _write_sample_parquet(path: Any) -> None:
    """写一个 5 行的小 parquet(含 Decimal/时间/NaN 列)。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    table = pa.table(
        {
            "timestamp": pa.array(
                [datetime(2024, 1, i, tzinfo=UTC) for i in range(1, 6)]
            ),
            "close": pa.array(
                [Decimal("1.10"), Decimal("1.20"), None, Decimal("1.40"), Decimal("1.50")],
                type=pa.decimal128(18, 4),
            ),
            "volume": pa.array([100.0, 200.0, float("nan"), 400.0, 500.0]),
        }
    )
    pq.write_table(table, path)


class TestValidatePreviewSymbol:
    def test_accepts_exchange_suffixed_code(self) -> None:
        from finboard_api._preview import validate_preview_symbol

        assert validate_preview_symbol("510300.SH") == "510300.SH"
        assert validate_preview_symbol("if2506.cffex") == "IF2506.CFFEX"

    def test_rejects_path_traversal(self) -> None:
        from finboard_api._preview import validate_preview_symbol

        with pytest.raises(ValueError, match="非法标的代码"):
            validate_preview_symbol("../secrets")
        with pytest.raises(ValueError, match="非法标的代码"):
            validate_preview_symbol("a/b")


class TestReadParquetTail:
    def test_tail_rows_and_jsonify(self, tmp_path: Any) -> None:
        from finboard_api._preview import read_parquet_tail

        artifact = tmp_path / "sample.parquet"
        _write_sample_parquet(artifact)

        columns, rows, total = read_parquet_tail(artifact, 2)
        assert columns == ["timestamp", "close", "volume"]
        assert total == 5
        assert len(rows) == 2
        assert rows[0]["timestamp"] == "2024-01-04T00:00:00+00:00"
        assert rows[0]["close"] == "1.4000"
        assert rows[0]["volume"] == 400.0
        # NaN → None(最后一行)
        assert rows[-1]["close"] == "1.5000"

    def test_limit_clamped_to_total(self, tmp_path: Any) -> None:
        from finboard_api._preview import read_parquet_tail

        artifact = tmp_path / "sample.parquet"
        _write_sample_parquet(artifact)

        _columns, rows, total = read_parquet_tail(artifact, 500)
        assert total == 5
        assert len(rows) == 5


class TestCachePreviewEndpoint:
    @pytest.mark.asyncio
    async def test_preview_tail_bars(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_api.routes import data as data_module

        _write_sample_parquet(tmp_path / "510300.SH_1d_qfq.parquet")
        monkeypatch.setattr(data_module, "_CACHE_DIR", str(tmp_path))

        result = await data_module.preview_cache_bars(symbol="510300.SH", limit=3, adjust="qfq")
        assert result.label.startswith("510300.SH")
        assert result.total_rows == 5
        assert len(result.rows) == 3
        assert result.truncated is True
        assert result.artifact == "510300.SH_1d_qfq.parquet"

    @pytest.mark.asyncio
    async def test_missing_cache_file_404(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_api.routes import data as data_module

        monkeypatch.setattr(data_module, "_CACHE_DIR", str(tmp_path))
        with pytest.raises(HTTPException) as exc_info:
            await data_module.preview_cache_bars(symbol="000001.SZ", limit=20)
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_invalid_symbol_422(self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        from finboard_api.routes import data as data_module

        monkeypatch.setattr(data_module, "_CACHE_DIR", str(tmp_path))
        with pytest.raises(HTTPException) as exc_info:
            await data_module.preview_cache_bars(symbol="../../etc", limit=20)
        assert exc_info.value.status_code == 422


def _fake_release(tmp_path: Any) -> SimpleNamespace:
    """发布桩:一只标的,artifact 指向 tmp_path 下的真实文件。"""

    instrument = SimpleNamespace(
        code="510300.SH",
        artifact_path="bars/510300.SH.parquet",
        artifact_checksum="0" * 64,
        ready=True,
        issues=(),
    )
    return SimpleNamespace(
        release_id="ut-release-v1",
        dataset_name="multi_asset_mixed",
        version="v3",
        instruments=(instrument,),
    )


class TestReleasePreviewEndpoint:
    @pytest.mark.asyncio
    async def test_preview_release_symbol(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from finboard_api.routes import instruments as instruments_module
        from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository

        release_root = tmp_path / "releases"
        artifact = release_root / "ut-release-v1" / "bars" / "510300.SH.parquet"
        _write_sample_parquet(artifact)
        monkeypatch.setenv("FINBOARD_DATA_RELEASE_ROOT", str(release_root))

        async def fake_get(self: Any, release_id: str) -> Any:
            return _fake_release(tmp_path)

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", fake_get)

        result = await instruments_module.preview_dataset_release(
            release_id="ut-release-v1",
            symbol=None,
            limit=2,
            session=_fake_session(),
        )
        # 默认取第一只标的
        assert "510300.SH" in result.label
        assert result.total_rows == 5
        assert len(result.rows) == 2
        assert result.truncated is True
        assert result.artifact == "bars/510300.SH.parquet"

    @pytest.mark.asyncio
    async def test_release_not_found_404(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes import instruments as instruments_module
        from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository

        monkeypatch.setenv("FINBOARD_DATA_RELEASE_ROOT", str(tmp_path))

        async def fake_get(self: Any, release_id: str) -> None:
            return None

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", fake_get)

        with pytest.raises(HTTPException) as exc_info:
            await instruments_module.preview_dataset_release(
                release_id="missing",
                symbol=None,
                limit=20,
                session=_fake_session(),
            )
        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_symbol_not_in_release_422(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes import instruments as instruments_module
        from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository

        monkeypatch.setenv("FINBOARD_DATA_RELEASE_ROOT", str(tmp_path))

        async def fake_get(self: Any, release_id: str) -> Any:
            return _fake_release(tmp_path)

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", fake_get)

        with pytest.raises(HTTPException) as exc_info:
            await instruments_module.preview_dataset_release(
                release_id="ut-release-v1",
                symbol="000001.SZ",
                limit=20,
                session=_fake_session(),
            )
        assert exc_info.value.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_artifact_404(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi import HTTPException

        from finboard_api.routes import instruments as instruments_module
        from finboard_persistence.dataset_release_repo import ResearchDatasetReleaseRepository

        # release_root 存在但发布目录下没有文件
        monkeypatch.setenv("FINBOARD_DATA_RELEASE_ROOT", str(tmp_path / "empty"))

        async def fake_get(self: Any, release_id: str) -> Any:
            return _fake_release(tmp_path)

        monkeypatch.setattr(ResearchDatasetReleaseRepository, "get", fake_get)

        with pytest.raises(HTTPException) as exc_info:
            await instruments_module.preview_dataset_release(
                release_id="ut-release-v1",
                symbol="510300.SH",
                limit=20,
                session=_fake_session(),
            )
        assert exc_info.value.status_code == 404
