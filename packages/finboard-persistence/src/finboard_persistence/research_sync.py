"""研究数据同步、质量门和原子发布。

同步批次先独立落库,再在一个事务中写入规范化数据并发布。上游失败、质量失败或
数据库异常都不会改变最近一次已发布版本。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from datetime import date, datetime
from decimal import Decimal

from finboard_data.quality import (
    QualityReport,
    QualityStatus,
    ResearchDataQualityValidator,
)
from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
)
from finboard_persistence.models import ResearchSyncBatchModel
from finboard_persistence.research_repo import (
    ResearchDataset,
    ResearchDatasetRepository,
    ResearchSyncBatchRepository,
)
from finboard_persistence.session import AsyncSessionMaker

type _JsonValue = None | bool | int | float | str | list["_JsonValue"] | dict[str, "_JsonValue"]
type _Writer = Callable[
    [ResearchDatasetRepository, ResearchSyncBatchModel],
    Awaitable[int],
]
_SENSITIVE_KEYS = frozenset(
    {"token", "password", "secret", "auth_code", "authorization", "api_key"}
)


class ResearchDataSyncService:
    """按来源和数据集版本执行可重试的增量同步。"""

    def __init__(
        self,
        session_maker: AsyncSessionMaker,
        *,
        validator_factory: Callable[[], ResearchDataQualityValidator] | None = None,
    ) -> None:
        self._session_maker = session_maker
        self._validator_factory = validator_factory or ResearchDataQualityValidator

    async def sync_instrument_profiles(
        self,
        *,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        records: list[InstrumentProfile],
        expected_symbols: set[str] | None = None,
    ) -> ResearchSyncBatchModel:
        """校验并同步标的档案快照。"""
        report = self._validator_factory().validate_instrument_profiles(
            records,
            expected_source=source,
            expected_symbols=expected_symbols,
        )

        async def writer(
            repo: ResearchDatasetRepository,
            batch: ResearchSyncBatchModel,
        ) -> int:
            return await repo.upsert_instrument_profiles(batch, records)

        return await self._sync(
            dataset=ResearchDataset.INSTRUMENT_PROFILES,
            source=source,
            dataset_version=dataset_version,
            code_version=code_version,
            parameters=parameters,
            raw_payload=raw_payload,
            report=report,
            writer=writer,
        )

    async def sync_daily_metrics(
        self,
        *,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        records: list[DailySecurityMetrics],
        expected_trade_date: date,
        expected_symbols: set[str] | None = None,
    ) -> ResearchSyncBatchModel:
        """校验并同步单日证券指标截面。"""
        report = self._validator_factory().validate_daily_metrics(
            records,
            expected_trade_date=expected_trade_date,
            expected_source=source,
            expected_symbols=expected_symbols,
        )

        async def writer(
            repo: ResearchDatasetRepository,
            batch: ResearchSyncBatchModel,
        ) -> int:
            return await repo.upsert_daily_metrics(batch, records)

        return await self._sync(
            dataset=ResearchDataset.DAILY_METRICS,
            source=source,
            dataset_version=dataset_version,
            code_version=code_version,
            parameters=parameters,
            raw_payload=raw_payload,
            report=report,
            writer=writer,
        )

    async def sync_financial_indicators(
        self,
        *,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        records: list[FinancialIndicator],
    ) -> ResearchSyncBatchModel:
        """校验并同步财务公告修订。"""
        report = self._validator_factory().validate_financial_indicators(
            records,
            expected_source=source,
        )

        async def writer(
            repo: ResearchDatasetRepository,
            batch: ResearchSyncBatchModel,
        ) -> int:
            return await repo.upsert_financial_indicators(batch, records)

        return await self._sync(
            dataset=ResearchDataset.FINANCIAL_INDICATORS,
            source=source,
            dataset_version=dataset_version,
            code_version=code_version,
            parameters=parameters,
            raw_payload=raw_payload,
            report=report,
            writer=writer,
        )

    async def sync_industry_memberships(
        self,
        *,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        records: list[IndustryMembership],
        expected_symbols: set[str] | None = None,
    ) -> ResearchSyncBatchModel:
        """校验并同步行业分类字典及成员有效区间。"""
        report = self._validator_factory().validate_industry_memberships(
            records,
            expected_source=source,
            expected_symbols=expected_symbols,
        )

        async def writer(
            repo: ResearchDatasetRepository,
            batch: ResearchSyncBatchModel,
        ) -> int:
            return await repo.upsert_industry_memberships(batch, records)

        return await self._sync(
            dataset=ResearchDataset.INDUSTRY_MEMBERSHIPS,
            source=source,
            dataset_version=dataset_version,
            code_version=code_version,
            parameters=parameters,
            raw_payload=raw_payload,
            report=report,
            writer=writer,
        )

    async def record_upstream_failure(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        error_type: str,
    ) -> ResearchSyncBatchModel:
        """登记限流、超时或上游不完整响应,不保存异常正文或凭据。"""
        safe_error_chars: list[str] = []
        for char in error_type:
            if not (char.isalnum() or char in "._-"):
                break
            safe_error_chars.append(char)
        safe_error_type = "".join(safe_error_chars)[:80]
        summary = f"{safe_error_type or 'UpstreamError'}: upstream fetch failed"
        async with self._session_maker() as session:
            batches = ResearchSyncBatchRepository(session)
            batch, already_published = await batches.prepare(
                dataset=dataset,
                source=source,
                dataset_version=dataset_version,
                code_version=code_version,
                parameters=_json_mapping(parameters),
                raw_payload=None,
                expected_rows=None,
                received_rows=0,
            )
            if not already_published:
                batch = await batches.mark_failed(
                    batch.id,
                    error_summary=summary,
                )
            await session.commit()
            return batch

    async def _sync(
        self,
        *,
        dataset: ResearchDataset,
        source: str,
        dataset_version: str,
        code_version: str,
        parameters: Mapping[str, object],
        raw_payload: object | None,
        report: QualityReport,
        writer: _Writer,
    ) -> ResearchSyncBatchModel:
        safe_parameters = _json_mapping(parameters)
        safe_raw_payload = _json_value(raw_payload)
        async with self._session_maker() as session:
            batches = ResearchSyncBatchRepository(session)
            batch, already_published = await batches.prepare(
                dataset=dataset,
                source=source,
                dataset_version=dataset_version,
                code_version=code_version,
                parameters=safe_parameters,
                raw_payload=safe_raw_payload,
                expected_rows=report.expected_count,
                received_rows=report.row_count,
            )
            await session.commit()
            if already_published:
                return batch
            batch_id = batch.id

        if not report.publishable:
            async with self._session_maker() as session:
                batches = ResearchSyncBatchRepository(session)
                batch = await batches.mark_quality_rejected(
                    batch_id,
                    partial=report.status is QualityStatus.PARTIAL,
                    quality_status=report.status.value,
                    quality_report=report.as_dict(),
                    rejected_rows=report.row_count,
                )
                await session.commit()
                return batch

        try:
            async with self._session_maker() as session:
                batches = ResearchSyncBatchRepository(session)
                locked_batch = await batches.get(batch_id, for_update=True)
                if locked_batch is None:
                    raise RuntimeError(f"研究数据同步批次 {batch_id} 不存在")
                accepted_rows = await writer(
                    ResearchDatasetRepository(session),
                    locked_batch,
                )
                batch = await batches.mark_published(
                    batch_id,
                    accepted_rows=accepted_rows,
                    quality_status=report.status.value,
                    quality_report=report.as_dict(),
                )
                await session.commit()
                return batch
        except Exception:
            await self._mark_database_failure(batch_id)
            raise

    async def _mark_database_failure(self, batch_id: int) -> None:
        try:
            async with self._session_maker() as session:
                batch = await ResearchSyncBatchRepository(session).mark_failed(
                    batch_id,
                    error_summary="DatabaseError: normalized write failed",
                )
                del batch
                await session.commit()
        except Exception:
            # 保留最初的数据库异常;状态修复可由同 dataset_version 的调度重试完成。
            return


def _json_mapping(value: Mapping[str, object]) -> dict[str, _JsonValue]:
    result = _json_value(dict(value))
    if not isinstance(result, dict):
        raise ValueError("parameters 必须是 JSON 对象")
    return result


def _json_value(value: object, *, key: str | None = None) -> _JsonValue:
    if key is not None and key.lower() in _SENSITIVE_KEYS:
        return "***"
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {
            str(item_key): _json_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    raise ValueError(f"无法归档非 JSON 类型: {type(value).__name__}")
