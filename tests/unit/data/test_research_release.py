"""研究数据发布(daily_metrics / financial_indicators)单元测试(issue #187)。

用内存 stub ``ResearchDataReleaseSource`` 验证非 bars 发布链路:字段白名单
校验、parquet 冻结、质量门、PIT 读取、schema_version 递增。不依赖 PostgreSQL
或远端数据源。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from finboard_data.releases import (
    DAILY_METRICS_FIELDS,
    FINANCIAL_INDICATORS_FIELDS,
    RELEASE_FIELDS,
    DatasetQualityStatus,
    DatasetReleaseQualityError,
    DatasetReleaseSpec,
    ExecutionMetadata,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ImmutableReleaseError,
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
from finboard_shared.models import Symbol
from finboard_shared.types import AssetClass, InstrumentType, ListingStatus, Market

_AVAILABLE_AT = datetime(2024, 3, 15, 15, 30, tzinfo=UTC)


def _stock(code: str = "600001.SH") -> ReleaseInstrumentSpec:
    return ReleaseInstrumentSpec(
        code=code,
        name=f"stub-{code}",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
        available_at=_AVAILABLE_AT,
        execution=ExecutionMetadata(
            lot_size=Decimal("100"),
            price_tick=Decimal("0.01"),
            settlement_days=1,
        ),
        list_date=date(2023, 1, 1),
        status=ListingStatus.ACTIVE,
    )


def _daily(symbol: str, trade_date: date, *, pb: Decimal = Decimal("1.1")) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=trade_date,
        close=Decimal("12.0"),
        turnover_rate=Decimal("0.02"),
        turnover_rate_free=Decimal("0.015"),
        volume_ratio=Decimal("1.2"),
        pe=Decimal("10.0"),
        pe_ttm=Decimal("9.5"),
        pb=pb,
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
        observed_at=_AVAILABLE_AT,
        available_at=datetime.combine(trade_date, datetime.min.time(), tzinfo=UTC),
    )


def _financial(symbol: str, report_period: date) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=symbol,
        announcement_date=date(report_period.year, 4, 30),
        report_period=report_period,
        update_flag="1",
        eps=Decimal("0.8"),
        diluted_eps=None,
        book_value_per_share=Decimal("12"),
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal("0.08"),
        weighted_return_on_equity=None,
        gross_profit_margin=Decimal("0.25"),
        net_profit_margin=Decimal("0.1"),
        debt_to_assets=Decimal("0.6"),
        revenue_yoy=Decimal("0.15"),
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=_AVAILABLE_AT,
        available_at=datetime.combine(
            date(report_period.year, 4, 30), datetime.min.time(), tzinfo=UTC
        ),
    )


@dataclass
class _StubResearchSource:
    """内存 stub:按日期范围返回研究记录。"""

    daily_records: dict[str, list[DailySecurityMetrics]] | None = None
    financial_records: dict[str, list[FinancialIndicator]] | None = None

    async def daily_metrics(
        self,
        *,
        symbols: Sequence[str],
        start_date: date,
        end_date: date,
    ) -> dict[str, list[DailySecurityMetrics]]:
        records = self.daily_records or {}
        return {
            symbol: [
                item
                for item in records.get(symbol, [])
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
        records = self.financial_records or {}
        return {
            symbol: [
                item
                for item in records.get(symbol, [])
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


@pytest.mark.asyncio
async def test_daily_metrics_release_freezes_and_reads_pit(tmp_path) -> None:
    code = "600001.SH"
    trades = [
        _daily(code, date(2024, 1, 2)),
        _daily(code, date(2024, 1, 3), pb=Decimal("1.3")),
        _daily(code, date(2024, 1, 4), pb=Decimal("1.5")),
    ]
    source = _StubResearchSource(daily_records={code: trades})
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = DatasetReleaseSpec(
        release_id="metrics-v1",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS,
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await builder.publish(spec, [_stock(code)])
    assert release.dataset_kind is ReleaseDatasetKind.DAILY_METRICS
    assert release.symbol_count == 1
    instrument = release.instrument(code)
    assert instrument.ready
    assert instrument.row_count == 3
    assert instrument.coverage_pct == 1

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id="metrics-v1",
    )
    # PIT:decision_at 在 1/3 收盘后,只能看到 1/2/1/3 两天的记录。
    decision_at = datetime(2024, 1, 3, 15, 30, tzinfo=UTC)
    records = await provider.fetch_daily_metrics(
        Symbol(code, Market.A_SHARE),
        start=date(2024, 1, 1),
        end=date(2024, 1, 31),
        decision_at=decision_at,
    )
    assert [item.trade_date for item in records] == [date(2024, 1, 2), date(2024, 1, 3)]
    assert records[-1].pb == Decimal("1.3")
    assert records[-1].total_market_cap == Decimal("1200000000")


@pytest.mark.asyncio
async def test_financial_indicators_release_freezes_and_reads_latest(tmp_path) -> None:
    code = "600001.SH"
    financials = [
        _financial(code, date(2024, 3, 31)),
        _financial(code, date(2024, 6, 30)),
    ]
    source = _StubResearchSource(financial_records={code: financials})
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = DatasetReleaseSpec(
        release_id="fina-v1",
        dataset_name="a_share_financial_indicators",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        code_version="test",
        fields=FINANCIAL_INDICATORS_FIELDS,
        dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await builder.publish(spec, [_stock(code)])
    instrument = release.instrument(code)
    assert instrument.ready
    assert instrument.row_count == 2
    assert instrument.coverage_pct == 1

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id="fina-v1",
    )
    records = await provider.fetch_financial_indicators(
        Symbol(code, Market.A_SHARE),
        decision_at=datetime(2024, 6, 30, 23, 59, tzinfo=UTC),
    )
    assert len(records) == 2
    assert records[-1].report_period == date(2024, 6, 30)
    assert records[-1].return_on_equity == Decimal("0.08")
    assert records[-1].gross_profit_margin == Decimal("0.25")


@pytest.mark.asyncio
async def test_financial_indicators_pit_gate_hides_unannounced_reports(tmp_path) -> None:
    """#212 PIT 回归:公告锚点 = 公告日次日 00:00(上海时区)。

    锚点由 ``TushareResearchDataProvider._parse_financial`` 锚定(ann_date+1);
    决策时点早于锚点时,该报告期(含修订)必须完全不可见。锚点若被改成
    报告期 end_date 或公告日当天 00:00,本用例会在边界断言处失败。
    """
    code = "600001.SH"
    anchor = datetime(2024, 5, 1, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    financial = replace(
        _financial(code, date(2024, 3, 31)),  # fixture 公告日 2024-04-30
        available_at=anchor,
    )
    source = _StubResearchSource(financial_records={code: [financial]})
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = DatasetReleaseSpec(
        release_id="fina-pit-v1",
        dataset_name="a_share_financial_indicators",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        code_version="test",
        fields=FINANCIAL_INDICATORS_FIELDS,
        dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await builder.publish(spec, [_stock(code)])
    assert release.instrument(code).row_count == 1

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id="fina-pit-v1",
    )
    symbol = Symbol(code, Market.A_SHARE)
    before = await provider.fetch_financial_indicators(
        symbol,
        decision_at=datetime(2024, 4, 30, 23, 59, tzinfo=ZoneInfo("Asia/Shanghai")),
    )
    assert before == []
    after = await provider.fetch_financial_indicators(symbol, decision_at=anchor)
    assert [item.report_period for item in after] == [date(2024, 3, 31)]


@pytest.mark.asyncio
async def test_research_release_rejects_bars_fields_and_missing_source(tmp_path) -> None:
    # 非 bars kind 配 bars fields → 字段白名单校验失败。
    with pytest.raises(ValueError, match="未冻结字段"):
        DatasetReleaseSpec(
            release_id="bad-v1",
            dataset_name="x",
            source="tushare",
            version="v1",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 31),
            code_version="test",
            fields=RELEASE_FIELDS,
            dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
            adjustment="none",
        )
    # 缺少研究数据源注入 → 发布 fail-closed。
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=None,
    )
    spec = DatasetReleaseSpec(
        release_id="metrics-no-source",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS,
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    with pytest.raises(DatasetReleaseQualityError, match="缺少研究数据源注入"):
        await builder.publish(spec, [_stock()])


@pytest.mark.asyncio
async def test_research_release_quality_gate_reports_null_fields(tmp_path) -> None:
    code = "600001.SH"
    # 全部 pb/市值 为 None → all_null_fields 出现在 issues,release 不可用。
    trades = [
        DailySecurityMetrics(
            symbol=code,
            trade_date=date(2024, 1, 2),
            close=Decimal("12.0"),
            turnover_rate=Decimal("0.02"),
            turnover_rate_free=None,
            volume_ratio=None,
            pe=None,
            pe_ttm=None,
            pb=None,
            ps=None,
            ps_ttm=None,
            dividend_yield=None,
            dividend_yield_ttm=None,
            total_shares=None,
            float_shares=None,
            free_shares=None,
            total_market_cap=None,
            circulating_market_cap=None,
            limit_status=None,
            source="tushare",
            observed_at=_AVAILABLE_AT,
            available_at=datetime(2024, 1, 2, 15, 30, tzinfo=UTC),
        ),
    ]
    source = _StubResearchSource(daily_records={code: trades})
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = DatasetReleaseSpec(
        release_id="metrics-null",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS,
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await builder.publish(spec, [_stock(code)])
    instrument = release.instrument(code)
    # 财务指标字段稀疏是常态:all_null_fields 是可见 warning,不阻止 ready。
    assert instrument.ready
    assert any("all_null_fields" in issue for issue in instrument.issues)


@pytest.mark.asyncio
async def test_research_release_sparse_coverage_is_warning_not_gate(tmp_path) -> None:
    """#212:研究数据逐标的 coverage 缺口只是可见 warning,不再阻止发布。

    全市场实测 5534 只中 1421 只跨度口径 coverage<0.98(停牌日 daily_basic
    无截面、最新报告期未公告是常态),逐标的 0.98 硬门会让任何真实全市场
    研究发布不可发布;发布级平均覆盖率(研究 kind 由 executor 默认放宽到
    0.95,此处显式传入对齐)仍是硬门。
    """
    full = "600001.SH"
    sparse = "600002.SH"
    quarters = [
        date(2021, 6, 30), date(2021, 9, 30), date(2021, 12, 31),
        date(2022, 3, 31), date(2022, 6, 30), date(2022, 9, 30), date(2022, 12, 31),
        date(2023, 3, 31), date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31),
        date(2024, 3, 31), date(2024, 6, 30), date(2024, 9, 30),
    ]

    def _fin(symbol: str, period: date) -> FinancialIndicator:
        # 审计的跨度回退按 available_at 日期取边界:公告日按报告期+25 天展开,
        # 与真实数据一致(_financial 默认全年挤在 4/30 会把跨度压扁)。
        ann = period + timedelta(days=25)
        return replace(
            _financial(symbol, period),
            announcement_date=ann,
            available_at=datetime.combine(ann, datetime.min.time(), tzinfo=UTC) + timedelta(days=1),
        )

    # list_date=None → 审计按记录自身跨度回退,expected=13 个季末。
    full_instrument = replace(_stock(full), list_date=None)
    sparse_instrument = replace(_stock(sparse), list_date=None)
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=_StubResearchSource(
            financial_records={
                full: [_fin(full, q) for q in quarters],
                # 缺 1 期内部报告期:12/13≈0.923,发布级均值≈0.962≥0.95。
                sparse: [_fin(sparse, q) for q in quarters if q != date(2023, 6, 30)],
            }
        ),
    )
    spec = DatasetReleaseSpec(
        release_id="fina-sparse-warn",
        dataset_name="a_share_financial_indicators",
        source="tushare",
        version="v1",
        start_date=date(2021, 1, 1),
        end_date=date(2024, 9, 30),
        code_version="test",
        fields=FINANCIAL_INDICATORS_FIELDS,
        dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        required_capabilities=("stock",),
        adjustment="none",
        minimum_release_coverage=Decimal("0.95"),
    )
    release = await builder.publish(spec, [full_instrument, sparse_instrument])
    sparse_out = release.instrument(sparse)
    # 元数据完整即 ready;coverage 缺口作为可见 warning 留在 issues。
    assert sparse_out.ready
    assert any("coverage:" in issue for issue in sparse_out.issues)
    assert release.quality_status is DatasetQualityStatus.WARNINGS

    # 发布级平均覆盖率跌破阈值仍硬失败:再缺 2 期 → 均值≈0.885<0.95。
    strict_builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache2",
        release_root=tmp_path / "releases2",
        research_source=_StubResearchSource(
            financial_records={
                full: [_fin(full, q) for q in quarters],
                sparse: [
                    _fin(sparse, q)
                    for q in quarters
                    if q not in (date(2023, 6, 30), date(2023, 9, 30), date(2023, 12, 31))
                ],
            }
        ),
    )
    with pytest.raises(DatasetReleaseQualityError, match="release_coverage"):
        await strict_builder.publish(
            replace(spec, release_id="fina-sparse-fail"),
            [full_instrument, sparse_instrument],
        )


@pytest.mark.asyncio
async def test_research_release_schema_version_must_increment(tmp_path) -> None:
    code = "600001.SH"
    source = _StubResearchSource(
        daily_records={
            code: [_daily(code, date(2024, 1, 2))],
        }
    )
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec_v1 = DatasetReleaseSpec(
        release_id="metrics-schema-v1",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS,
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    released_v1 = await builder.publish(spec_v1, [_stock(code)])

    # 同名 dataset 字段发生变化但 schema_version 未递增 → 拒绝。
    spec_v2 = DatasetReleaseSpec(
        release_id="metrics-schema-v2",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v2",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        # 故意少一个字段,模拟字段集合变化。
        fields=DAILY_METRICS_FIELDS[:-1],
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    with pytest.raises(DatasetReleaseQualityError, match="schema_version 未递增"):
        await builder.publish(spec_v2, [_stock(code)], previous_release=released_v1)


@pytest.mark.asyncio
async def test_research_release_identity_is_immutable(tmp_path) -> None:
    code = "600001.SH"
    source = _StubResearchSource(
        daily_records={code: [_daily(code, date(2024, 1, 2))]}
    )
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = DatasetReleaseSpec(
        release_id="metrics-identity",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS,
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    release = await builder.publish(spec, [_stock(code)])
    # 相同 id 相同规格 → 幂等返回。
    again = await builder.publish(spec, [_stock(code)])
    assert again == release
    # 相同 id 不同字段 → 拒绝覆盖。
    bad = DatasetReleaseSpec(
        release_id="metrics-identity",
        dataset_name="a_share_daily_metrics",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 31),
        code_version="test",
        fields=DAILY_METRICS_FIELDS[:-1],
        dataset_kind=ReleaseDatasetKind.DAILY_METRICS,
        required_capabilities=("stock",),
        adjustment="none",
    )
    with pytest.raises(ImmutableReleaseError, match="禁止覆盖"):
        await builder.publish(bad, [_stock(code)])
