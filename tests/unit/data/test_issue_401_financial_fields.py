"""issue #401:fina_indicator 白名单扩展(批次 3)的一致性与语义单测。

* **三方一致性** —— provider 字段映射(``_FINANCIAL_FIELD_MAP``)↔ 发布
  白名单(``FINANCIAL_INDICATORS_FIELDS``)↔ 表列(``ResearchFinancialIndicatorModel``
  与迁移 #401)逐字段对齐;
* **单位语义** —— percent 类(同比/环比/利润率/回报率)÷100 归一小数,
  倍数/比率类(周转率/流动比率/产权比率/ICR/权益乘数/现金流比率)原值
  小数(2026-09 真实 API 抽样口径,样本见 issue 评论);
* **发布往返** —— 新字段冻结 → 读回逐值一致;旧发布行(无新键)读回
  新字段为 None(checksum 不漂移);字段变化 + schema_version 未递增
  被 #187 机制具名拒绝。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from finboard_data.releases import (
    FINANCIAL_INDICATORS_FIELDS,
    DatasetReleaseQualityError,
    DatasetReleaseSpec,
    ExecutionMetadata,
    FrozenDatasetReleaseBuilder,
    FrozenReleaseProvider,
    ReleaseDatasetKind,
    ReleaseInstrumentSpec,
    _financial_indicator_from_release_row,
    _validate_schema_compatibility,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
)
from finboard_data.tushare_provider import (
    _FINANCIAL_FIELD_MAP,
    TushareResearchDataProvider,
)
from finboard_shared.models import Symbol
from finboard_shared.types import AssetClass, InstrumentType, ListingStatus, Market

_AVAILABLE_AT = datetime(2024, 3, 15, 15, 30, tzinfo=UTC)

#: #401 之前(批次 2 及更早)的白名单——旧发布冻结字段集合。
_LEGACY_FIELDS: tuple[str, ...] = tuple(FINANCIAL_INDICATORS_FIELDS[:15])


# --------------------------------------------------------------------- #
# 三方一致性
# --------------------------------------------------------------------- #


def _model_financial_columns() -> set[str]:
    from finboard_persistence.models import ResearchFinancialIndicatorModel

    table = ResearchFinancialIndicatorModel.__table__
    meta = {"id", "batch_id", "source", "dataset_version", "symbol",
            "observed_at", "available_at", "ingested_at"}
    return {column.name for column in table.columns} - meta


def _migration_new_columns() -> tuple[str, ...]:
    import importlib.util
    from pathlib import Path

    path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "765d46a5f29e_financial_indicators_batch3_fields_401.py"
    )
    spec = importlib.util.spec_from_file_location("_mig_401", path)
    assert spec is not None
    assert spec.loader is not None
    module: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return tuple(module._NEW_COLUMNS)


class TestThreeWayConsistency:
    def test_map_covers_whitelist_exactly(self) -> None:
        """发布白名单(除日期/修订元键)↔ provider 映射的领域字段名逐一相等。"""
        whitelist_metrics = set(FINANCIAL_INDICATORS_FIELDS) - {
            "announcement_date",
            "report_period",
            "update_flag",
        }
        mapped = {name for _, name, _ in _FINANCIAL_FIELD_MAP}
        assert mapped == whitelist_metrics
        assert len(_FINANCIAL_FIELD_MAP) == len(mapped), "映射表领域字段名不得重复"

    def test_model_columns_match_whitelist(self) -> None:
        """表列(模型)↔ 白名单逐一相等(加列迁移落地后两者同构)。"""
        assert _model_financial_columns() == set(FINANCIAL_INDICATORS_FIELDS)

    def test_migration_new_columns_match_model_additions(self) -> None:
        """迁移 #401 的加列清单 ↔ 模型相对旧白名单的新增列逐一相等。"""
        added = set(FINANCIAL_INDICATORS_FIELDS) - set(_LEGACY_FIELDS)
        assert set(_migration_new_columns()) == added
        assert len(_migration_new_columns()) == 29

    def test_tushare_request_fields_follow_map(self) -> None:
        """provider 请求串 = 元键 + 映射表的 tushare 列名(单一事实源)。"""
        from finboard_data.tushare_provider import _FINANCIAL_FIELDS

        columns = _FINANCIAL_FIELDS.split(",")
        assert columns[:4] == ["ts_code", "ann_date", "end_date", "update_flag"]
        assert columns[4:] == [column for column, _, _ in _FINANCIAL_FIELD_MAP]

    def test_domain_dataclass_fields_match_map(self) -> None:
        """领域 dataclass 字段 ↔ 映射领域字段名逐一相等。"""
        dataclass_fields = {
            f.name
            for f in FinancialIndicator.__dataclass_fields__.values()
        } - {"symbol", "announcement_date", "report_period", "update_flag",
             "source", "observed_at", "available_at"}
        assert dataclass_fields == {name for _, name, _ in _FINANCIAL_FIELD_MAP}

    def test_whitelist_expanded_from_legacy(self) -> None:
        assert len(_LEGACY_FIELDS) == 15
        assert set(_LEGACY_FIELDS).issubset(set(FINANCIAL_INDICATORS_FIELDS))
        assert len(FINANCIAL_INDICATORS_FIELDS) == 44


