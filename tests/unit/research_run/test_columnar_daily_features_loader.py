"""daily_metrics 观测装配的列式优先等值(issue #371 同构消费)。

``_load_daily_metrics_features`` 的对象路径(``fetch_daily_metrics``)把
「发布起点→决策日」全部历史行逐行 dict→Decimal 强转成 ``DailySecurityMetrics``
后仅取 ``available_at`` 最新一条,全市场发布 x 多期下 99.9% 强转被扔掉、
纯 Python 单线程成为加载瓶颈。loader 对 provider 做 getattr 探测:有
``fetch_daily_metrics_columns``(PIT/区间门控在 Arrow 内完成)就走列式,
只物化每标的最新一条可见行;无该属性(测试 stub 等)回退对象路径。

本文件用真实 ``FrozenDatasetReleaseBuilder`` 冻结的 daily_metrics 发布
(#300 列式等值同夹具风格)锁定两条路径的逐值等值:等值 / tie-break /
PIT 边界 / 回退 / #252 容忍。纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_backtest.research_run.contracts import FeatureValue, UniverseCandidate
from finboard_backtest.research_run.frozen_loader import (
    _load_daily_metrics_features,
)
from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    DatasetReleaseSpec,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
    ResearchDatasetRelease,
    default_execution_metadata,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
)
from finboard_shared.models import Symbol
from finboard_shared.types import AssetClass, InstrumentType, Market

_RELEASE_ID = "daily-col-eq-r1"
_CODES = ("600001.SH", "000002.SZ")


def _daily(
    symbol: str,
    trade_date: date,
    *,
    available_at: datetime,
    pb: str,
) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=trade_date,
        close=Decimal("12.0"),
        turnover_rate=Decimal("0.02"),
        turnover_rate_free=Decimal("0.015"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("10.0"),
        pe_ttm=Decimal("9.5"),
        pb=Decimal(pb),
        ps=Decimal("2.0"),
        ps_ttm=Decimal("1.9"),
        dividend_yield=Decimal("0.03"),
        dividend_yield_ttm=Decimal("0.031"),
        total_shares=Decimal("100000000"),
        float_shares=Decimal("80000000"),
        free_shares=Decimal("70000000"),
        total_market_cap=Decimal("1200000000"),
        circulating_market_cap=Decimal("960000000"),
        limit_status=0,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )


class _StubDailySource:
    """daily_metrics 注入源 stub(``ResearchDataReleaseSource`` 结构性满足)。

    其余数据集方法按协议占位返回空(本文件只发布 daily_metrics)。
    """

    def __init__(self, records: dict[str, list[DailySecurityMetrics]]) -> None:
        self._records = records

    async def daily_metrics(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DailySecurityMetrics]]:
        return {
            symbol: [
                item
                for item in self._records.get(symbol, [])
                if start_date <= item.trade_date <= end_date
            ]
            for symbol in symbols
        }

    async def financial_indicators(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[FinancialIndicator]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def income_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[IncomeStatement]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def balance_sheets(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[BalanceSheet]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def cashflow_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[CashflowStatement]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def dividends(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DividendRecord]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}


def _instrument(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"样本{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2024, 1, 1, tzinfo=UTC),
        execution=default_execution_metadata(
            market=Market.A_SHARE,
            instrument_type=InstrumentType.STOCK,
        ),
        list_date=date(2020, 1, 1),
    )


async def _publish_release(
    tmp_path: Path,
    records: dict[str, list[DailySecurityMetrics]],
) -> FrozenReleaseProvider:
    """用真实发布构建器冻结一份 daily_metrics 发布(#300 夹具同款)。"""
    release_root = tmp_path / "releases"
    codes = list(records)
    await FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=release_root,
        research_source=_StubDailySource(records),
    ).publish(
        DatasetReleaseSpec(
            release_id=_RELEASE_ID,
            dataset_name="columnar_daily_features_equivalence",
            source="tushare",
            version="v1",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            code_version="deadbeef",
            fields=DAILY_METRICS_FIELDS,
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
            required_capabilities=("stock",),
            adjustment="none",
        ),
        [_instrument(code) for code in codes],
    )
    return FrozenReleaseProvider(release_root=release_root, release_id=_RELEASE_ID)


