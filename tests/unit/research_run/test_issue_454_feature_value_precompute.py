"""issue #454 层 2:每期 FeatureValue 对象税——共享实例等值压缩。

r11 py-spy 采样与 job timing:加载段主进程事件循环线程上纯 Python 逐期构造
~15k 个 ``FeatureValue``(价格特征)/ 最多 ~36k 个(研究观测 7 因子),75 期
累计百万级对象构造;研究观测路径还存在逐 (因子, 标的) 重扫全部 records 求
最新 ``available_at`` 的 O(因子数 x 标的数²) 扫描。本文件锁定:

* :func:`_period_feature_values` 输出与实现前逐字段相等;
  ``source_artifact_ids`` 全期共享同一元组实例;值相等且时区表示一致的
  ``available_at`` 共享同一 datetime 实例(键含 tzinfo,跨时区同瞬间不互换,
  时区表示不漂移);
* :func:`_matrix_to_feature_values` 输出与实现前逐字段相等;每标的最新
  ``available_at`` 只计算一次(O(n²)→O(n)),同标的跨因子共享同一实例;
* ``FeatureValue`` 契约不变:外部直接构造仍逐实例走 ``__post_init__`` 校验。
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from finboard_backtest.research_run.contracts import FeatureValue
from finboard_backtest.research_run.frozen_loader import (
    _latest_available_at,
    _matrix_to_feature_values,
)
from finboard_backtest.research_run.signal_engine import _period_feature_values
from finboard_data.factors import FactorInputBatch, FactorInputRecord
from finboard_data.research import DailySecurityMetrics

_RELEASE_ID = "feature-precompute-r1"
_CST = ZoneInfo("Asia/Shanghai")


# ---- 参照实现(实现前语义的逐字拷贝,等值对照基线)--------------------------


def _reference_period_feature_values(
    snapshot: object,
    release_id: str,
) -> tuple[FeatureValue, ...]:
    return tuple(
        FeatureValue(
            symbol=observation.symbol,  # type: ignore[attr-defined]
            feature_id=observation.feature_name,  # type: ignore[attr-defined]
            value=observation.value,  # type: ignore[attr-defined]
            source_artifact_ids=(release_id,),
            available_at=observation.available_at,  # type: ignore[attr-defined]
        )
        for observation in snapshot.observations  # type: ignore[attr-defined]
    )


def _reference_matrix_to_feature_values(
    batch: FactorInputBatch,
    *,
    release_id: str,
) -> list[FeatureValue]:
    from finboard_backtest.factors.extract import extract_factor_matrix

    matrix = extract_factor_matrix(batch)
    values: list[FeatureValue] = []
    for factor_name, by_symbol in sorted(matrix.items()):
        for symbol, value in sorted(by_symbol.items()):
            values.append(
                FeatureValue(
                    symbol=symbol,
                    feature_id=factor_name,
                    value=float(value),
                    source_artifact_ids=(release_id,),
                    available_at=_latest_available_at(batch, symbol),
                )
            )
    return values


# ---- 测试样本 ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Observation:
    symbol: str
    feature_name: str
    value: float
    available_at: datetime


@dataclass(frozen=True, slots=True)
class _Snapshot:
    observations: tuple[_Observation, ...]


def _daily_metrics(
    symbol: str,
    *,
    available_at: datetime,
    pb: str | None = "2.5",
    turnover: str | None = "0.01",
    pe_ttm: str | None = "12.5",
    dividend: str | None = "0.02",
    market_cap: str | None = "1000000000",
) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=date(2024, 3, 1),
        close=Decimal("50.00"),
        turnover_rate=None if turnover is None else Decimal(turnover),
        turnover_rate_free=None,
        volume_ratio=None,
        pe=None,
        pe_ttm=None if pe_ttm is None else Decimal(pe_ttm),
        pb=None if pb is None else Decimal(pb),
        ps=None,
        ps_ttm=None,
        dividend_yield=None,
        dividend_yield_ttm=None if dividend is None else Decimal(dividend),
        total_shares=None,
        float_shares=None,
        free_shares=None,
        total_market_cap=None if market_cap is None else Decimal(market_cap),
        circulating_market_cap=None,
        limit_status=None,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )


def _sample_observations() -> tuple[_Observation, ...]:
    at_early = datetime(2024, 1, 5, 7, 30, tzinfo=UTC)
    at_late = datetime(2024, 3, 1, 15, 30, tzinfo=_CST)
    # 同一瞬间的 UTC 与 +08:00 表示(值相等、tzinfo 不同):不得互换实例。
    at_late_as_utc = datetime(2024, 3, 1, 7, 30, tzinfo=UTC)
    return (
        _Observation("600519.SH", "momentum", 0.05, at_late),
        _Observation("600519.SH", "volatility_20d", 0.21, at_late),
        _Observation("000001.SZ", "momentum", -0.03, at_early),
        _Observation("000001.SZ", "volatility_20d", 0.35, at_late_as_utc),
        _Observation("600036.SH", "momentum", 0.11, at_late_as_utc),
    )


# ---- _period_feature_values ------------------------------------------------


class TestPeriodFeatureValuesEquivalence:
    def test_fieldwise_equals_reference(self) -> None:
        snapshot = _Snapshot(observations=_sample_observations())
        result = _period_feature_values(snapshot, _RELEASE_ID)
        reference = _reference_period_feature_values(snapshot, _RELEASE_ID)
        assert len(result) == len(reference)
        for actual, expected in zip(result, reference, strict=True):
            assert actual.symbol == expected.symbol
            assert actual.feature_id == expected.feature_id
            assert actual.value == expected.value
            assert actual.source_artifact_ids == expected.source_artifact_ids
            assert actual.available_at == expected.available_at
            assert actual == expected

    def test_source_artifact_ids_shared_single_instance(self) -> None:
        snapshot = _Snapshot(observations=_sample_observations())
        result = _period_feature_values(snapshot, _RELEASE_ID)
        assert len({id(item.source_artifact_ids) for item in result}) == 1

    def test_equal_available_at_shares_instance_same_tz(self) -> None:
        snapshot = _Snapshot(observations=_sample_observations())
        result = _period_feature_values(snapshot, _RELEASE_ID)
        by_tz_value: dict[tuple[object, datetime], list[datetime]] = {}
        for item in result:
            key = (item.available_at.tzinfo, item.available_at)
            by_tz_value.setdefault(key, []).append(item.available_at)
        # 同一 (tzinfo, 值) 的 available_at 全部是同一实例。
        for instances in by_tz_value.values():
            assert len({id(item) for item in instances}) == 1

    def test_same_instant_different_tz_not_interchanged(self) -> None:
        """跨时区同瞬间:值相等但 tzinfo 表示不同,不互换实例。"""
        snapshot = _Snapshot(observations=_sample_observations())
        result = _period_feature_values(snapshot, _RELEASE_ID)
        late_cst = next(
            item.available_at
            for item in result
            if item.symbol == "600519.SH" and item.feature_id == "momentum"
        )
        late_utc = next(
            item.available_at
            for item in result
            if item.symbol == "600036.SH"
        )
        assert late_cst == late_utc  # 同一瞬间
        assert late_cst.tzinfo is _CST  # 表示保持原样
        assert late_utc.tzinfo is UTC
        assert late_cst is not late_utc

    def test_empty_observations(self) -> None:
        assert _period_feature_values(_Snapshot(observations=()), _RELEASE_ID) == ()


# ---- _matrix_to_feature_values ---------------------------------------------


def _batch() -> FactorInputBatch:
    at_first = datetime(2024, 1, 10, 15, 30, tzinfo=_CST)
    at_latest = datetime(2024, 3, 1, 15, 30, tzinfo=_CST)
    records = (
        # 同标的两个时点的观测:矩阵取后值,available_at 取最新。
        FactorInputRecord(
            symbol="600519.SH",
            profile=None,
            daily=_daily_metrics("600519.SH", available_at=at_first, pb="3.0"),
            financial=None,
            industry=None,
        ),
        FactorInputRecord(
            symbol="600519.SH",
            profile=None,
            daily=_daily_metrics("600519.SH", available_at=at_latest, pb="2.5"),
            financial=None,
            industry=None,
        ),
        # 部分字段为 None(提取器按 None 跳过)。
        FactorInputRecord(
            symbol="000001.SZ",
            profile=None,
            daily=_daily_metrics(
                "000001.SZ",
                available_at=at_latest,
                pb=None,
                turnover=None,
                dividend=None,
                market_cap=None,
            ),
            financial=None,
            industry=None,
        ),
        FactorInputRecord(
            symbol="600036.SH",
            profile=None,
            daily=_daily_metrics("600036.SH", available_at=at_first),
            financial=None,
            industry=None,
        ),
    )
    return FactorInputBatch(
        records=records,
        source="tushare",
        dataset_versions={"research_release": "frozen"},
    )


class TestMatrixToFeatureValuesEquivalence:
    def test_fieldwise_equals_reference(self) -> None:
        batch = _batch()
        result = _matrix_to_feature_values(batch, release_id=_RELEASE_ID)
        reference = _reference_matrix_to_feature_values(batch, release_id=_RELEASE_ID)
        assert result == reference
        assert len(result) > 0

    def test_artifact_ids_shared_and_available_at_shared_per_symbol(self) -> None:
        batch = _batch()
        result = _matrix_to_feature_values(batch, release_id=_RELEASE_ID)
        assert len({id(item.source_artifact_ids) for item in result}) == 1
        # 同一标的跨因子共享同一 available_at 实例。
        by_symbol: dict[str, list[datetime]] = {}
        for item in result:
            by_symbol.setdefault(item.symbol, []).append(item.available_at)
        for instances in by_symbol.values():
            assert len({id(item) for item in instances}) == 1

    def test_latest_available_at_selection_unchanged(self) -> None:
        """同标的多次观测:取最新 available_at(与逐对查询同值)。"""
        batch = _batch()
        result = _matrix_to_feature_values(batch, release_id=_RELEASE_ID)
        latest = max(
            record.daily.available_at
            for record in batch.records
            if record.symbol == "600519.SH" and record.daily is not None
        )
        for item in result:
            if item.symbol == "600519.SH":
                assert item.available_at == latest

    def test_empty_batch(self) -> None:
        empty = FactorInputBatch(records=(), source="tushare", dataset_versions={})
        assert _matrix_to_feature_values(empty, release_id=_RELEASE_ID) == []


# ---- FeatureValue 契约不变 --------------------------------------------------


class TestFeatureValueContractUnchanged:
    def test_direct_construction_still_validates(self) -> None:
        at = datetime(2024, 3, 1, 15, 30, tzinfo=_CST)
        with pytest.raises(ValueError, match="时区"):
            FeatureValue(
                symbol="600519.SH",
                feature_id="pb",
                value=1.0,
                source_artifact_ids=(_RELEASE_ID,),
                available_at=datetime(2024, 3, 1, 15, 30),
            )
        with pytest.raises(ValueError, match="有限"):
            FeatureValue(
                symbol="600519.SH",
                feature_id="pb",
                value=float("nan"),
                source_artifact_ids=(_RELEASE_ID,),
                available_at=at,
            )
        with pytest.raises(ValueError, match="冻结来源"):
            FeatureValue(
                symbol="600519.SH",
                feature_id="pb",
                value=1.0,
                source_artifact_ids=(),
                available_at=at,
            )
        value = FeatureValue(
            symbol="600519.SH",
            feature_id="pb",
            value=None,
            source_artifact_ids=(_RELEASE_ID,),
            available_at=at,
        )
        assert dataclasses.fields(value)[0].name == "symbol"