# --------------------------------------------------------------------- #
# 单位语义(percent → 小数 / 倍率原值)
# --------------------------------------------------------------------- #


def _upstream_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "ts_code": "000001.SZ",
        "ann_date": "20260425",
        "end_date": "20260331",
        "update_flag": "1",
    }
    for column, _, kind in _FINANCIAL_FIELD_MAP:
        row[column] = 32.0 if kind == "percent" else 2.5
    row.update(overrides)
    return row


def _parse_row(row: dict[str, object]) -> FinancialIndicator:
    return TushareResearchDataProvider._parse_financial(
        row, 0, datetime(2026, 9, 9, tzinfo=UTC)
    )


class TestUnitSemantics:
    def test_percent_fields_divided_by_hundred(self) -> None:
        percent_fields = [
            name for _, name, kind in _FINANCIAL_FIELD_MAP if kind == "percent"
        ]
        assert set(percent_fields) >= {
            "revenue_yoy", "net_profit_yoy", "return_on_equity",
            "gross_profit_margin", "netprofit_margin_q", "roic",
            "revenue_qoq", "netprofit_yoy_q", "expense_to_revenue",
        }
        record = _parse_row(_upstream_row())
        for name in percent_fields:
            value = getattr(record, name)
            assert value == Decimal("0.32"), name

    def test_ratio_fields_kept_as_is(self) -> None:
        decimal_fields = [
            name for _, name, kind in _FINANCIAL_FIELD_MAP if kind == "decimal"
        ]
        # 真实 API 口径:以下字段上游是倍数/比率(不是百分数)
        assert set(decimal_fields) >= {
            "inventory_turnover", "total_assets_turnover", "current_ratio",
            "quick_ratio", "debt_to_equity", "interest_coverage",
            "equity_multiplier", "ocf_to_revenue", "ocf_to_debt",
        }
        record = _parse_row(_upstream_row())
        for name in decimal_fields:
            value = getattr(record, name)
            assert value == Decimal("2.5"), name

    def test_null_fields_stay_none(self) -> None:
        row = _upstream_row(**{column: None for column, _, _ in _FINANCIAL_FIELD_MAP})
        record = _parse_row(row)
        for _, name, _ in _FINANCIAL_FIELD_MAP:
            assert getattr(record, name) is None


# --------------------------------------------------------------------- #
# 发布往返与旧发布兼容
# --------------------------------------------------------------------- #


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


def _financial(symbol: str, report_period: date) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=symbol,
        announcement_date=date(report_period.year, 4, 30),
        report_period=report_period,
        update_flag="1",
        eps=Decimal("0.8"),
        diluted_eps=None,
        book_value_per_share=None,
        operating_cash_flow_per_share=None,
        return_on_equity=None,
        weighted_return_on_equity=None,
        gross_profit_margin=None,
        net_profit_margin=None,
        debt_to_assets=None,
        revenue_yoy=Decimal("0.15"),
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=_AVAILABLE_AT,
        available_at=datetime.combine(
            date(report_period.year, 4, 30), datetime.min.time(), tzinfo=UTC
        ),
        # #401 新字段(抽查代表)
        netprofit_qoq=Decimal("-0.654946"),
        return_on_assets=Decimal("0.3126"),
        roic=Decimal("0.351877"),
        total_assets_turnover=Decimal("0.6093"),
        current_ratio=Decimal("4.4541"),
        interest_coverage=None,
        equity_multiplier=Decimal("1.2353"),
        ocf_to_debt=Decimal("1.6241"),
    )