@dataclass
class _ObjectPathOnlyProvider:
    """只暴露对象路径的 provider 包装(无 ``fetch_daily_metrics_columns`` 属性)。

    模拟测试 stub / 未实现列式方法的 provider:loader 的 getattr 探测必须
    回退对象路径;``object_calls`` 记录对象路径确实被走到。
    """

    inner: FrozenReleaseProvider
    object_calls: int = 0

    @property
    def release(self) -> ResearchDatasetRelease:
        return self.inner.release

    async def fetch_daily_metrics(
        self,
        symbol: Symbol,
        *,
        start: date,
        end: date,
        decision_at: datetime,
    ) -> list[DailySecurityMetrics]:
        self.object_calls += 1
        return await self.inner.fetch_daily_metrics(
            symbol,
            start=start,
            end=end,
            decision_at=decision_at,
        )


def _candidate(code: str) -> UniverseCandidate:
    return UniverseCandidate(
        symbol=code,
        included=True,
        reasons=("unit-test",),
        asset_class="equity",
        market=Market.A_SHARE.value,
    )


def _pb_values(values: Sequence[FeatureValue]) -> set[tuple[str, float | None]]:
    """提取 pb 特征的 (symbol, value) 集合。"""
    return {(item.symbol, item.value) for item in values if item.feature_id == "pb"}


