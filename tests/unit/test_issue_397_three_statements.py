"""issue #397 单测:三表 + dividend 的 provider 解析 / 质量门 / 发布白名单。

覆盖:
* tushare provider 四个 fetch 的字段解析、修订版本保留、PIT=ann_date+1、
  按 symbol 查询拒绝 skip 行级口径、标的漂移与截断护栏整批拒;
* 公告类研究数据(三表/dividend)批次质量门:重复键 / 报告期晚于公告日 /
  available_at 不晚于公告日(PIT 破坏)整批拒;
* 发布白名单:ReleaseDatasetKind 四 kind 的字段白名单分派、未知字段拒绝、
  executor 默认字段映射、REST schema release_kind 扩展。

纯离线:provider 注入 fake client,不连网;质量门纯函数。不连 broker / 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from finboard_data import DatasetReleaseSpec, ReleaseDatasetKind
from finboard_data.quality import QualityStatus, ResearchDataQualityValidator
from finboard_data.releases import (
    BALANCE_SHEETS_FIELDS,
    CASHFLOW_STATEMENTS_FIELDS,
    DIVIDENDS_FIELDS,
    INCOME_STATEMENTS_FIELDS,
    _fields_whitelist,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DividendRecord,
    IncomeStatement,
)
from finboard_data.tushare_provider import (
    _BALANCE_FIELDS,
    _CASHFLOW_FIELDS,
    _DIVIDEND_FIELDS,
    _INCOME_FIELDS,
    TushareResearchDataProvider,
)

pytestmark = pytest.mark.unit

_NOW = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)


def _provider(client: Any) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(client=client, now=lambda: _NOW)


class _FakeBudget:
    async def acquire(self) -> None:
        return None


class _FakeClient:
    """SDK 同形 fake:记录 kwargs,返回构造时注入的行。"""

    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self._rows = rows
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __getattr__(self, endpoint: str) -> Any:
        def _call(**kwargs: str) -> list[dict[str, object]]:
            self.calls.append((endpoint, dict(kwargs)))
            return list(self._rows.get(endpoint, []))

        return _call


def _income_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(_INCOME_FIELDS.split(","))
    row.update(
        {
            "ts_code": "600519.SH",
            "ann_date": "20240403",
            "f_ann_date": "20240403",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "update_flag": "0",
            "basic_eps": 59.49,
            "total_revenue": 150560330316.45,
            "n_income_attr_p": 74734071550.75,
            "ebitda": 103819869620.55,
        }
    )
    row.update(overrides)
    return row


def _balance_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(_BALANCE_FIELDS.split(","))
    row.update(
        {
            "ts_code": "600519.SH",
            "ann_date": "20240403",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "update_flag": "0",
            "money_cap": 69070136376.12,
            "inventories": 46435185061.53,
            "total_cur_assets": 225172517821.28,
            "total_cur_liab": 48697611501.2,
            "total_assets": 272699660092.25,
        }
    )
    row.update(overrides)
    return row


def _cashflow_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(_CASHFLOW_FIELDS.split(","))
    row.update(
        {
            "ts_code": "600519.SH",
            "ann_date": "20240403",
            "end_date": "20231231",
            "report_type": "1",
            "comp_type": "1",
            "update_flag": "1",
            "n_cashflow_act": 66593247721.09,
            "c_pay_acq_const_fiolta": 2619755888.79,
            "free_cashflow": 78667169645.9262,
        }
    )
    row.update(overrides)
    return row


def _dividend_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = dict.fromkeys(_DIVIDEND_FIELDS.split(","))
    row.update(
        {
            "ts_code": "600519.SH",
            "end_date": "20231231",
            "ann_date": "20240403",
            "div_proc": "实施",
            "stk_div": 0.0,
            "cash_div": 30.876,
            "cash_div_tax": 29.3322,
            "ex_date": "20240627",
            "imp_ann_date": "20240614",
        }
    )
    row.update(overrides)
    return row


class TestProviderParsing:
    @pytest.mark.anyio
    async def test_income_parse_keeps_revisions_and_pit(self) -> None:
        provider = _provider(
            _FakeClient(
                {
                    "income": [
                        _income_row(update_flag="0"),
                        _income_row(update_flag="1", basic_eps=59.6),
                    ]
                }
            )
        )
        records = await provider.fetch_income_statements(
            "600519.SH",
            start_announced=date(2024, 1, 1),
            end_announced=date(2024, 12, 31),
        )
        assert len(records) == 2
        first, revision = records
        assert first.symbol == "600519.SH"
        assert first.report_period == date(2023, 12, 31)
        assert first.announcement_date == date(2024, 4, 3)
        assert first.basic_eps == Decimal("59.49")
        assert first.total_revenue == Decimal("150560330316.45")
        assert first.ebitda == Decimal("103819869620.55")
        # 未声明字段(None)与修订版本并存。
        assert first.rd_exp is None
        assert revision.update_flag == "1"
        assert revision.basic_eps == Decimal("59.6")
        # PIT=ann_date+1 零点(上海)。
        assert first.available_at.isoformat() == "2024-04-04T00:00:00+08:00"
        # 调用形状:ts_code + fields + start_date/end_date(公告日窗)。
        endpoint, kwargs = provider._client.calls[0]  # type: ignore[attr-defined]
        assert endpoint == "income"
        assert kwargs["ts_code"] == "600519.SH"
        assert kwargs["start_date"] == "20240101"
        assert kwargs["end_date"] == "20241231"

    async def test_balance_and_cashflow_parse(self) -> None:
        provider = _provider(
            _FakeClient(
                {
                    "balancesheet": [_balance_row()],
                    "cashflow": [_cashflow_row()],
                }
            )
        )
        balances = await provider.fetch_balance_sheets(
            "600519.SH",
            start_announced=date(2024, 1, 1),
            end_announced=date(2024, 12, 31),
        )
        assert isinstance(balances[0], BalanceSheet)
        assert balances[0].total_cur_assets == Decimal("225172517821.28")
        assert balances[0].available_at.isoformat() == "2024-04-04T00:00:00+08:00"

        cashflows = await provider.fetch_cashflow_statements(
            "600519.SH",
            start_announced=date(2024, 1, 1),
            end_announced=date(2024, 12, 31),
        )
        assert isinstance(cashflows[0], CashflowStatement)
        assert cashflows[0].free_cashflow == Decimal("78667169645.9262")
        assert cashflows[0].c_pay_acq_const_fiolta == Decimal("2619755888.79")

    async def test_dividend_parse_keeps_progress_and_nan_to_none(self) -> None:
        provider = _provider(
            _FakeClient(
                {
                    "dividend": [
                        _dividend_row(),
                        _dividend_row(
                            div_proc="预案",
                            cash_div=float("nan"),
                            cash_div_tax=float("nan"),
                            ex_date=None,
                        ),
                    ]
                }
            )
        )
        records = await provider.fetch_dividends(
            "600519.SH",
            start_announced=date(2024, 1, 1),
            end_announced=date(2024, 12, 31),
        )
        assert [(r.div_proc, r.cash_div, r.ex_date) for r in records] == [
            ("实施", Decimal("30.876"), date(2024, 6, 27)),
            ("预案", None, None),
        ]
        assert records[0].imp_ann_date == date(2024, 6, 14)
        assert records[0].available_at.isoformat() == "2024-04-04T00:00:00+08:00"

    async def test_dividend_window_filtered_client_side(self) -> None:
        # 上游 dividend 接口没有 start_date/end_date 区间参数(2026-09 真实
        # API 实测:传了也被服务端忽略,返回全历史)→ 客户端按 ann_date 过滤,
        # 切片键与实际返回范围一致。
        provider = _provider(
            _FakeClient(
                {
                    "dividend": [
                        _dividend_row(),  # ann 2024-04-03,窗内
                        _dividend_row(
                            ann_date="20250403",
                            div_proc="实施",
                            cash_div=27.673,
                        ),  # 窗外(2025 公告)→ 过滤
                        _dividend_row(
                            ann_date="20221215",
                            div_proc="预案",
                        ),  # 窗外(2022 公告)→ 过滤
                    ]
                }
            )
        )
        records = await provider.fetch_dividends(
            "600519.SH",
            start_announced=date(2024, 1, 1),
            end_announced=date(2024, 12, 31),
        )
        assert [r.announcement_date for r in records] == [date(2024, 4, 3)]
        # 窗口参数不再透传给上游(服务端不支持,传了是噪音)。
        endpoint, kwargs = provider._client.calls[0]  # type: ignore[attr-defined]
        assert endpoint == "dividend"
        assert "start_date" not in kwargs
        assert "end_date" not in kwargs

    async def test_symbol_query_rejects_skip_policy(self) -> None:
        provider = _provider(_FakeClient({"income": []}))
        from finboard_data.research import ResearchDataConfigurationError

        for fetch in (
            provider.fetch_income_statements,
            provider.fetch_balance_sheets,
            provider.fetch_cashflow_statements,
            provider.fetch_dividends,
        ):
            with pytest.raises(ResearchDataConfigurationError, match="skip"):
                await fetch(
                    "600519.SH",
                    start_announced=date(2024, 1, 1),
                    end_announced=date(2024, 12, 31),
                    dirty_row_policy="skip",
                )

    async def test_symbol_drift_rejected(self) -> None:
        provider = _provider(
            _FakeClient({"income": [_income_row(ts_code="000858.SH")]})
        )
        from finboard_data.research import ResearchDataContractError

        with pytest.raises(ResearchDataContractError, match="标的之外"):
            await provider.fetch_income_statements(
                "600519.SH",
                start_announced=date(2024, 1, 1),
                end_announced=date(2024, 12, 31),
            )

    async def test_missing_identity_column_rejected(self) -> None:
        row = _income_row()
        del row["ann_date"]
        provider = _provider(_FakeClient({"income": [row]}))
        from finboard_data.research import ResearchDataContractError

        with pytest.raises(ResearchDataContractError, match="ann_date"):
            await provider.fetch_income_statements(
                "600519.SH",
                start_announced=date(2024, 1, 1),
                end_announced=date(2024, 12, 31),
            )

    async def test_truncation_guard(self) -> None:
        provider = _provider(
            _FakeClient({"dividend": [_dividend_row() for _ in range(1000)]})
        )
        from finboard_data.research import ResearchDataContractError

        with pytest.raises(ResearchDataContractError, match="截断"):
            await provider.fetch_dividends(
                "600519.SH",
                start_announced=date(2024, 1, 1),
                end_announced=date(2024, 12, 31),
            )

    async def test_window_inversion_rejected(self) -> None:
        provider = _provider(_FakeClient({}))
        from finboard_data.research import ResearchDataConfigurationError

        with pytest.raises(ResearchDataConfigurationError, match="晚于"):
            await provider.fetch_dividends(
                "600519.SH",
                start_announced=date(2024, 12, 31),
                end_announced=date(2024, 1, 1),
            )


def _income_record(**overrides: object) -> IncomeStatement:
    kwargs: dict[str, Any] = dict.fromkeys(INCOME_STATEMENTS_FIELDS)
    kwargs.update(
        {
            "symbol": "600519.SH",
            "announcement_date": date(2024, 4, 3),
            "report_period": date(2023, 12, 31),
            "formal_announcement_date": None,
            "report_type": "1",
            "comp_type": "1",
            "update_flag": "0",
            "total_revenue": Decimal("150560330316.45"),
            "source": "tushare",
            "observed_at": _NOW,
            "available_at": datetime(2024, 4, 4, 0, 0, tzinfo=UTC),
        }
    )
    kwargs.update(overrides)
    return IncomeStatement(**kwargs)


class TestQualityGate:
    def test_valid_batch_passes(self) -> None:
        validator = ResearchDataQualityValidator(now=_NOW)
        report = validator.validate_income_statements(
            [_income_record()], expected_source="tushare"
        )
        assert report.status is QualityStatus.PASSED
        assert report.publishable

        report = validator.validate_dividends(
            [
                DividendRecord(
                    symbol="600519.SH",
                    announcement_date=date(2024, 4, 3),
                    report_period=date(2023, 12, 31),
                    div_proc="实施",
                    stk_div=None,
                    stk_bo_rate=None,
                    stk_co_rate=None,
                    cash_div=Decimal("30.876"),
                    cash_div_tax=None,
                    record_date=None,
                    ex_date=date(2024, 6, 27),
                    pay_date=None,
                    div_listdate=None,
                    imp_ann_date=None,
                    source="tushare",
                    observed_at=_NOW,
                    available_at=datetime(2024, 4, 4, tzinfo=UTC),
                )
            ],
            expected_source="tushare",
        )
        assert report.status is QualityStatus.PASSED

    def test_period_after_announcement_rejected(self) -> None:
        validator = ResearchDataQualityValidator(now=_NOW)
        # 报告期晚于公告日(未公告先有报告)→ 整批拒。
        bad = _income_record(report_period=date(2024, 12, 31))
        report = validator.validate_income_statements(
            [bad], expected_source="tushare"
        )
        assert report.status is QualityStatus.FAILED
        assert any(issue.code == "invalid_financial_time" for issue in report.issues)

    def test_pit_violation_rejected(self) -> None:
        validator = ResearchDataQualityValidator(now=_NOW)
        # available_at == 公告日(PIT=ann_date+1 被破坏)→ 整批拒。
        bad = _income_record(
            available_at=datetime(2024, 4, 3, 0, 0, tzinfo=UTC),
        )
        report = validator.validate_income_statements(
            [bad], expected_source="tushare"
        )
        assert report.status is QualityStatus.FAILED
        assert any(issue.code == "invalid_financial_time" for issue in report.issues)

    def test_duplicate_identity_rejected(self) -> None:
        validator = ResearchDataQualityValidator(now=_NOW)
        record = _income_record()
        report = validator.validate_income_statements(
            [record, record], expected_source="tushare"
        )
        assert report.status is QualityStatus.FAILED
        assert any(issue.code == "duplicate_record" for issue in report.issues)

    def test_sparse_values_are_not_quality_issues(self) -> None:
        validator = ResearchDataQualityValidator(now=_NOW)
        # 全 None 值字段(保险/银行专用列在一般企业为空)不构成质量问题(#187)。
        report = validator.validate_income_statements(
            [_income_record()], expected_source="tushare"
        )
        assert report.status is QualityStatus.PASSED


class TestReleaseWhitelist:
    def test_fields_whitelist_dispatch(self) -> None:
        assert _fields_whitelist(ReleaseDatasetKind.INCOME_STATEMENTS) == frozenset(
            INCOME_STATEMENTS_FIELDS
        )
        assert _fields_whitelist(ReleaseDatasetKind.BALANCE_SHEETS) == frozenset(
            BALANCE_SHEETS_FIELDS
        )
        assert _fields_whitelist(ReleaseDatasetKind.CASHFLOW_STATEMENTS) == frozenset(
            CASHFLOW_STATEMENTS_FIELDS
        )
        assert _fields_whitelist(ReleaseDatasetKind.DIVIDENDS) == frozenset(
            DIVIDENDS_FIELDS
        )

    def test_whitelist_sizes_in_issue_range(self) -> None:
        # issue #397:三表各 30-50 个字段(含修订可见性列);
        # dividend 按上游接口实际列数(div_proc 进展维度,无 update_flag)。
        assert 30 <= len(set(INCOME_STATEMENTS_FIELDS)) <= 50
        assert 30 <= len(set(BALANCE_SHEETS_FIELDS)) <= 50
        assert 30 <= len(set(CASHFLOW_STATEMENTS_FIELDS)) <= 50
        assert len(set(DIVIDENDS_FIELDS)) == 13

    def test_spec_rejects_unknown_field(self) -> None:
        with pytest.raises(ValueError, match="未冻结字段"):
            DatasetReleaseSpec(
                release_id="unit-r397",
                dataset_name="income_statements",
                source="tushare",
                version="v1",
                start_date=date(2024, 1, 1),
                end_date=date(2024, 3, 31),
                code_version="test",
                adjustment="none",
                fields=("total_revenue", "nope_field"),
                dataset_kind=ReleaseDatasetKind.INCOME_STATEMENTS,
            )

    def test_spec_accepts_subset(self) -> None:
        spec = DatasetReleaseSpec(
            release_id="unit-r397",
            dataset_name="income_statements",
            source="tushare",
            version="v1",
            start_date=date(2024, 1, 1),
            end_date=date(2024, 3, 31),
            code_version="test",
            adjustment="none",
            fields=("total_revenue", "n_income_attr_p", "ebitda"),
            dataset_kind=ReleaseDatasetKind.INCOME_STATEMENTS,
        )
        assert spec.fields == ("total_revenue", "n_income_attr_p", "ebitda")

    def test_executor_default_fields(self) -> None:
        from finboard_backtest.background_jobs.executors.dataset_publish import (
            _RELEASE_KIND_TO_DATASET_KIND,
            _RELEASE_KIND_TO_SOURCE,
            _default_release_fields,
        )

        for release_kind, kind_value in (
            ("income_statements", "income_statements"),
            ("balance_sheets", "balance_sheets"),
            ("cashflow_statements", "cashflow_statements"),
            ("dividends", "dividends"),
        ):
            assert _RELEASE_KIND_TO_DATASET_KIND[release_kind] == kind_value
            assert _RELEASE_KIND_TO_SOURCE[release_kind] == "tushare"
            fields = _default_release_fields(kind_value)
            assert set(fields) == _fields_whitelist(ReleaseDatasetKind(kind_value))

    def test_rest_schema_accepts_new_kinds(self) -> None:
        from finboard_api.schemas import ResearchDatasetReleaseCreate

        for release_kind in (
            "income_statements",
            "balance_sheets",
            "cashflow_statements",
            "dividends",
        ):
            body = ResearchDatasetReleaseCreate(
                release_id="unit-r397",
                dataset_name=release_kind,
                release_kind=release_kind,
                version="v1",
                symbols=["600519.SH"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 3, 31),
            )
            assert body.release_kind == release_kind
            # source 缺省按 kind 归一为 tushare 的校验只拒绝显式不一致。
            body = ResearchDatasetReleaseCreate(
                release_id="unit-r397",
                dataset_name=release_kind,
                release_kind=release_kind,
                source="tushare",
                version="v1",
                symbols=["600519.SH"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 3, 31),
            )
            assert body.source == "tushare"

    def test_rest_schema_rejects_source_mismatch(self) -> None:
        from finboard_api.schemas import ResearchDatasetReleaseCreate

        with pytest.raises(ValueError, match="数据来源"):
            ResearchDatasetReleaseCreate(
                release_id="unit-r397",
                dataset_name="income_statements",
                release_kind="income_statements",
                source="akshare",
                version="v1",
                symbols=["600519.SH"],
                start_date=date(2024, 1, 1),
                end_date=date(2024, 3, 31),
            )

    def test_research_dataset_enum_roundtrip(self) -> None:
        from finboard_persistence.research_repo import ResearchDataset

        for name in (
            "income_statements",
            "balance_sheets",
            "cashflow_statements",
            "dividends",
        ):
            assert ResearchDataset(name).value == name


class TestFourWaySchemaConsistency:
    """领域记录 ↔ 发布白名单 ↔ ORM 列 ↔ 迁移列 四方一致(#401 先例)。

    任何一处单独加列/改名都会在此具名失败,防四处白名单漂移。
    """

    _META_FIELDS = frozenset({"symbol", "source", "observed_at", "available_at"})
    _TABLES = (
        ("income_statements", IncomeStatement, INCOME_STATEMENTS_FIELDS),
        ("balance_sheets", BalanceSheet, BALANCE_SHEETS_FIELDS),
        ("cashflow_statements", CashflowStatement, CASHFLOW_STATEMENTS_FIELDS),
        ("dividends", DividendRecord, DIVIDENDS_FIELDS),
    )

    def test_dataclass_matches_whitelist(self) -> None:
        import dataclasses

        for _, record_type, whitelist in self._TABLES:
            fields = {f.name for f in dataclasses.fields(record_type)}
            assert fields == set(whitelist) | self._META_FIELDS, record_type

    def test_model_columns_cover_whitelist(self) -> None:
        from finboard_persistence.models import (
            ResearchBalanceSheetModel,
            ResearchCashflowStatementModel,
            ResearchDividendModel,
            ResearchIncomeStatementModel,
        )

        models = {
            "income_statements": ResearchIncomeStatementModel,
            "balance_sheets": ResearchBalanceSheetModel,
            "cashflow_statements": ResearchCashflowStatementModel,
            "dividends": ResearchDividendModel,
        }
        for table, _, whitelist in self._TABLES:
            columns = {c.name for c in models[table].__table__.columns}
            missing = [name for name in whitelist if name not in columns]
            assert not missing, f"{table} 缺列: {missing}"

    def test_migration_columns_match_models(self) -> None:
        import re
        from pathlib import Path

        from finboard_persistence.models import (
            ResearchBalanceSheetModel,
            ResearchCashflowStatementModel,
            ResearchDividendModel,
            ResearchIncomeStatementModel,
        )

        migration = (
            Path(__file__).resolve().parents[2]
            / "migrations"
            / "versions"
            / "c159fcd63f94_three_statements_dividends_397.py"
        )
        source = migration.read_text(encoding="utf-8")
        models = {
            "research_income_statements": ResearchIncomeStatementModel,
            "research_balance_sheets": ResearchBalanceSheetModel,
            "research_cashflow_statements": ResearchCashflowStatementModel,
            "research_dividends": ResearchDividendModel,
        }
        for table, model in models.items():
            block = source[source.index(f'"{table}"') :]
            block = block[: block.index("op.create_index")]
            declared = set(
                re.findall(r'sa\.Column\(\s*"?([a-z0-9_]+)"?', block)
            )
            columns = {c.name for c in model.__table__.columns}
            assert declared == columns, f"{table} 迁移与 ORM 列不一致"
            down_block = source[source.index("def downgrade") :]
            assert f'drop_table("{table}")' in down_block, table