@dataclass
class _StubResearchSource:
    financial_records: dict[str, list[FinancialIndicator]]

    async def daily_metrics(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[DailySecurityMetrics]]:
        del start_date, end_date
        return {symbol: [] for symbol in symbols}

    async def financial_indicators(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[FinancialIndicator]]:
        records = self.financial_records
        return {
            symbol: [
                item
                for item in records.get(symbol, [])
                if start_date <= item.report_period <= end_date
            ]
            for symbol in symbols
        }

    # issue #402:补齐 Protocol 其余方法签名(mypy 结构化契约;
    # 本文件用例只走 financial_indicators 通道,其余恒空)。
    async def income_statements(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[IncomeStatement]]:
        del symbols, start_date, end_date
        return {}

    async def balance_sheets(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[BalanceSheet]]:
        del symbols, start_date, end_date
        return {}

    async def cashflow_statements(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[CashflowStatement]]:
        del symbols, start_date, end_date
        return {}

    async def dividends(
        self, *, symbols: Sequence[str], start_date: date, end_date: date
    ) -> dict[str, list[DividendRecord]]:
        del symbols, start_date, end_date
        return {}


def _spec(release_id: str, fields: tuple[str, ...], schema_version: str = "v1") -> DatasetReleaseSpec:
    return DatasetReleaseSpec(
        release_id=release_id,
        dataset_name="a_share_financial_indicators",
        source="tushare",
        version="v1",
        start_date=date(2024, 1, 1),
        end_date=date(2024, 12, 31),
        code_version="test",
        schema_version=schema_version,
        fields=fields,
        dataset_kind=ReleaseDatasetKind.FINANCIAL_INDICATORS,
        required_capabilities=("stock",),
        adjustment="none",
    )


@pytest.mark.asyncio
async def test_release_roundtrip_with_new_fields(tmp_path) -> None:
    """扩列后的发布:新字段冻结 → PIT 读回逐值一致。"""
    code = "600001.SH"
    source = _StubResearchSource(
        {code: [_financial(code, date(2024, 3, 31))]}
    )
    builder = FrozenDatasetReleaseBuilder(
        cache_dir=tmp_path / "cache",
        release_root=tmp_path / "releases",
        research_source=source,
    )
    spec = _spec("fina-401-v1", FINANCIAL_INDICATORS_FIELDS, schema_version="v2")
    release = await builder.publish(spec, [_stock(code)])
    assert release.schema_version == "v2"
    assert release.instrument(code).ready

    provider = FrozenReleaseProvider(
        release_root=tmp_path / "releases",
        release_id="fina-401-v1",
    )
    records = await provider.fetch_financial_indicators(
        Symbol(code, Market.A_SHARE),
        decision_at=datetime(2024, 12, 31, tzinfo=UTC),
    )
    assert len(records) == 1
    record = records[0]
    assert record.revenue_yoy == Decimal("0.15")
    assert record.netprofit_qoq == Decimal("-0.654946")
    assert record.return_on_assets == Decimal("0.3126")
    assert record.total_assets_turnover == Decimal("0.6093")
    assert record.current_ratio == Decimal("4.4541")
    assert record.equity_multiplier == Decimal("1.2353")
    assert record.ocf_to_debt == Decimal("1.6241")
    # 未声明值 → None(缺测语义)
    assert record.interest_coverage is None


