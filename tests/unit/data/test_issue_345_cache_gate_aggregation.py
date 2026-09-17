"""bars 冻结缓存工件门聚合报告(issue #345)单元测试。

``FrozenDatasetReleaseBuilder.publish`` 的冻结循环此前对缓存 parquet 缺失
/ 读取失败「首错即拒」:多只标的缺缓存时每次只暴露一只,操作者只能
「修一个换一个」。本组测试锁定聚合语义:

* 多只标的 ``source_artifact_missing`` / ``source_read_failed`` 一次性
  聚合抛出,错误文本含全部标的与各自原因;
* 单只失败保持既有单错消息逐字不变(单失败语义零变化);
* 聚合只覆盖缓存工件门两类逐标的错误,其他冻结错误原样上抛;
* 失败路径的暂存区清理语义不变(不留残缺 staging 目录)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_data import (
    DatasetReleaseQualityError,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    ReleaseInstrumentSpec,
    default_execution_metadata,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import _aggregate_cache_gate_errors, _FreezeCacheGateError
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import AssetClass, BarPeriod, EtfCategory, InstrumentType, Market

_START = date(2024, 1, 2)
_END = date(2024, 1, 5)


def _bars(code: str) -> list[Bar]:
    symbol = Symbol(code=code, market=Market.A_SHARE)
    return [
        Bar(
            symbol=symbol,
            period=BarPeriod.D1,
            timestamp=datetime.combine(
                _START + timedelta(days=offset),
                datetime.min.time(),
                tzinfo=UTC,
            ),
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=Decimal("1000"),
            amount=Decimal("0"),
            source="fixed_sample",
        )
        for offset in range(4)
    ]


def _stock(code: str = "600519.SH") -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name="固定股票样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2001, 8, 27, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        exchange="SSE",
        list_date=date(2001, 8, 27),
    )


def _etf(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name="固定 ETF 样本",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.ETF,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2013, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.ETF,
            etf_category=EtfCategory.INDEX,
        ),
        exchange="SSE",
        etf_category=EtfCategory.INDEX,
        list_date=date(2013, 1, 1),
    )


def _spec(release_id: str = "fixed-r345") -> DatasetReleaseSpec:
    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="multi_asset_daily_bars",
        source="fixed_sample",
        version="2024.01",
        start_date=_START,
        end_date=_END,
        code_version="issue345",
    )


async def _seed(cache_dir: Path, codes: list[str]) -> None:
    cache = ParquetCache(cache_dir)
    for code in codes:
        await cache.write(
            Symbol(code=code, market=Market.A_SHARE),
            BarPeriod.D1,
            "qfq",
            _bars(code),
        )


@pytest.mark.asyncio
async def test_four_missing_caches_reported_in_one_error(tmp_path: Path) -> None:
    """4 只标的缓存工件全部缺失:一次抛出,文本含全部标的与原因。"""
    instruments = [
        _stock(),
        _etf("510300.SH"),
        _etf("513100.SH"),
        _etf("518880.SH"),
    ]

    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(_spec(), instruments)

    text = str(exc_info.value)
    assert "以下 4 个标的缓存工件缺失或读取失败,禁止发布:" in text
    for item in instruments:
        assert f"{item.code}:source_artifact_missing" in text
    # 失败路径暂存区清理语义不变:不留残缺 staging 目录。
    assert not (tmp_path / "releases" / "fixed-r345").exists()
    assert not list((tmp_path / "releases").glob(".fixed-r345-*"))


@pytest.mark.asyncio
async def test_missing_and_read_failed_artifacts_aggregated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缺缓存与读取失败两类错误混合同样聚合,各自原因可见。"""
    instruments = [
        _stock("600519.SH"),
        _etf("510300.SH"),
        _etf("513100.SH"),
        _etf("518880.SH"),
    ]
    # 600519.SH 缓存在但读取失败;513100.SH/518880.SH 缓存缺失。
    await _seed(tmp_path / "cache", ["600519.SH", "510300.SH"])
    original_read = ParquetCache.read

    async def failing_read(self: ParquetCache, *args: object, **kwargs: object) -> object:
        assert isinstance(args[0], Symbol)
        if args[0].code == "600519.SH":
            raise ConnectionError("simulated rate limit/network interruption")
        return await original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ParquetCache, "read", failing_read)

    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(_spec(), instruments)

    text = str(exc_info.value)
    assert "以下 3 个标的缓存工件缺失或读取失败,禁止发布:" in text
    assert "600519.SH:source_read_failed:ConnectionError" in text
    assert "513100.SH:source_artifact_missing" in text
    assert "518880.SH:source_artifact_missing" in text


@pytest.mark.asyncio
async def test_single_missing_artifact_keeps_original_message(tmp_path: Path) -> None:
    """单只失败保持既有单错消息逐字不变(单失败语义零变化)。"""
    instruments = [_stock(), _etf("510300.SH")]
    await _seed(tmp_path / "cache", ["510300.SH"])

    with pytest.raises(DatasetReleaseQualityError) as exc_info:
        await FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / "cache",
            release_root=tmp_path / "releases",
        ).publish(_spec(), instruments)

    assert str(exc_info.value) == "600519.SH:source_artifact_missing"


def test_aggregate_helper_passthrough_single_gate_error() -> None:
    """仅单只缓存工件失败:原样返回首个异常,不做聚合包装。"""
    first = _FreezeCacheGateError("600519.SH:source_artifact_missing")
    result = _aggregate_cache_gate_errors(first, [first])
    assert result is first


def test_aggregate_helper_passthrough_non_gate_first_error() -> None:
    """首个异常属其他冻结阶段(非缓存工件门):原样上抛,不误收聚合。"""
    other = DatasetReleaseQualityError("600519.SH:no_bars_in_release_range")
    gate_a = _FreezeCacheGateError("510300.SH:source_artifact_missing")
    gate_b = _FreezeCacheGateError("513100.SH:source_read_failed:OSError")
    result = _aggregate_cache_gate_errors(other, [other, gate_a, gate_b])
    assert result is other


def test_aggregate_helper_lists_all_codes_in_sorted_order() -> None:
    """聚合错误按冻结输入(代码排序)顺序列出全部标的,并保留首错为 cause。"""
    gate_a = _FreezeCacheGateError("510300.SH:source_artifact_missing")
    gate_b = _FreezeCacheGateError("513100.SH:source_read_failed:OSError")
    first = _FreezeCacheGateError("518880.SH:source_artifact_missing")
    result = _aggregate_cache_gate_errors(first, [gate_a, gate_b, first])
    assert isinstance(result, DatasetReleaseQualityError)
    assert result is not first
    assert str(result) == (
        "以下 3 个标的缓存工件缺失或读取失败,禁止发布: "
        "510300.SH:source_artifact_missing; "
        "513100.SH:source_read_failed:OSError; "
        "518880.SH:source_artifact_missing"
    )
    assert result.__cause__ is first