@pytest.mark.asyncio
class TestColumnarDailyFeaturesEquivalence:
    """loader 列式路径 ↔ 对象路径逐值等值(getattr 探测两分支)。"""

    async def test_multi_symbol_multi_decision_dates_equivalent(
        self, tmp_path: Path
    ) -> None:
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="1.3",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 4),
                    available_at=datetime(2024, 1, 4, 15, 30, tzinfo=UTC),
                    pb="1.5",
                ),
            ],
            "000002.SZ": [
                _daily(
                    "000002.SZ",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="2.0",
                ),
                _daily(
                    "000002.SZ",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="2.2",
                ),
                _daily(
                    "000002.SZ",
                    date(2024, 1, 4),
                    available_at=datetime(2024, 1, 4, 15, 30, tzinfo=UTC),
                    pb="2.4",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate(code) for code in _CODES]
        wrapper = _ObjectPathOnlyProvider(provider)
        decision_points = [
            datetime(2024, 1, 1, 16, 0, tzinfo=UTC),  # 发布前:两路径都无观测
            datetime(2024, 1, 3, 16, 0, tzinfo=UTC),  # 中途
            datetime(2024, 1, 4, 16, 0, tzinfo=UTC),
            datetime(2024, 1, 5, 16, 0, tzinfo=UTC),  # 全量可见
        ]
        for decision_at in decision_points:
            columnar = await _load_daily_metrics_features(
                provider, candidates, decision_at, _RELEASE_ID
            )
            via_object = await _load_daily_metrics_features(
                wrapper, candidates, decision_at, _RELEASE_ID  # type: ignore[arg-type]
            )
            assert columnar == via_object
        # 对象路径确实被走到(每候选一次 x 决策日数)。
        assert wrapper.object_calls == len(_CODES) * len(decision_points)
        # 全量可见时观测非空、missing 为空,最新一条 = 各标的 1/4 行。
        values, missing = await _load_daily_metrics_features(
            provider, candidates, decision_points[-1], _RELEASE_ID
        )
        assert missing == ()
        assert values
        assert _pb_values(values) == {("600001.SH", 1.5), ("000002.SZ", 2.4)}
        # 发布前决策日:两路径同为空(空表 → None, False 语义)。
        empty, empty_missing = await _load_daily_metrics_features(
            provider, candidates, decision_points[0], _RELEASE_ID
        )
        assert empty == []
        assert empty_missing == ()

    async def test_tie_break_picks_last_in_original_order(self, tmp_path: Path) -> None:
        """available_at 并列:Python sorted 稳定 → 取原读取顺序最后一条。"""
        same_available = datetime(2024, 1, 3, 15, 30, tzinfo=UTC)
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=same_available,
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=same_available,
                    pb="2.2",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        # 前提锁定:发布行序 = 源顺序(builder 稳定排序不重排列行)。
        rows = await provider.fetch_daily_metrics(
            Symbol(code="600001.SH", market=Market.A_SHARE),
            start=date(2024, 1, 1),
            end=date(2024, 1, 31),
            decision_at=datetime(2024, 1, 6, 16, 0, tzinfo=UTC),
        )
        assert [item.trade_date for item in rows] == [date(2024, 1, 2), date(2024, 1, 3)]
        decision_at = datetime(2024, 1, 6, 16, 0, tzinfo=UTC)
        values, missing = await _load_daily_metrics_features(
            provider, [_candidate("600001.SH")], decision_at, _RELEASE_ID
        )
        assert missing == ()
        assert _pb_values(values) == {("600001.SH", 2.2)}
        wrapper = _ObjectPathOnlyProvider(provider)
        values_obj, _ = await _load_daily_metrics_features(
            wrapper,  # type: ignore[arg-type]
            [_candidate("600001.SH")],
            decision_at,
            _RELEASE_ID,
        )
        assert _pb_values(values_obj) == {("600001.SH", 2.2)}

    async def test_pit_boundary_available_at_equals_decision_at_visible(
        self, tmp_path: Path
    ) -> None:
        """available_at == decision_at 两路径均可见;早 1 微秒均不可见。"""
        boundary = datetime(2024, 1, 3, 15, 30, tzinfo=UTC)
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.1",
                ),
                _daily("600001.SH", date(2024, 1, 3), available_at=boundary, pb="1.3"),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate("600001.SH")]
        wrapper = _ObjectPathOnlyProvider(provider)

        at_boundary = await _load_daily_metrics_features(
            provider, candidates, boundary, _RELEASE_ID
        )
        at_boundary_obj = await _load_daily_metrics_features(
            wrapper, candidates, boundary, _RELEASE_ID  # type: ignore[arg-type]
        )
        assert at_boundary == at_boundary_obj
        assert _pb_values(at_boundary[0]) == {("600001.SH", 1.3)}

        before = await _load_daily_metrics_features(
            provider, candidates, boundary - timedelta(microseconds=1), _RELEASE_ID
        )
        before_obj = await _load_daily_metrics_features(
            wrapper,  # type: ignore[arg-type]
            candidates,
            boundary - timedelta(microseconds=1),
            _RELEASE_ID,
        )
        assert before == before_obj
        assert _pb_values(before[0]) == {("600001.SH", 1.1)}

    async def test_provider_without_columnar_attribute_uses_object_path(
        self, tmp_path: Path
    ) -> None:
        """provider 无 ``fetch_daily_metrics_columns`` 属性 → 走对象路径。

        既有 stub 测试(test_frozen_loader 等)不被破坏的保障。
        """
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="1.3",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        assert hasattr(provider, "fetch_daily_metrics_columns")
        wrapper = _ObjectPathOnlyProvider(provider)
        assert not hasattr(wrapper, "fetch_daily_metrics_columns")
        values, missing = await _load_daily_metrics_features(
            wrapper,  # type: ignore[arg-type]
            [_candidate("600001.SH")],
            datetime(2024, 1, 4, 16, 0, tzinfo=UTC),
            _RELEASE_ID,
        )
        assert missing == ()
        assert _pb_values(values) == {("600001.SH", 1.3)}
        assert wrapper.object_calls == 1

    async def test_columnar_capability_error_tolerated_as_missing(
        self, tmp_path: Path
    ) -> None:
        """#252 容忍:列式调用抛 ``ReleaseCapabilityError`` → (None, True)。

        真实 provider 的列式方法对不在发布中的标的在 ``instrument()`` 处抛
        ``ReleaseCapabilityError``;探测分支必须接住并计入 missing,不炸
        其余标的。
        """
        records = {
            "600001.SH": [
                _daily(
                    "600001.SH",
                    date(2024, 1, 2),
                    available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
                    pb="1.1",
                ),
                _daily(
                    "600001.SH",
                    date(2024, 1, 3),
                    available_at=datetime(2024, 1, 3, 15, 30, tzinfo=UTC),
                    pb="1.3",
                ),
            ],
        }
        provider = await _publish_release(tmp_path, records)
        candidates = [_candidate("600001.SH"), _candidate("000002.SZ")]
        values, missing = await _load_daily_metrics_features(
            provider, candidates, datetime(2024, 1, 4, 16, 0, tzinfo=UTC), _RELEASE_ID
        )
        assert missing == ("000002.SZ",)
        assert {item.symbol for item in values} == {"600001.SH"}
        assert _pb_values(values) == {("600001.SH", 1.3)}
