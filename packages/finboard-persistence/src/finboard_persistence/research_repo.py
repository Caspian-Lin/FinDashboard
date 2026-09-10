"""时点化研究数据 Repository。

所有读取都先锁定一个已发布批次,再应用 ``available_at <= decision_at``。
这避免查询在不同来源或数据集版本之间静默拼接。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.factors import (
    FactorInputBatch,
    FactorInputRecord,
    FactorSelectionConfig,
    InputsMode,
)
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DailySecurityMetrics,
    DividendRecord,
    FinancialIndicator,
    IncomeStatement,
    IndustryMembership,
    InstrumentProfile,
    SuspensionRecord,
)
from finboard_persistence.models import (
    ResearchBalanceSheetModel,
    ResearchCashflowStatementModel,
    ResearchDailyMetricModel,
    ResearchDividendModel,
    ResearchFinancialIndicatorModel,
    ResearchIncomeStatementModel,
    ResearchIndustryClassificationModel,
    ResearchIndustryMembershipModel,
    ResearchInstrumentProfileModel,
    ResearchSuspensionModel,
    ResearchSyncBatchModel,
)

#: 三表 / dividend 表中在 ``_upsert_announced_rows`` 里**显式赋值**的列 ——
#: 值列拷贝按表列反射时排除(其余全部列名 = 领域记录同名属性,setattr 拷贝;
#: dividend 的 record_date/ex_date 等日期列走通用拷贝,不在此列)。
_STATEMENT_IDENTITY_COLUMNS = frozenset(
    {
        "id",
        "batch_id",
        "source",
        "dataset_version",
        "symbol",
        "announcement_date",
        "report_period",
        "formal_announcement_date",
        "report_type",
        "comp_type",
        "update_flag",
        "div_proc",
        "observed_at",
        "available_at",
        "ingested_at",
    }
)


class ResearchDataset(StrEnum):
    """受质量门保护的研究数据集。"""

    INSTRUMENT_PROFILES = "instrument_profiles"
    DAILY_METRICS = "daily_metrics"
    FINANCIAL_INDICATORS = "financial_indicators"
    INDUSTRY_MEMBERSHIPS = "industry_memberships"
    SUSPENSIONS = "suspensions"
    # issue #397:财务面扩展(三表 + dividend 分红明细)。
    INCOME_STATEMENTS = "income_statements"
    BALANCE_SHEETS = "balance_sheets"
    CASHFLOW_STATEMENTS = "cashflow_statements"
    DIVIDENDS = "dividends"


class SyncBatchStatus(StrEnum):
    """同步批次状态。"""

    RUNNING = "running"
    PUBLISHED = "published"
    PARTIAL = "partial"
    FAILED = "failed"


class ResearchSyncBatchRepository:
    """同步批次及发布指针仓储。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def prepare(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        expected_rows: int | None,
        received_rows: int,
    ) -> tuple[ResearchSyncBatchModel, bool]:
        """创建或重置批次;已发布版本保持不可变并直接返回。"""
        stmt = (
            select(ResearchSyncBatchModel)
            .where(
                ResearchSyncBatchModel.dataset == dataset.value,
                ResearchSyncBatchModel.source == source,
                ResearchSyncBatchModel.dataset_version == dataset_version,
            )
            .with_for_update()
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None and row.status == SyncBatchStatus.PUBLISHED.value:
            return row, True
        now = datetime.now(UTC)
        if row is None:
            row = ResearchSyncBatchModel(
                dataset=dataset.value,
                source=source,
                dataset_version=dataset_version,
                code_version=code_version,
                parameters=dict(parameters),
                raw_payload=raw_payload,
                status=SyncBatchStatus.RUNNING.value,
                expected_rows=expected_rows,
                received_rows=received_rows,
                accepted_rows=0,
                rejected_rows=0,
                started_at=now,
            )
            self._session.add(row)
        else:
            row.code_version = code_version
            row.parameters = dict(parameters)
            row.raw_payload = raw_payload
            row.status = SyncBatchStatus.RUNNING.value
            row.quality_status = None
            row.expected_rows = expected_rows
            row.received_rows = received_rows
            row.accepted_rows = 0
            row.rejected_rows = 0
            row.quality_report = None
            row.error_summary = None
            row.started_at = now
            row.completed_at = None
            row.published_at = None
        await self._session.flush()
        return row, False

    async def get(
        self,
        batch_id: int,
        *,
        for_update: bool = False,
    ) -> ResearchSyncBatchModel | None:
        """按主键读取批次。"""
        stmt = select(ResearchSyncBatchModel).where(ResearchSyncBatchModel.id == batch_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def mark_quality_rejected(
        self,
        batch_id: int,
        *,
        partial: bool,
        quality_status: str,
        quality_report: dict[str, object],
        rejected_rows: int,
    ) -> ResearchSyncBatchModel:
        """记录质量门拒绝,不产生可查询的数据集。"""
        row = await self._required(batch_id, for_update=True)
        row.status = SyncBatchStatus.PARTIAL.value if partial else SyncBatchStatus.FAILED.value
        row.quality_status = quality_status
        row.quality_report = quality_report
        row.accepted_rows = 0
        row.rejected_rows = rejected_rows
        row.completed_at = datetime.now(UTC)
        row.published_at = None
        await self._session.flush()
        return row

    async def mark_published(
        self,
        batch_id: int,
        *,
        accepted_rows: int,
        quality_status: str,
        quality_report: dict[str, object],
    ) -> ResearchSyncBatchModel:
        """原子发布已写入且通过质量门的数据集。"""
        row = await self._required(batch_id, for_update=True)
        now = datetime.now(UTC)
        row.status = SyncBatchStatus.PUBLISHED.value
        row.quality_status = quality_status
        row.quality_report = quality_report
        row.accepted_rows = accepted_rows
        row.rejected_rows = 0
        row.completed_at = now
        row.published_at = now
        row.error_summary = None
        await self._session.flush()
        return row

    async def mark_failed(
        self,
        batch_id: int,
        *,
        error_summary: str,
    ) -> ResearchSyncBatchModel:
        """记录同步/数据库异常;错误摘要不得包含上游凭据。"""
        row = await self._required(batch_id, for_update=True)
        row.status = SyncBatchStatus.FAILED.value
        row.error_summary = error_summary[:500]
        row.accepted_rows = 0
        row.rejected_rows = row.received_rows
        row.completed_at = datetime.now(UTC)
        row.published_at = None
        await self._session.flush()
        return row

    async def latest_published(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
    ) -> ResearchSyncBatchModel | None:
        """读取指定来源最近发布的完整数据集。"""
        stmt = (
            select(ResearchSyncBatchModel)
            .where(
                ResearchSyncBatchModel.dataset == dataset.value,
                ResearchSyncBatchModel.source == source,
                ResearchSyncBatchModel.status == SyncBatchStatus.PUBLISHED.value,
            )
            .order_by(
                ResearchSyncBatchModel.published_at.desc(),
                ResearchSyncBatchModel.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def published_version(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str,
    ) -> ResearchSyncBatchModel | None:
        """读取指定的已发布版本。"""
        stmt = select(ResearchSyncBatchModel).where(
            ResearchSyncBatchModel.dataset == dataset.value,
            ResearchSyncBatchModel.source == source,
            ResearchSyncBatchModel.dataset_version == dataset_version,
            ResearchSyncBatchModel.status == SyncBatchStatus.PUBLISHED.value,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _required(
        self,
        batch_id: int,
        *,
        for_update: bool,
    ) -> ResearchSyncBatchModel:
        row = await self.get(batch_id, for_update=for_update)
        if row is None:
            raise ValueError(f"研究数据同步批次 {batch_id} 不存在")
        return row


class ResearchDatasetRepository:
    """规范化研究数据写入与时点读取。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._batches = ResearchSyncBatchRepository(session)

    async def load_factor_inputs(
        self,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        source: str,
        required_datasets: frozenset[str],
        dataset_versions: dict[str, str],
    ) -> FactorInputBatch:
        """批量读取单一来源、单一发布版本且决策时点可见的因子输入。"""
        _require_aware_datetime(decision_at, "decision_at")
        batches: dict[str, ResearchSyncBatchModel] = {}
        issues: list[str] = []
        for dataset_name in sorted(required_datasets):
            try:
                dataset = ResearchDataset(dataset_name)
            except ValueError:
                issues.append(f"unknown_dataset:{dataset_name}")
                continue
            batch = await self._resolve_factor_batch(
                dataset=dataset,
                source=source,
                dataset_version=dataset_versions.get(dataset_name),
                business_date=business_date,
                decision_at=decision_at,
            )
            if batch is None:
                issues.append(f"dataset_unpublished:{dataset_name}")
            else:
                batches[dataset_name] = batch

        profiles = await self._factor_profiles(
            batches.get(ResearchDataset.INSTRUMENT_PROFILES.value),
            symbols=symbols,
            decision_at=decision_at,
        )
        daily = await self._factor_daily_metrics(
            batches.get(ResearchDataset.DAILY_METRICS.value),
            symbols=symbols,
            business_date=business_date,
            decision_at=decision_at,
        )
        financial = await self._factor_financials(
            batches.get(ResearchDataset.FINANCIAL_INDICATORS.value),
            symbols=symbols,
            decision_at=decision_at,
        )
        industry = await self._factor_industries(
            batches.get(ResearchDataset.INDUSTRY_MEMBERSHIPS.value),
            symbols=symbols,
            business_date=business_date,
            decision_at=decision_at,
        )
        return FactorInputBatch(
            records=tuple(
                FactorInputRecord(
                    symbol=symbol,
                    profile=profiles.get(symbol),
                    daily=daily.get(symbol),
                    financial=financial.get(symbol),
                    industry=industry.get(symbol),
                )
                for symbol in symbols
            ),
            source=source,
            dataset_versions={
                name: batch.dataset_version for name, batch in sorted(batches.items())
            },
            issues=tuple(issues),
        )

    async def selection_inputs_gate(
        self,
        config: FactorSelectionConfig,
    ) -> tuple[str, ...]:
        """v1 选股必需数据集的发布状态门(issue #255)。

        research_db 选股在必需数据集批次从未发布时,旧链路会逐期 SKIPPED、
        引擎 0 交易「成功」收场(runs 273-275)。本方法把检查前移:未启用
        选股或非 research_db 模式(bars/snapshot 按设计降级)返回空元组;
        research_db 模式逐个解析 ``required_datasets`` 的已发布批次
        (声明 dataset_version 时查该版本,否则取最近发布),未发布的以
        ``dataset_unpublished:{dataset}`` 具名返回。入队(REST/MCP)与执行端
        (``run_backtest_and_persist``)共用同一检查,双保险。

        注意这是「是否存在已发布批次」的粗粒度检查;批次已发布但不覆盖具体
        交易日的场景仍由逐期 SKIPPED 快照的 skip_reason 承载(配合引擎的
        selection_diagnostics 可见)。
        """
        if not config.enabled or config.inputs_mode is not InputsMode.RESEARCH_DB:
            return ()
        missing: list[str] = []
        for name in sorted(config.required_datasets):
            try:
                dataset = ResearchDataset(name)
            except ValueError:
                missing.append(f"unknown_dataset:{name}")
                continue
            batch = await self._resolve_batch(
                dataset,
                config.source,
                config.dataset_versions.get(name),
            )
            if batch is None:
                missing.append(f"dataset_unpublished:{name}")
        return tuple(missing)

    async def upsert_instrument_profiles(
        self,
        batch: ResearchSyncBatchModel,
        records: list[InstrumentProfile],
    ) -> int:
        """按来源、版本、标的幂等写入档案。"""
        existing = {
            row.symbol: row
            for row in (
                await self._session.execute(
                    select(ResearchInstrumentProfileModel).where(
                        ResearchInstrumentProfileModel.source == batch.source,
                        ResearchInstrumentProfileModel.dataset_version == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            row = existing.get(item.symbol)
            if row is None:
                row = ResearchInstrumentProfileModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    symbol=item.symbol,
                )
                self._session.add(row)
            row.batch_id = batch.id
            row.name = item.name
            row.exchange = item.exchange
            row.market = item.market
            row.list_status = item.list_status
            row.list_date = item.list_date
            row.delist_date = item.delist_date
            row.industry = item.industry
            row.observed_at = item.observed_at
            row.available_at = item.available_at
        await self._session.flush()
        return len(records)

    async def upsert_daily_metrics(
        self,
        batch: ResearchSyncBatchModel,
        records: list[DailySecurityMetrics],
    ) -> int:
        """按来源、版本、标的、交易日幂等写入每日指标。"""
        existing = {
            (row.symbol, row.trade_date): row
            for row in (
                await self._session.execute(
                    select(ResearchDailyMetricModel).where(
                        ResearchDailyMetricModel.source == batch.source,
                        ResearchDailyMetricModel.dataset_version == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            row = existing.get((item.symbol, item.trade_date))
            if row is None:
                row = ResearchDailyMetricModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    symbol=item.symbol,
                    trade_date=item.trade_date,
                )
                self._session.add(row)
            row.batch_id = batch.id
            _copy_daily_fields(row, item)
        await self._session.flush()
        return len(records)

    async def upsert_financial_indicators(
        self,
        batch: ResearchSyncBatchModel,
        records: list[FinancialIndicator],
    ) -> int:
        """幂等写入财务公告版本,不覆盖不同修订。"""
        existing = {
            (
                row.symbol,
                row.report_period,
                row.announcement_date,
                row.update_flag,
            ): row
            for row in (
                await self._session.execute(
                    select(ResearchFinancialIndicatorModel).where(
                        ResearchFinancialIndicatorModel.source == batch.source,
                        ResearchFinancialIndicatorModel.dataset_version == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            key = (
                item.symbol,
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
            )
            row = existing.get(key)
            if row is None:
                row = ResearchFinancialIndicatorModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    symbol=item.symbol,
                    report_period=item.report_period,
                    announcement_date=item.announcement_date,
                    update_flag=item.update_flag or "",
                )
                self._session.add(row)
            row.batch_id = batch.id
            _copy_financial_fields(row, item)
        await self._session.flush()
        return len(records)

    async def upsert_suspensions(
        self,
        batch: ResearchSyncBatchModel,
        records: list[SuspensionRecord],
    ) -> int:
        """按来源、版本、标的、交易日幂等写入停复牌记录(issue #396)。"""
        existing = {
            (row.symbol, row.trade_date, row.suspend_kind): row
            for row in (
                await self._session.execute(
                    select(ResearchSuspensionModel).where(
                        ResearchSuspensionModel.source == batch.source,
                        ResearchSuspensionModel.dataset_version
                        == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            key = (item.symbol, item.trade_date, item.suspend_kind)
            row = existing.get(key)
            if row is None:
                row = ResearchSuspensionModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    symbol=item.symbol,
                    trade_date=item.trade_date,
                    suspend_kind=item.suspend_kind,
                )
                self._session.add(row)
            row.batch_id = batch.id
            row.suspend_type = item.suspend_type
            row.suspend_timing = item.suspend_timing
            row.observed_at = item.observed_at
            row.available_at = item.available_at
        await self._session.flush()
        return len(records)

    async def list_suspensions_as_of(
        self,
        *,
        start_date: date,
        end_date: date,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> list[SuspensionRecord]:
        """读取 ``[start_date, end_date]`` 内决策时点可见的停复牌记录。

        PIT 门控按 ``available_at <= decision_at``;PIT=当日语义下,同一
        交易日的记录在当日 09:30(上海)后可见。
        """
        _require_aware_datetime(decision_at, "decision_at")
        if start_date > end_date:
            return []
        batch = await self._resolve_batch(
            ResearchDataset.SUSPENSIONS,
            source,
            dataset_version,
        )
        if batch is None:
            return []
        stmt = (
            select(ResearchSuspensionModel)
            .where(
                ResearchSuspensionModel.batch_id == batch.id,
                ResearchSuspensionModel.trade_date >= start_date,
                ResearchSuspensionModel.trade_date <= end_date,
                ResearchSuspensionModel.available_at <= decision_at,
            )
            .order_by(
                ResearchSuspensionModel.trade_date,
                ResearchSuspensionModel.symbol,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_suspension_from_orm(row) for row in rows]

    async def upsert_income_statements(
        self,
        batch: ResearchSyncBatchModel,
        records: list[IncomeStatement],
    ) -> int:
        """幂等写入利润表公告版本,修订/报告口径行并存不互相覆盖(#397)。"""
        return await self._upsert_announced_rows(
            batch,
            records,
            model=ResearchIncomeStatementModel,
            keys=lambda row: (
                row.symbol,
                row.report_period,
                row.announcement_date,
                row.update_flag,
                row.report_type,
                row.comp_type,
            ),
            record_keys=lambda item: (
                item.symbol,
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
                item.report_type or "",
                item.comp_type or "",
            ),
        )

    async def upsert_balance_sheets(
        self,
        batch: ResearchSyncBatchModel,
        records: list[BalanceSheet],
    ) -> int:
        """幂等写入资产负债表公告版本(修订语义同利润表,#397)。"""
        return await self._upsert_announced_rows(
            batch,
            records,
            model=ResearchBalanceSheetModel,
            keys=lambda row: (
                row.symbol,
                row.report_period,
                row.announcement_date,
                row.update_flag,
                row.report_type,
                row.comp_type,
            ),
            record_keys=lambda item: (
                item.symbol,
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
                item.report_type or "",
                item.comp_type or "",
            ),
        )

    async def upsert_cashflow_statements(
        self,
        batch: ResearchSyncBatchModel,
        records: list[CashflowStatement],
    ) -> int:
        """幂等写入现金流量表公告版本(修订语义同利润表,#397)。"""
        return await self._upsert_announced_rows(
            batch,
            records,
            model=ResearchCashflowStatementModel,
            keys=lambda row: (
                row.symbol,
                row.report_period,
                row.announcement_date,
                row.update_flag,
                row.report_type,
                row.comp_type,
            ),
            record_keys=lambda item: (
                item.symbol,
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
                item.report_type or "",
                item.comp_type or "",
            ),
        )

    async def upsert_dividends(
        self,
        batch: ResearchSyncBatchModel,
        records: list[DividendRecord],
    ) -> int:
        """幂等写入分红送股进展记录;``div_proc`` 进身份键全保留(#397)。"""
        return await self._upsert_announced_rows(
            batch,
            records,
            model=ResearchDividendModel,
            keys=lambda row: (
                row.symbol,
                row.report_period,
                row.announcement_date,
                row.div_proc,
            ),
            record_keys=lambda item: (
                item.symbol,
                item.report_period,
                item.announcement_date,
                item.div_proc or "",
            ),
        )

    async def _upsert_announced_rows(
        self,
        batch: ResearchSyncBatchModel,
        records: list[Any],
        *,
        model: Any,
        keys: Callable[[Any], tuple[object, ...]],
        record_keys: Callable[[Any], tuple[object, ...]],
    ) -> int:
        """三表/dividend 共用的幂等写入(身份键由调用方声明,值列按表列
        反射拷贝 —— 模型 ↔ 领域记录 ↔ 白名单三处同名,加列零改动)。"""
        identity_columns = _STATEMENT_IDENTITY_COLUMNS
        value_columns = tuple(
            column.name
            for column in model.__table__.columns
            if column.name not in identity_columns
        )
        existing = {
            keys(row): row
            for row in (
                await self._session.execute(
                    select(model).where(
                        model.source == batch.source,
                        model.dataset_version == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            row = existing.get(record_keys(item))
            if row is None:
                row = model(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    symbol=item.symbol,
                    report_period=item.report_period,
                    announcement_date=item.announcement_date,
                )
                self._session.add(row)
            row.batch_id = batch.id
            if hasattr(item, "update_flag"):
                row.update_flag = item.update_flag or ""
            if hasattr(item, "report_type"):
                row.report_type = item.report_type or ""
                row.comp_type = item.comp_type or ""
            if hasattr(item, "div_proc"):
                row.div_proc = item.div_proc or ""
            if hasattr(item, "formal_announcement_date"):
                row.formal_announcement_date = item.formal_announcement_date
            for name in value_columns:
                setattr(row, name, getattr(item, name))
            row.observed_at = item.observed_at
            row.available_at = item.available_at
        await self._session.flush()
        return len(records)

    async def upsert_industry_memberships(
        self,
        batch: ResearchSyncBatchModel,
        records: list[IndustryMembership],
    ) -> int:
        """幂等写入行业分类字典和成员有效区间。"""
        await self._upsert_industry_classifications(batch, records)
        existing = {
            (
                row.taxonomy,
                row.symbol,
                row.level3_code,
                row.valid_from,
            ): row
            for row in (
                await self._session.execute(
                    select(ResearchIndustryMembershipModel).where(
                        ResearchIndustryMembershipModel.source == batch.source,
                        ResearchIndustryMembershipModel.dataset_version == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        for item in records:
            key = (
                item.taxonomy,
                item.symbol,
                item.level3_code,
                item.effective_from,
            )
            row = existing.get(key)
            if row is None:
                row = ResearchIndustryMembershipModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    taxonomy=item.taxonomy,
                    symbol=item.symbol,
                    level3_code=item.level3_code,
                    valid_from=item.effective_from,
                )
                self._session.add(row)
            row.batch_id = batch.id
            row.security_name = item.security_name
            row.level1_code = item.level1_code
            row.level2_code = item.level2_code
            row.level3_code = item.level3_code
            row.valid_to = item.effective_to
            row.is_current = item.is_current
            row.observed_at = item.observed_at
            row.available_at = item.available_at
        await self._session.flush()
        return len(records)

    async def get_instrument_profile_as_of(
        self,
        *,
        symbol: str,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> InstrumentProfile | None:
        """读取决策时点可见的标的档案。"""
        _require_aware_datetime(decision_at, "decision_at")
        batch = await self._resolve_batch(
            ResearchDataset.INSTRUMENT_PROFILES,
            source,
            dataset_version,
        )
        if batch is None:
            return None
        stmt = select(ResearchInstrumentProfileModel).where(
            ResearchInstrumentProfileModel.batch_id == batch.id,
            ResearchInstrumentProfileModel.symbol == symbol,
            ResearchInstrumentProfileModel.available_at <= decision_at,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _profile_from_orm(row) if row is not None else None

    async def get_daily_metric_as_of(
        self,
        *,
        symbol: str,
        trade_date: date,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> DailySecurityMetrics | None:
        """读取某交易日且在决策时点已可用的每日指标。"""
        _require_aware_datetime(decision_at, "decision_at")
        batch = await self._resolve_batch(
            ResearchDataset.DAILY_METRICS,
            source,
            dataset_version,
        )
        if batch is None:
            return None
        stmt = select(ResearchDailyMetricModel).where(
            ResearchDailyMetricModel.batch_id == batch.id,
            ResearchDailyMetricModel.symbol == symbol,
            ResearchDailyMetricModel.trade_date == trade_date,
            ResearchDailyMetricModel.available_at <= decision_at,
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return _daily_from_orm(row) if row is not None else None

    async def list_daily_metrics_as_of(
        self,
        *,
        trade_date: date,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> list[DailySecurityMetrics]:
        """读取同一已发布版本的单日截面。"""
        _require_aware_datetime(decision_at, "decision_at")
        batch = await self._resolve_batch(
            ResearchDataset.DAILY_METRICS,
            source,
            dataset_version,
        )
        if batch is None:
            return []
        stmt = (
            select(ResearchDailyMetricModel)
            .where(
                ResearchDailyMetricModel.batch_id == batch.id,
                ResearchDailyMetricModel.trade_date == trade_date,
                ResearchDailyMetricModel.available_at <= decision_at,
            )
            .order_by(ResearchDailyMetricModel.symbol)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_daily_from_orm(row) for row in rows]

    async def list_financial_indicators_as_of(
        self,
        *,
        symbol: str,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> list[FinancialIndicator]:
        """读取决策时点已经公告的全部财务修订。"""
        _require_aware_datetime(decision_at, "decision_at")
        batch = await self._resolve_batch(
            ResearchDataset.FINANCIAL_INDICATORS,
            source,
            dataset_version,
        )
        if batch is None:
            return []
        stmt = (
            select(ResearchFinancialIndicatorModel)
            .where(
                ResearchFinancialIndicatorModel.batch_id == batch.id,
                ResearchFinancialIndicatorModel.symbol == symbol,
                ResearchFinancialIndicatorModel.available_at <= decision_at,
            )
            .order_by(
                ResearchFinancialIndicatorModel.report_period,
                ResearchFinancialIndicatorModel.announcement_date,
                ResearchFinancialIndicatorModel.update_flag,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_financial_from_orm(row) for row in rows]

    async def list_industry_memberships_as_of(
        self,
        *,
        symbol: str,
        business_date: date,
        decision_at: datetime,
        source: str,
        dataset_version: str | None = None,
    ) -> list[IndustryMembership]:
        """按业务有效区间和信息可用时间读取行业归属。"""
        _require_aware_datetime(decision_at, "decision_at")
        batch = await self._resolve_batch(
            ResearchDataset.INDUSTRY_MEMBERSHIPS,
            source,
            dataset_version,
        )
        if batch is None:
            return []
        stmt = (
            select(ResearchIndustryMembershipModel)
            .where(
                ResearchIndustryMembershipModel.batch_id == batch.id,
                ResearchIndustryMembershipModel.symbol == symbol,
                ResearchIndustryMembershipModel.valid_from <= business_date,
                or_(
                    ResearchIndustryMembershipModel.valid_to.is_(None),
                    ResearchIndustryMembershipModel.valid_to >= business_date,
                ),
                ResearchIndustryMembershipModel.available_at <= decision_at,
            )
            .order_by(ResearchIndustryMembershipModel.level3_code)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        classifications = await self._classification_lookup(batch.id)
        return [_industry_from_orm(row, classifications) for row in rows]

    async def _resolve_batch(
        self,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str | None,
    ) -> ResearchSyncBatchModel | None:
        if dataset_version is not None:
            return await self._batches.published_version(
                dataset=dataset,
                source=source,
                dataset_version=dataset_version,
            )
        return await self._batches.latest_published(dataset=dataset, source=source)

    async def _resolve_factor_batch(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str | None,
        business_date: date,
        decision_at: datetime,
    ) -> ResearchSyncBatchModel | None:
        if dataset_version is not None:
            return await self._batches.published_version(
                dataset=dataset,
                source=source,
                dataset_version=dataset_version,
            )
        if dataset is not ResearchDataset.DAILY_METRICS:
            return await self._batches.latest_published(
                dataset=dataset,
                source=source,
            )
        stmt = (
            select(ResearchSyncBatchModel)
            .join(
                ResearchDailyMetricModel,
                ResearchDailyMetricModel.batch_id == ResearchSyncBatchModel.id,
            )
            .where(
                ResearchSyncBatchModel.dataset == dataset.value,
                ResearchSyncBatchModel.source == source,
                ResearchSyncBatchModel.status == SyncBatchStatus.PUBLISHED.value,
                ResearchDailyMetricModel.trade_date == business_date,
                ResearchDailyMetricModel.available_at <= decision_at,
            )
            .order_by(
                ResearchSyncBatchModel.published_at.desc(),
                ResearchSyncBatchModel.id.desc(),
            )
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def _factor_profiles(
        self,
        batch: ResearchSyncBatchModel | None,
        *,
        symbols: tuple[str, ...],
        decision_at: datetime,
    ) -> dict[str, InstrumentProfile]:
        if batch is None:
            return {}
        stmt = select(ResearchInstrumentProfileModel).where(
            ResearchInstrumentProfileModel.batch_id == batch.id,
            ResearchInstrumentProfileModel.symbol.in_(symbols),
            ResearchInstrumentProfileModel.available_at <= decision_at,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.symbol: _profile_from_orm(row) for row in rows}

    async def _factor_daily_metrics(
        self,
        batch: ResearchSyncBatchModel | None,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
    ) -> dict[str, DailySecurityMetrics]:
        if batch is None:
            return {}
        stmt = select(ResearchDailyMetricModel).where(
            ResearchDailyMetricModel.batch_id == batch.id,
            ResearchDailyMetricModel.symbol.in_(symbols),
            ResearchDailyMetricModel.trade_date == business_date,
            ResearchDailyMetricModel.available_at <= decision_at,
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return {row.symbol: _daily_from_orm(row) for row in rows}

    async def _factor_financials(
        self,
        batch: ResearchSyncBatchModel | None,
        *,
        symbols: tuple[str, ...],
        decision_at: datetime,
    ) -> dict[str, FinancialIndicator]:
        if batch is None:
            return {}
        stmt = (
            select(ResearchFinancialIndicatorModel)
            .where(
                ResearchFinancialIndicatorModel.batch_id == batch.id,
                ResearchFinancialIndicatorModel.symbol.in_(symbols),
                ResearchFinancialIndicatorModel.available_at <= decision_at,
            )
            .order_by(
                ResearchFinancialIndicatorModel.symbol,
                ResearchFinancialIndicatorModel.report_period.desc(),
                ResearchFinancialIndicatorModel.announcement_date.desc(),
                ResearchFinancialIndicatorModel.update_flag.desc(),
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        result: dict[str, FinancialIndicator] = {}
        for row in rows:
            result.setdefault(row.symbol, _financial_from_orm(row))
        return result

    async def _factor_industries(
        self,
        batch: ResearchSyncBatchModel | None,
        *,
        symbols: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
    ) -> dict[str, IndustryMembership]:
        if batch is None:
            return {}
        stmt = (
            select(ResearchIndustryMembershipModel)
            .where(
                ResearchIndustryMembershipModel.batch_id == batch.id,
                ResearchIndustryMembershipModel.symbol.in_(symbols),
                ResearchIndustryMembershipModel.valid_from <= business_date,
                or_(
                    ResearchIndustryMembershipModel.valid_to.is_(None),
                    ResearchIndustryMembershipModel.valid_to >= business_date,
                ),
                ResearchIndustryMembershipModel.available_at <= decision_at,
            )
            .order_by(
                ResearchIndustryMembershipModel.symbol,
                ResearchIndustryMembershipModel.taxonomy,
                ResearchIndustryMembershipModel.level3_code,
            )
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        names = await self._classification_lookup(batch.id)
        result: dict[str, IndustryMembership] = {}
        for row in rows:
            result.setdefault(row.symbol, _industry_from_orm(row, names))
        return result

    async def _upsert_industry_classifications(
        self,
        batch: ResearchSyncBatchModel,
        records: list[IndustryMembership],
    ) -> None:
        existing = {
            (row.taxonomy, row.level, row.industry_code): row
            for row in (
                await self._session.execute(
                    select(ResearchIndustryClassificationModel).where(
                        ResearchIndustryClassificationModel.source == batch.source,
                        ResearchIndustryClassificationModel.dataset_version
                        == batch.dataset_version,
                    )
                )
            )
            .scalars()
            .all()
        }
        values: dict[
            tuple[str, int, str],
            tuple[str, datetime, datetime],
        ] = {}
        for item in records:
            values[(item.taxonomy, 1, item.level1_code)] = (
                item.level1_name,
                item.observed_at,
                item.available_at,
            )
            values[(item.taxonomy, 2, item.level2_code)] = (
                item.level2_name,
                item.observed_at,
                item.available_at,
            )
            values[(item.taxonomy, 3, item.level3_code)] = (
                item.level3_name,
                item.observed_at,
                item.available_at,
            )
        for key, value in values.items():
            taxonomy, level, code = key
            name, observed_at, available_at = value
            row = existing.get(key)
            if row is None:
                row = ResearchIndustryClassificationModel(
                    batch_id=batch.id,
                    source=batch.source,
                    dataset_version=batch.dataset_version,
                    taxonomy=taxonomy,
                    level=level,
                    industry_code=code,
                )
                self._session.add(row)
            row.batch_id = batch.id
            row.industry_name = name
            row.observed_at = observed_at
            row.available_at = available_at

    async def _classification_lookup(
        self,
        batch_id: int,
    ) -> dict[tuple[str, int, str], str]:
        rows = (
            (
                await self._session.execute(
                    select(ResearchIndustryClassificationModel).where(
                        ResearchIndustryClassificationModel.batch_id == batch_id
                    )
                )
            )
            .scalars()
            .all()
        )
        return {(row.taxonomy, row.level, row.industry_code): row.industry_name for row in rows}


def _copy_daily_fields(
    row: ResearchDailyMetricModel,
    item: DailySecurityMetrics,
) -> None:
    row.close = item.close
    row.turnover_rate = item.turnover_rate
    row.turnover_rate_free = item.turnover_rate_free
    row.volume_ratio = item.volume_ratio
    row.pe = item.pe
    row.pe_ttm = item.pe_ttm
    row.pb = item.pb
    row.ps = item.ps
    row.ps_ttm = item.ps_ttm
    row.dividend_yield = item.dividend_yield
    row.dividend_yield_ttm = item.dividend_yield_ttm
    row.total_shares = item.total_shares
    row.float_shares = item.float_shares
    row.free_shares = item.free_shares
    row.total_market_cap = item.total_market_cap
    row.circulating_market_cap = item.circulating_market_cap
    row.limit_status = item.limit_status
    row.observed_at = item.observed_at
    row.available_at = item.available_at


def _copy_financial_fields(
    row: ResearchFinancialIndicatorModel,
    item: FinancialIndicator,
) -> None:
    row.eps = item.eps
    row.diluted_eps = item.diluted_eps
    row.book_value_per_share = item.book_value_per_share
    row.operating_cash_flow_per_share = item.operating_cash_flow_per_share
    row.return_on_equity = item.return_on_equity
    row.weighted_return_on_equity = item.weighted_return_on_equity
    row.gross_profit_margin = item.gross_profit_margin
    row.net_profit_margin = item.net_profit_margin
    row.debt_to_assets = item.debt_to_assets
    row.revenue_yoy = item.revenue_yoy
    row.net_profit_yoy = item.net_profit_yoy
    row.operating_cash_flow_yoy = item.operating_cash_flow_yoy
    row.observed_at = item.observed_at
    row.available_at = item.available_at


def _profile_from_orm(row: ResearchInstrumentProfileModel) -> InstrumentProfile:
    return InstrumentProfile(
        symbol=row.symbol,
        name=row.name,
        exchange=row.exchange,
        market=row.market,
        list_status=row.list_status,
        list_date=row.list_date,
        delist_date=row.delist_date,
        industry=row.industry,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _daily_from_orm(row: ResearchDailyMetricModel) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=row.symbol,
        trade_date=row.trade_date,
        close=row.close,
        turnover_rate=row.turnover_rate,
        turnover_rate_free=row.turnover_rate_free,
        volume_ratio=row.volume_ratio,
        pe=row.pe,
        pe_ttm=row.pe_ttm,
        pb=row.pb,
        ps=row.ps,
        ps_ttm=row.ps_ttm,
        dividend_yield=row.dividend_yield,
        dividend_yield_ttm=row.dividend_yield_ttm,
        total_shares=row.total_shares,
        float_shares=row.float_shares,
        free_shares=row.free_shares,
        total_market_cap=row.total_market_cap,
        circulating_market_cap=row.circulating_market_cap,
        limit_status=row.limit_status,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _financial_from_orm(
    row: ResearchFinancialIndicatorModel,
) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=row.symbol,
        announcement_date=row.announcement_date,
        report_period=row.report_period,
        update_flag=row.update_flag or None,
        eps=row.eps,
        diluted_eps=row.diluted_eps,
        book_value_per_share=row.book_value_per_share,
        operating_cash_flow_per_share=row.operating_cash_flow_per_share,
        return_on_equity=row.return_on_equity,
        weighted_return_on_equity=row.weighted_return_on_equity,
        gross_profit_margin=row.gross_profit_margin,
        net_profit_margin=row.net_profit_margin,
        debt_to_assets=row.debt_to_assets,
        revenue_yoy=row.revenue_yoy,
        net_profit_yoy=row.net_profit_yoy,
        operating_cash_flow_yoy=row.operating_cash_flow_yoy,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _industry_from_orm(
    row: ResearchIndustryMembershipModel,
    names: dict[tuple[str, int, str], str],
) -> IndustryMembership:
    return IndustryMembership(
        symbol=row.symbol,
        security_name=row.security_name,
        taxonomy=row.taxonomy,
        level1_code=row.level1_code,
        level1_name=names[(row.taxonomy, 1, row.level1_code)],
        level2_code=row.level2_code,
        level2_name=names[(row.taxonomy, 2, row.level2_code)],
        level3_code=row.level3_code,
        level3_name=names[(row.taxonomy, 3, row.level3_code)],
        effective_from=row.valid_from,
        effective_to=row.valid_to,
        is_current=row.is_current,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _suspension_from_orm(row: ResearchSuspensionModel) -> SuspensionRecord:
    return SuspensionRecord(
        symbol=row.symbol,
        trade_date=row.trade_date,
        suspend_kind=row.suspend_kind,
        suspend_type=row.suspend_type,
        suspend_timing=row.suspend_timing,
        source=row.source,
        observed_at=row.observed_at,
        available_at=row.available_at,
    )


def _require_aware_datetime(value: object, field: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} 必须是带时区的 datetime")
