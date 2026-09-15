"""离线 ResearchRun 与逐阶段 artifact 仓储(issue #80)。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Json
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from finboard_persistence.models import (
    ResearchRunArtifactModel,
    ResearchRunModel,
)

#: artifact 流式读取的行缓冲(#470 前半场):决策级 13 行/决策,26 行 ≈ 2 决策。
_ARTIFACT_STREAM_CHUNK = 26


def _identity_json_dumps(obj: Any) -> Any:
    """驱动级 Json 包装的直写 dumps(#472):入参已是 canonical JSON 文本。"""

    return obj


class ResearchRunPersistenceConflictError(RuntimeError):
    """数据库中的幂等内容或状态与请求冲突。"""


@dataclass(frozen=True)
class ResearchRunArtifactSummary:
    """``ResearchRunRepository.summarize_artifacts`` 的有界聚合结果(#478)。

    只含计数与分组键,不含任何 artifact payload;``summary_dict`` 输出与
    ``finboard_mcp.reporting.summarize_run_artifacts`` 的返回逐键同构。
    """

    artifact_count: int
    universe_total: int
    universe_included: int
    universe_excluded_by_reason: dict[str, int]
    fills_total: int
    fills_by_decision: dict[str, int]

    def summary_dict(self) -> dict[str, Any]:
        return {
            "universe": {
                "total": self.universe_total,
                "included": self.universe_included,
                "excluded_by_reason": dict(self.universe_excluded_by_reason),
            },
            "fills": {
                "total": self.fills_total,
                "by_decision": dict(self.fills_by_decision),
            },
        }


#: run 全部 artifact 行数(不限 stage;主键/索引扫描,不触 payload)。
_ARTIFACT_COUNT_SQL = text(
    "SELECT count(*) FROM research_run_artifacts WHERE run_id = :run_id"
)

#: 候选池聚合计数:单次物化 CTE 同时算 total / included / excluded_by_reason。
#: 语义与 summarize_run_artifacts 的 Python 聚合一致 —— ``included`` 仅认
#: JSON 布尔 true(平台契约候选池 included 恒为 bool);未 included 标的按
#: reasons 逐条计数,reasons 缺失 / 为空 / 非数组归 ``unknown``。
#: 健壮性:jsonb 函数只出现在 CASE 的 THEN 分支(PG 不保证 AND 两侧短路,
#: 嵌套 CASE 才有定义的求值顺序),非数组 candidates / reasons 静默归零,
#: 不抛错。
_UNIVERSE_SUMMARY_SQL = text(
    """
    WITH candidates AS MATERIALIZED (
        SELECT COALESCE((c.value -> 'included') = 'true'::jsonb, false) AS included,
               CASE
                   WHEN jsonb_typeof(c.value -> 'reasons') = 'array'
                   THEN CASE
                            WHEN jsonb_array_length(c.value -> 'reasons') > 0
                            THEN c.value -> 'reasons'
                            ELSE '["unknown"]'::jsonb
                        END
                   ELSE '["unknown"]'::jsonb
               END AS reasons
        FROM research_run_artifacts AS artifact
        CROSS JOIN LATERAL jsonb_array_elements(
            CASE
                WHEN jsonb_typeof(artifact.payload::jsonb -> 'candidates') = 'array'
                THEN artifact.payload::jsonb -> 'candidates'
                ELSE '[]'::jsonb
            END
        ) AS c(value)
        WHERE artifact.run_id = :run_id
          AND artifact.stage = 'universe'
    )
    SELECT (SELECT count(*) FROM candidates) AS total,
           (SELECT count(*) FROM candidates WHERE included) AS included,
           (SELECT COALESCE(jsonb_object_agg(reason_text, reason_count), '{}'::jsonb)
              FROM (SELECT reason_text, count(*) AS reason_count
                      FROM candidates
                      CROSS JOIN LATERAL jsonb_array_elements_text(reasons)
                          AS reason_text
                     WHERE NOT included
                  GROUP BY reason_text) AS reason_counts) AS excluded_by_reason
    """
)

#: fills 按决策计数:``decision_id`` NULL 记空串;0 长度也保键,与 Python
#: 聚合的无条件赋值一致;非数组 / 缺失 fills 记 0(jsonb 函数只在 CASE
#: THEN 分支求值,非数组不抛错)。
_FILLS_SUMMARY_SQL = text(
    """
    SELECT COALESCE(artifact.decision_id, '') AS decision_key,
           COALESCE(SUM(CASE
               WHEN jsonb_typeof(artifact.payload::jsonb -> 'fills') = 'array'
               THEN jsonb_array_length(artifact.payload::jsonb -> 'fills')
               ELSE 0
           END), 0) AS fill_count
    FROM research_run_artifacts AS artifact
    WHERE artifact.run_id = :run_id
      AND artifact.stage = 'fills'
    GROUP BY decision_key
    """
)


class ResearchRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def checkpoint(self) -> None:
        await self._session.commit()

    async def create_or_get(
        self,
        *,
        run_id: str,
        idempotency_key: str,
        replay_of_run_id: str | None,
        strategy_id: str,
        strategy_kind: str,
        status: str,
        schema_version: str,
        manifest_checksum: str,
        manifest: dict[str, object],
        requested_by: str,
        job_id: str | None = None,
    ) -> tuple[ResearchRunModel, bool]:
        existing = await self.get_by_idempotency_key(idempotency_key)
        if existing is None:
            existing = await self.get(run_id)
        if existing is not None:
            if existing.manifest_checksum != manifest_checksum:
                raise ResearchRunPersistenceConflictError(
                    "相同 run_id/idempotency_key 对应不同 manifest"
                )
            return existing, False
        row = ResearchRunModel(
            run_id=run_id,
            idempotency_key=idempotency_key,
            replay_of_run_id=replay_of_run_id,
            strategy_id=strategy_id,
            strategy_kind=strategy_kind,
            status=status,
            schema_version=schema_version,
            manifest_checksum=manifest_checksum,
            manifest=manifest,
            requested_by=requested_by,
            job_id=job_id,
        )
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
            return row, True
        except IntegrityError:
            existing = await self.get_by_idempotency_key(idempotency_key)
            if existing is None:
                existing = await self.get(run_id)
            if existing is None:
                raise
            if existing.manifest_checksum != manifest_checksum:
                raise ResearchRunPersistenceConflictError(
                    "并发创建命中相同身份但 manifest 不同"
                ) from None
            return existing, False

    async def get(
        self, run_id: str, *, for_update: bool = False
    ) -> ResearchRunModel | None:
        stmt = select(ResearchRunModel).where(ResearchRunModel.run_id == run_id)
        if for_update:
            stmt = stmt.with_for_update()
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_by_idempotency_key(
        self, idempotency_key: str
    ) -> ResearchRunModel | None:
        stmt = select(ResearchRunModel).where(
            ResearchRunModel.idempotency_key == idempotency_key
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_recent(
        self,
        *,
        statuses: Iterable[str] | None = None,
        strategy_kind: str | None = None,
        limit: int = 100,
    ) -> list[ResearchRunModel]:
        stmt = select(ResearchRunModel)
        if statuses is not None:
            stmt = stmt.where(ResearchRunModel.status.in_(tuple(statuses)))
        if strategy_kind is not None:
            stmt = stmt.where(ResearchRunModel.strategy_kind == strategy_kind)
        stmt = stmt.order_by(
            ResearchRunModel.created_at.desc(), ResearchRunModel.id.desc()
        ).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def get_status_by_job_id(self, job_id: str) -> str | None:
        """按关联 job_id 取 run 状态(issue #306);无关联 run 返回 None。

        供 job 视图(``GET /api/jobs/{id}`` / ``finboard_job_get``)把
        ``research_runs.status`` 透传为 ``run_status``,让「run interrupted 但
        job 仍 running」的两表不一致一眼可见。轻量投影只取 status 列。
        """

        stmt = (
            select(ResearchRunModel.status)
            .where(ResearchRunModel.job_id == job_id)
            .order_by(ResearchRunModel.id.asc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[str],
        target: str,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunModel:
        row = await self.get(run_id, for_update=True)
        if row is None:
            raise ResearchRunPersistenceConflictError(f"研究运行不存在: {run_id}")
        if row.status not in expected:
            raise ResearchRunPersistenceConflictError(
                f"运行 {run_id} 当前状态 {row.status} 不在 {sorted(expected)}"
            )
        now = datetime.now(UTC)
        row.status = target
        row.error_code = error_code
        row.error_summary = error_summary
        row.updated_at = now
        if target == "running" and row.started_at is None:
            row.started_at = now
        if target in {"completed", "failed", "rejected", "cancelled"}:
            row.completed_at = now
        await self._session.flush()
        return row

    async def save_result(
        self,
        run_id: str,
        *,
        result: dict[str, object],
        result_checksum: str,
    ) -> ResearchRunModel:
        row = await self.get(run_id, for_update=True)
        if row is None:
            raise ResearchRunPersistenceConflictError(f"研究运行不存在: {run_id}")
        if row.result_checksum is not None and row.result_checksum != result_checksum:
            raise ResearchRunPersistenceConflictError("同一次运行产生了不同结果")
        row.result = result
        row.result_checksum = result_checksum
        row.updated_at = datetime.now(UTC)
        await self._session.flush()
        return row

    async def append_artifact(
        self,
        *,
        run_id: str,
        artifact_id: str,
        decision_id: str | None,
        sequence: int,
        stage: str,
        trace_id: str,
        parent_trace_ids: list[str],
        payload: dict[str, object],
        checksum: str,
        payload_json: str | None = None,
    ) -> tuple[ResearchRunArtifactModel | None, bool]:
        # 幂等命中分支只比对 checksum,不消费 payload —— load_only 让既有行
        # 的 payload 列保持 deferred(#470 前半场:断点续算种子逐决策重持久化
        # 时,每个命中行省掉一次全量 payload JSONB → Python 物化,features
        # 期均 ~8.5MB)。命中行除 checksum 外的字段未加载,调用方只读 created
        # 标志;新插入行不受影响。
        stmt = (
            select(ResearchRunArtifactModel)
            .where(
                ResearchRunArtifactModel.run_id == run_id,
                ResearchRunArtifactModel.artifact_id == artifact_id,
            )
            .options(load_only(ResearchRunArtifactModel.checksum))
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            if existing.checksum != checksum:
                raise ResearchRunPersistenceConflictError(
                    f"artifact {artifact_id} checkpoint 内容冲突"
                )
            return existing, False
        # issue #472:payload_json 为 canonical JSON 文本时直写 —— 驱动级
        # ``Json(text, dumps=identity)`` 由 CanonicalPayloadJson 列放行,不再对
        # dict 二次 ``json.dumps``(文本已由研究域编码,checksum 即该文本的
        # sha256)。直写路径走 Core INSERT:驱动包装值不进 identity map(ORM
        # 对象属性会把 ``Json`` 包装物回吐给同 session 的读方),也无 dict 树
        # 物化;返回的首元素为 None(批次调用方只消费 created 标志)。
        if payload_json is not None:
            await self._session.execute(
                insert(ResearchRunArtifactModel).values(
                    run_id=run_id,
                    artifact_id=artifact_id,
                    decision_id=decision_id,
                    sequence=sequence,
                    stage=stage,
                    trace_id=trace_id,
                    parent_trace_ids=parent_trace_ids,
                    payload=Json(payload_json, dumps=_identity_json_dumps),
                    checksum=checksum,
                )
            )
            return None, True
        row = ResearchRunArtifactModel(
            run_id=run_id,
            artifact_id=artifact_id,
            decision_id=decision_id,
            sequence=sequence,
            stage=stage,
            trace_id=trace_id,
            parent_trace_ids=parent_trace_ids,
            payload=payload,
            checksum=checksum,
        )
        self._session.add(row)
        await self._session.flush()
        return row, True

    async def list_artifacts(self, run_id: str) -> list[ResearchRunArtifactModel]:
        stmt = (
            select(ResearchRunArtifactModel)
            .where(ResearchRunArtifactModel.run_id == run_id)
            .order_by(ResearchRunArtifactModel.sequence)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def summarize_artifacts(self, run_id: str) -> ResearchRunArtifactSummary:
        """数据库侧聚合 run 摘要计数(#478),任何量级 run 都不取回 payload。

        ``finboard_run_get(view=summary)`` / ``finboard_report_run(view=summary)``
        的口径来源;各字段语义与
        ``finboard_mcp.reporting.summarize_run_artifacts`` 的 Python 逐行
        聚合一致(见各 SQL 常量注释)。``payload`` 列在真实库 / 测试库可能
        是 ``json`` 或 ``jsonb``(泛型 JSON 列),统一 ``::jsonb`` 归一后再用
        jsonb 函数;cast 只作用于 universe / fills 行 —— features 等大
        payload 行被 stage 谓词先行过滤,不进入 detoast(全历史 run 实测
        7203 artifacts / ≈5.9GB JSON,旧全量加载曾把客户端顶到 13GB)。
        """
        artifact_count = int(
            (
                await self._session.execute(
                    _ARTIFACT_COUNT_SQL, {"run_id": run_id}
                )
            ).scalar_one()
        )
        universe_row = (
            await self._session.execute(_UNIVERSE_SUMMARY_SQL, {"run_id": run_id})
        ).one()
        fills_rows = (
            await self._session.execute(_FILLS_SUMMARY_SQL, {"run_id": run_id})
        ).all()
        return ResearchRunArtifactSummary(
            artifact_count=artifact_count,
            universe_total=int(universe_row.total),
            universe_included=int(universe_row.included),
            universe_excluded_by_reason={
                str(reason): int(count)
                for reason, count in (universe_row.excluded_by_reason or {}).items()
            },
            fills_total=sum(int(row.fill_count) for row in fills_rows),
            fills_by_decision={
                str(row.decision_key): int(row.fill_count) for row in fills_rows
            },
        )

    def iter_artifacts(self, run_id: str) -> AsyncIterator[ResearchRunArtifactModel]:
        """按 sequence 流式遍历 artifact 行(#470 前半场)。

        服务端游标 + ``yield_per`` 分块:全历史 run 的 payload 总量达数 GB
        (features 期均 ~8.5MB),一次性 ``list_artifacts`` 物化会在断点续算
        读回时把整个前缀的 payload 钉进内存(272 期前缀实测 ≈8GB)。流式
        读取让消费方(逐决策分组 → 复验 → 重建 bundle → 释放)任意时刻只
        持有 O(1) 个决策的载荷。行序与 ``list_artifacts`` 一致(sequence
        升序)。
        """

        async def _stream() -> AsyncIterator[ResearchRunArtifactModel]:
            stmt = (
                select(ResearchRunArtifactModel)
                .where(ResearchRunArtifactModel.run_id == run_id)
                .order_by(ResearchRunArtifactModel.sequence)
            )
            result = await self._session.stream(
                stmt, execution_options={"yield_per": _ARTIFACT_STREAM_CHUNK}
            )
            async for row in result:
                yield row[0]

        return _stream()

    async def list_artifact_digests(
        self, run_id: str
    ) -> list[tuple[str, str | None, str]]:
        """artifact 瘦指纹(stage, decision_id, checksum),不取 payload 列。

        result_checksum / 拒绝路径归档只消费这三个字段(#470 前半场);列级
        SELECT 避免 run 收尾把全量 payload 再物化一遍(556 期全历史 run
        ≈ 5GB JSON 文本)。行序与 ``list_artifacts`` 一致。
        """

        stmt = (
            select(
                ResearchRunArtifactModel.stage,
                ResearchRunArtifactModel.decision_id,
                ResearchRunArtifactModel.checksum,
            )
            .where(ResearchRunArtifactModel.run_id == run_id)
            .order_by(ResearchRunArtifactModel.sequence)
        )
        rows = (await self._session.execute(stmt)).all()
        return [(stage, decision_id, checksum) for stage, decision_id, checksum in rows]

    async def get_artifact_by_trace(
        self, run_id: str, trace_id: str
    ) -> ResearchRunArtifactModel | None:
        stmt = select(ResearchRunArtifactModel).where(
            ResearchRunArtifactModel.run_id == run_id,
            ResearchRunArtifactModel.trace_id == trace_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    @staticmethod
    def model_payload(row: ResearchRunModel) -> dict[str, Any]:
        return {
            "run_id": row.run_id,
            "status": row.status,
            "manifest": row.manifest,
            "result": row.result,
            "result_checksum": row.result_checksum,
            "error_code": row.error_code,
            "error_summary": row.error_summary,
            "job_id": row.job_id,
        }


__all__ = [
    "ResearchRunArtifactSummary",
    "ResearchRunPersistenceConflictError",
    "ResearchRunRepository",
]