def test_old_release_row_without_new_keys_reads_none() -> None:
    """旧发布(扩列前冻结)无新键:读回新字段 = None,旧路径零破坏。"""
    legacy_row: dict[str, object] = {
        "report_period": date(2024, 3, 31),
        "available_at": datetime(2024, 5, 1, tzinfo=UTC),
        "observed_at": datetime(2024, 5, 1, tzinfo=UTC),
        "source": "tushare",
        "announcement_date": date(2024, 4, 30),
        "update_flag": "1",
        "eps": "0.8",
        "return_on_equity": "0.08",
        "revenue_yoy": "0.15",
    }
    record = _financial_indicator_from_release_row(legacy_row, symbol="600001.SH")
    assert record.eps == Decimal("0.8")
    assert record.return_on_equity == Decimal("0.08")
    for _, name, _ in _FINANCIAL_FIELD_MAP:
        if name in {"eps", "return_on_equity", "revenue_yoy"}:
            continue
        assert getattr(record, name) is None, name


def test_old_release_parquet_checksum_unchanged_by_whitelist(tmp_path) -> None:
    """旧字段集合的发布产物在白名单扩展后重冻结内容一致(不漂移)。

    逐标的 artifact parquet 字节级一致;manifest 除 ``published_at``(墙钟)
    外逐键一致 —— 白名单扩展不改变「按旧 fields 冻结」的产物内容,既有
    发布不可变语义保持(旧发布不重冻结、checksum 不受影响)。
    """
    import asyncio
    import hashlib
    import json

    code = "600001.SH"
    source = _StubResearchSource(
        {code: [_financial(code, date(2024, 3, 31))]}
    )

    async def _publish(root_mark: str) -> tuple[bytes, dict[str, object]]:
        builder = FrozenDatasetReleaseBuilder(
            cache_dir=tmp_path / f"cache-{root_mark}",
            release_root=tmp_path / f"releases-{root_mark}",
            research_source=source,
        )
        await builder.publish(
            _spec("fina-legacy", _LEGACY_FIELDS),
            [_stock(code)],
        )
        root = tmp_path / f"releases-{root_mark}" / "fina-legacy"
        artifact = next(iter(root.rglob("600001.SH.parquet")))
        manifest = json.loads(
            (root / "manifest.json").read_text(encoding="utf-8")
        )
        return artifact.read_bytes(), manifest

    artifact_a, manifest_a = asyncio.run(_publish("a"))
    artifact_b, manifest_b = asyncio.run(_publish("b"))
    assert hashlib.sha256(artifact_a).hexdigest() == hashlib.sha256(
        artifact_b
    ).hexdigest()
    # published_at 是墙钟;release_checksum 是含 published_at 的派生值。
    for key in ("published_at", "release_checksum"):
        manifest_a.pop(key)
        manifest_b.pop(key)
    assert manifest_a == manifest_b


# --------------------------------------------------------------------- #
# schema_version 递增(#187 机制)
# --------------------------------------------------------------------- #


class TestSchemaVersionGate:
    def test_fields_change_without_bump_rejected(self) -> None:
        previous = SimpleNamespace(
            dataset_name="a_share_financial_indicators",
            source="tushare",
            schema_version="v1",
            fields=_LEGACY_FIELDS,
        )
        spec = _spec("fina-next", FINANCIAL_INDICATORS_FIELDS, schema_version="v1")
        with pytest.raises(DatasetReleaseQualityError, match="schema_version 未递增"):
            _validate_schema_compatibility(previous, spec)  # type: ignore[arg-type]

    def test_fields_change_with_bump_accepted(self) -> None:
        previous = SimpleNamespace(
            dataset_name="a_share_financial_indicators",
            source="tushare",
            schema_version="v1",
            fields=_LEGACY_FIELDS,
        )
        spec = _spec("fina-next", FINANCIAL_INDICATORS_FIELDS, schema_version="v2")
        _validate_schema_compatibility(previous, spec)  # type: ignore[arg-type]

    def test_same_fields_same_version_accepted(self) -> None:
        previous = SimpleNamespace(
            dataset_name="a_share_financial_indicators",
            source="tushare",
            schema_version="v1",
            fields=_LEGACY_FIELDS,
        )
        spec = _spec("fina-next", _LEGACY_FIELDS, schema_version="v1")
        _validate_schema_compatibility(previous, spec)  # type: ignore[arg-type]
