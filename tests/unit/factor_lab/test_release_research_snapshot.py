"""联合发布 → 横截面因子快照全链路单元测试(issue #187)。

用内存 stub research source 发布 bars(主) + daily_metrics + financial_indicators
三份冻结 release,经 ``build_cross_section_feature_snapshot_from_releases`` 产出
pb / 市值 / 换手 / ROE 等因子观测。不依赖 PostgreSQL。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from finboard_backtest.factor_lab import (
    build_cross_section_feature_snapshot_from_releases,
)
from finboard_data.cache import ParquetCache
from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    FINANCIAL_INDICATORS_FIELDS,
    DatasetReleaseSpec,
    ExecutionMetadata,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
)
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import (
    AssetClass,
    BarPeriod,
    InstrumentType,
    ListingStatus,
    Market,
)

_START = date(2024, 1, 2)
_END = date(2024, 3, 31)


@dataclass
class _StubResearchSource:
    """内存 stub:按代码 + 日期范围返回研究记录。"""

    daily: dict[str, list[DailySecurityMetrics]] | None = None
    financial: dict[str, list[FinancialIndicator]] | None = None

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
                for item in (self.daily or {}).get(symbol, [])
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
        return {
            symbol: [
                item
                for item in (self.financial or {}).get(symbol, [])
                if start_date <= item.report_period <= end_date
            ]
            for symbol in symbols
        }


    async def income_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[IncomeStatement]]:
        return {symbol: [] for symbol in symbols}

    async def balance_sheets(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[BalanceSheet]]:
        return {symbol: [] for symbol in symbols}

    async def cashflow_statements(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[CashflowStatement]]:
        return {symbol: [] for symbol in symbols}

    async def dividends(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DividendRecord]]:
        return {symbol: [] for symbol in symbols}


def _stock(code: str) -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"stub-{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=datetime(2023, 1, 1, tzinfo=UTC),
        execution=ExecutionMetadata(
            lot_size=Decimal("100"),
            price_tick=Decimal("0.01"),
            settlement_days=1,
        ),
        list_date=date(2023, 1, 1),
        status=ListingStatus.ACTIVE,
    )


def _trade_days() -> list[date]:
    result: list[date] = []
    current = _START
    while current <= _END:
        if current.weekday() < 5:
            result.append(current)
        current += timedelta(days=1)
    return result


async def _publish_bars(builder: FrozenDatasetReleaseBuilder, *codes: str) -> str:
    cache_dir = Path(builder._cache_dir)
    cache = ParquetCache(cache_dir)
    for code in codes:
        symbol = Symbol(code, Market.A_SHARE)
        close = Decimal("10.0")
        bars = [
            Bar(
                symbol=symbol,
                period=BarPeriod.D1,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
                open=close,
                high=close,
                low=close,
                close=close,
                volume=Decimal("10000"),
                amount=Decimal("100000"),
                source="fixed_sample",
            )
            for day in _trade_days()
        ]
        await cache.write(symbol, BarPeriod.D1, "qfq", bars)
    spec = DatasetReleaseSpec(
        release_id="bars-join-v1",
        dataset_name="join_daily_bars",
        source="fixed_sample",
        version="v1",
        start_date=_START,
        end_date=_END,
        code_version="test",
        required_capabilities=("stock",),
    )
    release = await builder.publish(spec, [_stock(code) for code in codes])
    assert release.is_usable
    return release.release_id


async def _publish_research(
    builder: FrozenDatasetReleaseBuilder,
    *,
    kind: ReleaseDatasetKind,
    code: str,
    release_id: str,
    daily_records: dict[str, list[DailySecurityMetrics]] | None = None,
    financial_records: dict[str, list[FinancialIndicator]] | None = None,
) -> str:
    source = _StubResearchSource(daily=daily_records, financial=financial_records)
    # builder 用注入的 source;重新构造带 research_source 的 builder。
    sub = FrozenDatasetReleaseBuilder(
        cache_dir=Path(builder._cache_dir),
        release_root=Path(builder._release_root),
        research_source=source,
    )
    fields = (
        DAILY_METRICS_FIELDS
        if kind is ReleaseDatasetKind.DAILY_METRICS
        else FINANCIAL_INDICATORS_FIELDS
    )
    dataset_name = (
        "join_daily_metrics"
        if kind is ReleaseDatasetKind.DAILY_METRICS
        else "join_financial_indicators"
    )
    spec = DatasetReleaseSpec(
        release_id=release_id,
        dataset_name=dataset_name,
        source="tushare",
        version="v1",
        start_date=_START,
        end_date=_END,
        code_version="test",
        fields=fields,
        dataset_kind=kind,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await sub.publish(spec, [_stock(code)])
    assert release.is_usable
    return release.release_id


def _daily(code: str, trade_date: date, *, pb: Decimal) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=code,
        trade_date=trade_date,
        close=Decimal("10.0"),
        turnover_rate=Decimal("0.02"),
        turnover_rate_free=Decimal("0.018"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("10.0"),
        pe_ttm=Decimal("9.6"),
        pb=pb,
        ps=Decimal("1.5"),
        ps_ttm=Decimal("1.4"),
        dividend_yield=Decimal("0.03"),
        dividend_yield_ttm=Decimal("0.031"),
        total_shares=Decimal("1000000000"),
        float_shares=Decimal("800000000"),
        free_shares=Decimal("750000000"),
        total_market_cap=Decimal("10000000000"),
        circulating_market_cap=Decimal("8000000000"),
        limit_status=0,
        source="tushare",
        observed_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC),
        available_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC),
    )


def _financial(code: str, report_period: date) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=code,
        announcement_date=date(2024, 3, 29),
        report_period=report_period,
        update_flag="1",
        eps=Decimal("1.0"),
        diluted_eps=None,
        book_value_per_share=Decimal("10"),
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal("0.10"),
        weighted_return_on_equity=None,
        gross_profit_margin=Decimal("0.30"),
        net_profit_margin=Decimal("0.12"),
        debt_to_assets=Decimal("0.5"),
        revenue_yoy=Decimal("0.20"),
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
        available_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_joined_release_builds_fundamental_feature_snapshot(tmp_path: Path) -> None:
    code = "600001.SH"
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=None,
    )
    bars_id = await _publish_bars(builder, code)

    trades = [_daily(code, day, pb=Decimal("9.5")) for day in _trade_days()]
    daily_id = await _publish_research(
        builder,
        kind=ReleaseDatasetKind.DAILY_METRICS,
        code=code,
        release_id="daily-join-v1",
        daily_records={code: trades},
    )
    fina_id = await _publish_research(
        builder,
        kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        code=code,
        release_id="fina-join-v1",
        financial_records={code: [_financial(code, date(2024, 3, 31))]},
    )

    providers = {
        release_id: FrozenReleaseProvider(
            release_root=tmp_path / "releases",
            release_id=release_id,
        )
        for release_id in (bars_id, daily_id, fina_id)
    }
    releases = [providers[release_id].release for release_id in (bars_id, daily_id, fina_id)]
    decision_at = datetime(2024, 3, 29, 16, 0, tzinfo=UTC)
    snapshot = await build_cross_section_feature_snapshot_from_releases(
        releases=releases,
        providers=providers,
        decision_at=decision_at,
        code_version="test",
        momentum_lookback=5,
        volatility_windows=(20,),
    )

    observed = {obs.feature_name: obs for obs in snapshot.observations}
    # 基本面因子来自 daily_metrics / financial_indicators 联合发布。
    assert observed["pb"].symbol == code
    assert observed["pb"].value == 9.5
    assert observed["market_cap"].value == 10_000_000_000.0
    assert observed["turnover_rate"].value == 0.02
    assert observed["roe"].value == 0.10
    assert observed["gross_profit_margin"].value == 0.30
    # 价格因子来自 bars 主发布。
    assert observed["momentum"].symbol == code
    assert observed["volatility_20d"].symbol == code


@pytest.mark.asyncio
async def test_joined_release_requires_exactly_one_bars(tmp_path: Path) -> None:
    """缺少 bars 主发布时 fail-closed。"""
    from finboard_backtest.factor_lab import FactorAnalysisError

    code = "600001.SH"
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=None,
    )
    trades = [_daily(code, day, pb=Decimal("9.5")) for day in _trade_days()]
    daily_id = await _publish_research(
        builder,
        kind=ReleaseDatasetKind.DAILY_METRICS,
        code=code,
        release_id="daily-no-bars",
        daily_records={code: trades},
    )
    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id=daily_id,
    )
    with pytest.raises(FactorAnalysisError, match="bars 主发布"):
        await build_cross_section_feature_snapshot_from_releases(
            releases=[provider.release],
            providers={daily_id: provider},
            decision_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
            code_version="test",
        )


@pytest.mark.asyncio
async def test_joined_release_tolerates_symbol_missing_in_research_release(
    tmp_path: Path,
) -> None:
    """#212:bars 标的不在附加研究发布 → 基本面因子为 null + issues 计数。

    全市场实测 bars 5534 与 financial 5533 标的集有差(次新股无财报等),
    旧逻辑对差集 fail-closed(误判为「回退外部数据源」),导致任何真实
    全市场联合快照整体不可计算。价格因子不受影响。
    """
    full = "600001.SH"
    sparse = "600002.SH"
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=None,
    )
    bars_id = await _publish_bars(builder, full, sparse)
    trades = [_daily(full, day, pb=Decimal("9.5")) for day in _trade_days()]
    daily_id = await _publish_research(
        builder,
        kind=ReleaseDatasetKind.DAILY_METRICS,
        code=full,
        release_id="daily-missing-v1",
        daily_records={full: trades},
    )
    fina_id = await _publish_research(
        builder,
        kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        code=full,
        release_id="fina-missing-v1",
        financial_records={full: [_financial(full, date(2024, 3, 31))]},
    )

    providers = {
        release_id: FrozenReleaseProvider(
            release_root=tmp_path / "releases",
            release_id=release_id,
        )
        for release_id in (bars_id, daily_id, fina_id)
    }
    releases = [providers[release_id].release for release_id in (bars_id, daily_id, fina_id)]
    snapshot = await build_cross_section_feature_snapshot_from_releases(
        releases=releases,
        providers=providers,
        decision_at=datetime(2024, 3, 29, 16, 0, tzinfo=UTC),
        code_version="test",
        momentum_lookback=5,
        volatility_windows=(20,),
    )

    # 缺失可见:两个研究发布各缺 sparse 一只。
    assert "missing_in_research_release:daily_metrics:1" in snapshot.issues
    assert "missing_in_research_release:financial_indicators:1" in snapshot.issues
    # 基本面因子只覆盖 full;价格因子两只都有。
    observed: dict[str, set[str]] = {}
    for obs in snapshot.observations:
        observed.setdefault(obs.feature_name, set()).add(obs.symbol)
    assert observed["roe"] == {full}
    assert observed["pb"] == {full}
    assert observed["momentum"] == {full, sparse}
