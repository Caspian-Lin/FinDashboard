"""``research_run`` 执行器 —— 把 ResearchRunCoordinator 接入统一队列(issue #143)。

worker 领取 ``kind=research_run`` 的任务后,本执行器从 ``job.payload.run_id`` 重建
``ResearchRunManifest``,委托 :class:`ResearchRunCoordinator.execute` 推进状态机。
Coordinator 内部已内建:

* QUEUED/INTERRUPTED/FAILED 重入(崩溃续跑)
* 每 decision ``checkpoint``(逐 commit,断点可恢复)
* 每 decision 重读 DB status(CANCELLED → 优雅退出,协作式取消)
* 异常分流(unsupported/constraint → REJECTED,interrupted → INTERRUPTED,
  其他 → FAILED)

因此执行器只需做:重建 manifest → 构造 adapter(注入)→ 调 execute → 映射终态。
进度上报(issue #188):把 worker 的 :type:`ProgressCallback` 原样透传给
Coordinator,后者在 ``stage x decision`` 粒度逐阶段回调
``progress(done, total, "research_run:<stage>")``,运行中
``finboard_job_get`` / ``GET /api/jobs/{id}`` 即可区分「正常计算」与「卡死」;
终态仍由本执行器回调 ``research_run:<status>`` 保持兼容。
``NormalizedSignal`` 等策略信号由注入的 ``adapter_factory`` 提供;本期 CLI 用固定
样本 :class:`~finboard_backtest.research_run.adapters.DecisionSequenceAdapter`,
真实「冻结产物 → PortfolioPipelineAdapter」信号引擎留后续 issue。

边界:不连 broker / 不下实盘单 / 不修改持仓;只写 ``research_runs`` 与
``research_run_artifacts`` 表(由 Coordinator 透过 store 写入)。
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_backtest.background_jobs.contracts import (
    ExecutorError,
    JobExecutor,
    JobRecord,
    JobResult,
    ProgressCallback,
    truncate_summary,
)
from finboard_backtest.research_run import (
    REPLAYABLE_SOURCE_STATUSES,
    ArtifactDigest,
    ResearchArtifact,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunRecord,
    ResearchRunReport,
    ResearchRunStatus,
)
from finboard_backtest.research_run.adapters import ResearchStrategyAdapter
from finboard_backtest.research_run.contracts import (
    JsonValue,
    ResearchRunInterruptedError,
)

if TYPE_CHECKING:
    from finboard_backtest.research_run.store import ResearchRunStore

#: 把一个 AsyncSession 包成 ResearchRunStore(注入点,避免 finboard-backtest
#: 反向依赖 finboard-app 的 SqlAlchemyResearchRunStore 实现)。
StoreFactory = Callable[[AsyncSession], "ResearchRunStore"]

_R = TypeVar("_R")

#: 研究运行 store 单操作超时上界(#471):服务端 ``statement_timeout`` 与
#: 客户端 ``wait_for`` 兜底共用该基准(客户端界再放 25% 缓冲,让服务端
#: 的 QueryCanceled 先行——它保持连接可用,客户端取消只作黑洞兜底)。
#: 120s 的理由:研究运行 store 的单操作都是本机(WSL2)PG 上的短读写 ——
#: 逐决策 artifact 批量提交(13 条 JSON,数 MB 级)、manifest / run 行
#: 读写,健康路径毫秒到秒级;#285/#383 实测最重的逐期持久化亦在秒级。
#: 120s ≈ 两个数量级余量,只截杀「永不返回」类挂死(#471 黑洞 / 服务端
#: 悬挂查询),不误伤任何已知的慢而有限的操作。bulk_download 等数据任务
#: **不走本 store**(各自 executor 独立 session),不受该值影响。
DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS = 120.0


class ResearchRunStoreOperationTimeoutError(ResearchRunInterruptedError):
    """研究运行 store 单操作超过上界(#471 活性防线第二层)。

    继承 :class:`ResearchRunInterruptedError`:coordinator 既有异常分流会把
    run 收敛为 INTERRUPTED(而非 FAILED)—— 挂死/黑洞属瞬态基础设施故障,
    attempt 重试按 #314 断点续算语义恢复,与 #450「连接断开只损失当次
    操作」同口径。
    """


def _postgres_statement_timeout_ms(
    session: AsyncSession, timeout_seconds: float
) -> int | None:
    """postgresql 会话返回 statement_timeout 毫秒值;其余方言返回 None。

    SET 只对本会话生效(逐操作短会话即逐操作界),sqlite 等测试后端跳过。
    逐层 getattr 带默认值:测试假会话(#450 桩)可能连 ``sync_session`` 都
    没有 —— 任何缺层都按「非 postgres」处理,不因探测本身抛错。
    """
    sync_session = getattr(session, "sync_session", None)
    bind = getattr(sync_session, "bind", None)
    dialect_name = getattr(getattr(bind, "dialect", None), "name", None)
    if dialect_name != "postgresql":
        return None
    return int(timeout_seconds * 1000)


class SessionPerOperationResearchRunStore:
    """逐操作短会话的 store 包装(#450 追续正确性修复;#471 活性界)。

    此前 executor 把**单一 AsyncSession** 包成 store 交给 coordinator 复用
    整个 run(可达数小时):任一次连接中断(WSL2 转发 PG 长跑实测
    ``SSL SYSCALL error 10053``)都会把该会话事务打进 invalid 态,后续全部
    store 操作 ``PendingRollbackError``——原始错误被掩盖、run 行卡 running
    (attempt 重试撞状态机秒败)、`_safe_terminal_transition` 也救不回来。
    改为逐操作短会话:连接断开只损失当次操作(attempt 重试兜底),存活
    连接由 ``pool_pre_ping`` 检出。写操作在会话关闭前 commit(原 checkpoint()
    的批量提交语义变为逐操作即时持久化,#314 续算以「逐决策 13 stage 全齐
    + checksum 复验」为界,不受影响);``checkpoint()`` 相应变为 no-op。

    #471 双层活性界(挂死签名:协程 await 永不返回、心跳照常续租):
    连接级 —— 引擎工厂默认注入 #450 keepalive(死连接 ~60s 显式报错);
    操作级 —— 每操作会话先 ``SET statement_timeout``(服务端界,慢查询
    在 120s 处以 QueryCanceled 显式失败),再整操作 ``asyncio.timeout``
    客户端兜底(黑洞连接上「查询从未送达服务端」时 statement_timeout
    永不触发,客户端界是唯一上界)。超时统一抛
    :class:`ResearchRunStoreOperationTimeoutError`,走 interrupted 重试语义。
    """

    def __init__(
        self,
        session_maker: async_sessionmaker[AsyncSession],
        store_factory: StoreFactory,
        *,
        operation_timeout_seconds: float = DEFAULT_RESEARCH_STORE_OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self._session_maker = session_maker
        self._store_factory = store_factory
        self._operation_timeout_seconds = operation_timeout_seconds

    async def _with_store(
        self,
        operation_name: str,
        operation: Callable[[ResearchRunStore], Awaitable[_R]],
    ) -> _R:
        client_bound = self._operation_timeout_seconds * 1.25
        try:
            async with asyncio.timeout(client_bound):
                async with self._session_maker() as session:
                    store = self._store_factory(session)
                    timeout_ms = _postgres_statement_timeout_ms(
                        session, self._operation_timeout_seconds
                    )
                    if timeout_ms is not None:
                        await session.execute(
                            text(f"SET statement_timeout = {timeout_ms}")
                        )
                    result = await operation(store)
                    await session.commit()
                    return result
        except TimeoutError as exc:
            raise ResearchRunStoreOperationTimeoutError(
                f"研究运行 store 操作 {operation_name} 超过 "
                f"{self._operation_timeout_seconds:.0f}s 上界"
                f"(客户端兜底 {client_bound:.0f}s 触发;服务端 statement_timeout "
                "未先行报错 —— 疑似连接黑洞或查询未送达,#471)"
            ) from exc

    async def create_or_get(
        self, manifest: ResearchRunManifest
    ) -> tuple[ResearchRunRecord, bool]:
        async def op(store: ResearchRunStore) -> tuple[ResearchRunRecord, bool]:
            return await store.create_or_get(manifest)

        return await self._with_store("create_or_get", op)

    async def get(self, run_id: str) -> ResearchRunRecord | None:
        async def op(store: ResearchRunStore) -> ResearchRunRecord | None:
            return await store.get(run_id)

        return await self._with_store("get", op)

    async def list_by_status(
        self, statuses: Iterable[ResearchRunStatus]
    ) -> list[ResearchRunRecord]:
        async def op(store: ResearchRunStore) -> list[ResearchRunRecord]:
            return await store.list_by_status(statuses)

        return await self._with_store("list_by_status", op)

    async def transition(
        self,
        run_id: str,
        *,
        expected: frozenset[ResearchRunStatus],
        target: ResearchRunStatus,
        error_code: str | None = None,
        error_summary: str | None = None,
    ) -> ResearchRunRecord:
        async def op(store: ResearchRunStore) -> ResearchRunRecord:
            return await store.transition(
                run_id,
                expected=expected,
                target=target,
                error_code=error_code,
                error_summary=error_summary,
            )

        return await self._with_store("transition", op)

    async def save_result(
        self,
        run_id: str,
        *,
        report: ResearchRunReport,
        result_checksum: str,
        timing: dict[str, JsonValue] | None = None,
        partial_failure: dict[str, JsonValue] | None = None,
    ) -> ResearchRunRecord:
        async def op(store: ResearchRunStore) -> ResearchRunRecord:
            return await store.save_result(
                run_id,
                report=report,
                result_checksum=result_checksum,
                timing=timing,
                partial_failure=partial_failure,
            )

        return await self._with_store("save_result", op)

    async def append_artifact(self, artifact: ResearchArtifact) -> bool:
        async def op(store: ResearchRunStore) -> bool:
            return await store.append_artifact(artifact)

        return await self._with_store("append_artifact", op)

    async def append_artifacts(
        self, artifacts: Sequence[ResearchArtifact]
    ) -> list[bool]:
        """整批 artifact 走**同一个**短会话(coordinator 逐决策批量落库,
        2026-09-14 决策段性能:此前 13 artifact = 13 次 session 打开/提交,
        psycopg + connect 占决策墙钟 ~10%)。批内全在或全不在,一次 commit;
        连接断开只损失本决策,attempt 重试按 #314 续算语义截断半截决策。
        """

        async def op(store: ResearchRunStore) -> list[bool]:
            return await store.append_artifacts(artifacts)

        return await self._with_store("append_artifacts", op)

    async def list_artifacts(self, run_id: str) -> list[ResearchArtifact]:
        async def op(store: ResearchRunStore) -> list[ResearchArtifact]:
            return await store.list_artifacts(run_id)

        return await self._with_store("list_artifacts", op)

    def iter_artifacts(self, run_id: str) -> AsyncIterator[ResearchArtifact]:
        """流式遍历 artifact(#470 前半场);会话生命周期覆盖整个迭代。

        与逐操作短会话同构:单一专属会话服务整个流(断点续算读回期间独占),
        迭代结束 / 提前截断(aclose)即关闭 —— 连接断开只损失本次读回,
        调用方(coordinator)按既有语义回退全量重算。#471:流内只设服务端
        ``statement_timeout``(界的是单条 fetch,不是整条流;客户端整流
        ``asyncio.timeout`` 会误杀合法的长读回,活性兜底交给 worker 侧
        stall watchdog)。
        """

        async def _stream() -> AsyncIterator[ResearchArtifact]:
            async with self._session_maker() as session:
                store = self._store_factory(session)
                timeout_ms = _postgres_statement_timeout_ms(
                    session, self._operation_timeout_seconds
                )
                if timeout_ms is not None:
                    await session.execute(text(f"SET statement_timeout = {timeout_ms}"))
                async for artifact in store.iter_artifacts(run_id):
                    yield artifact

        return _stream()

    async def list_artifact_digests(
        self, run_id: str
    ) -> list[ArtifactDigest]:
        async def op(store: ResearchRunStore) -> list[ArtifactDigest]:
            return await store.list_artifact_digests(run_id)

        return await self._with_store("list_artifact_digests", op)

    async def checkpoint(self) -> None:
        """no-op:逐操作短会话已即时持久化(原批量提交边界消失)。"""
        return None

#: 按 manifest 构造策略适配器(注入点)。本期 CLI 提供固定样本工厂;真实工厂
#: 接 FrozenInputLoader + 策略信号引擎后注入。
AdapterFactory = Callable[[ResearchRunManifest], ResearchStrategyAdapter]


class ResearchRunExecutor:
    """``kind=research_run`` 执行器。"""

    def __init__(
        self,
        *,
        session_maker: async_sessionmaker[AsyncSession],
        store_factory: StoreFactory,
        adapter_factory: AdapterFactory,
    ) -> None:
        self._session_maker = session_maker
        self._store_factory = store_factory
        self._adapter_factory = adapter_factory

    async def execute(
        self,
        job: JobRecord,
        progress: ProgressCallback,
    ) -> JobResult:
        run_id = _extract_run_id(job)
        await progress(0, None, "research_run:start")
        store = SessionPerOperationResearchRunStore(self._session_maker, self._store_factory)
        manifest = await _reconstruct_manifest(store, run_id)
        await _converge_stale_running(store, run_id)
        try:
            adapter = self._adapter_factory(manifest)
        except Exception as exc:
            # 伴生缺陷 A(issue #170):适配器工厂失败不能只落在
            # background_jobs 行 —— research_runs 必须同步 FAILED,
            # 否则 finboard_run_get 查不到失败原因。
            await _mark_run_failed(store, run_id, exc)
            raise ExecutorError(
                code=getattr(exc, "code", type(exc).__name__),
                summary=truncate_summary(str(exc)) or type(exc).__name__,
                retryable=False,
            ) from exc
        coordinator = ResearchRunCoordinator(store)
        record = await coordinator.execute(
            manifest,
            adapter,
            # issue #188:worker 的进度回调原样透传,Coordinator 在其
            # stage x decision 持久化路径上逐阶段回调。
            progress=progress,
        )
        await progress(1, 1, f"research_run:{record.status.value}")
        return _record_to_result(record)

    async def execute_replay(self, source_run_id: str) -> JobResult:
        """诊断重放(issue #383):按 #305 replay 语义新建 run,在调用进程内
        同步执行全量计算。

        仅供 ``finboard job-flamegraph`` 的诊断子进程使用 —— 不经 worker
        领取、不写 background_jobs;新 run 继承源 manifest 全部冻结输入,
        血缘标注 ``replay_of_run_id``(completed 源同时得到确定性重放对照,
        interrupted 源即事故恢复通道,均 #305 既有语义)。idempotency_key
        带时间戳:每次诊断重放产生新 run(重复采样不被「新 run 已 COMPLETED
        → execute 短路」挡住)。对 COMPLETED 源 run 走普通 ``execute`` 是
        瞬时 no-op(终态直接返回),采样不到计算 —— 这正是需要本方法的原因。
        源状态 CANCELLED / 不存在等由 replay 守卫 / store 具名拒绝。

        issue #455:适配器以「新 run 身份」的 manifest 预构造(身份字段与
        coordinator.replay 的 replace 同构)—— #306 加载期打断探针闭包按
        构造期 manifest.run_id 轮询,按源 manifest 构造会让重放被自己的
        探针在首块边界杀死(源 run 已终态 ≠ running)。
        """

        from finboard_backtest.research_run import ResearchRunConflictError

        # 同 execute:逐操作短会话(#450 追续),诊断重放同样不受长会话
        # 事务毒化影响。
        store = SessionPerOperationResearchRunStore(self._session_maker, self._store_factory)
        record = await store.get(source_run_id)
        if record is None:
            raise ExecutorError(
                code="missing_research_run",
                summary=f"研究运行 {source_run_id} 不存在,可能已被清理",
                retryable=False,
                context={"run_id": source_run_id},
            )
        try:
            # 幂等键 = 可读时间戳 + uuid 后缀:同一时钟粒度内的两次诊断重放
            # 也不会撞键(撞键会让 create_or_get 返回已 COMPLETED 的新 run,
            # 第二次采样扑空;Windows datetime.now 粒度可达 ~15ms)。
            # issue #455:幂等键 / 新 run_id 必须在适配器构造**之前**生成
            # —— #306 加载期打断探针在适配器工厂闭包内按 manifest.run_id
            # 轮询 run status(signal_engine.build_run_interrupt_probe,
            # 构造于 build_signal_engine_adapter_factory 的 _factory)。
            # 此前适配器按源 manifest 构造,探针闭包捕获的是**源 run id**;
            # coordinator.replay 新建 run 后,探针看到源 run 终态 status
            # != running,重放在加载期首块边界被自己的探针杀死(实测
            # 0.515s 自打断,火焰图只采到进程启动栈)。
            stamp = (
                f"{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}"
            )
            idempotency_key = f"job-flamegraph:{source_run_id}:{stamp}"
            digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
            new_run_id = f"RR-{digest}"
            requested_by = "job-flamegraph"
            if record.status in REPLAYABLE_SOURCE_STATUSES:
                # #455:按 coordinator.replay 的同构字段集预声明新 run 身份,
                # 适配器(及其探针闭包)以「新建 replay run」构造;coordinator
                # 再 replace 一次得到逐字段相同的 manifest。仅身份字段不同,
                # 冻结输入与 result checksum 零影响(replay_source_status
                # 不入 input_checksum,#305)。非可重放源(CANCELLED 等)
                # 不预替换 —— 交给 coordinator.replay 既有守卫具名拒绝,
                # 错误口径与常规通道逐字节一致。
                adapter_manifest = replace(
                    record.manifest,
                    run_id=new_run_id,
                    idempotency_key=idempotency_key,
                    requested_by=requested_by,
                    replay_of_run_id=source_run_id,
                    replay_source_status=record.status.value,
                )
            else:
                adapter_manifest = record.manifest
            adapter = self._adapter_factory(adapter_manifest)
        except Exception as exc:
            raise ExecutorError(
                code=getattr(exc, "code", type(exc).__name__),
                summary=truncate_summary(str(exc)) or type(exc).__name__,
                retryable=False,
            ) from exc
        coordinator = ResearchRunCoordinator(store)
        try:
            new_record = await coordinator.replay(
                source_run_id=source_run_id,
                new_run_id=new_run_id,
                idempotency_key=idempotency_key,
                requested_by=requested_by,
                adapter=adapter,
            )
        except ResearchRunConflictError as exc:
            raise ExecutorError(
                code="replay_guard_rejected",
                summary=str(exc),
                retryable=False,
                context={"run_id": source_run_id},
            ) from exc
        return _record_to_result(new_record)
        return _record_to_result(new_record)


def _extract_run_id(job: JobRecord) -> str:
    raw = job.payload.get("run_id")
    if not isinstance(raw, str) or not raw.startswith("RR-"):
        raise ExecutorError(
            code="invalid_payload",
            summary="research_run 任务 payload 必须包含合法 run_id(RR- 前缀)",
            retryable=False,
            context={"job_id": job.job_id},
        )
    return raw


async def _reconstruct_manifest(
    store: ResearchRunStore,
    run_id: str,
) -> ResearchRunManifest:
    record = await store.get(run_id)
    if record is None:
        raise ExecutorError(
            code="missing_research_run",
            summary=f"研究运行 {run_id} 不存在,可能已被清理",
            retryable=False,
            context={"run_id": run_id},
        )
    return record.manifest


async def _converge_stale_running(
    store: ResearchRunStore,
    run_id: str,
) -> None:
    """把「job 已被本 attempt 领取但 run 行仍 RUNNING」的硬杀残留收敛为
    INTERRUPTED(2026-09-13 事故:worker 被 taskkill 强杀 → lease 过期回收
    → attempt 2 领取后 coordinator 的 RUNNING 重入门禁原样返回未执行
    record → job 以 failed + 空错误码收口,run 行永久悬挂 RUNNING)。

    本执行器必然持有该 job 的 attempt claim:能再次被领取,前次 attempt 的
    lease 必已被 reclaim 过期回收(#161)—— RUNNING 行是硬杀残留而非活
    跃兄弟 worker(#306 属主守卫在 worker 启动扫描时已按当时 lease 保护过;
    reclaim 之后 lease 已过期,不再构成误标风险)。收敛语义与
    ``mark_stale_running_as_interrupted`` 的 process_restart 分支一致:
    coordinator 的 start_states 含 INTERRUPTED → 走 #305/#314 恢复通道续跑。
    """
    record = await store.get(run_id)
    if record is None or record.status is not ResearchRunStatus.RUNNING:
        return
    await store.transition(
        run_id,
        expected=frozenset({ResearchRunStatus.RUNNING}),
        target=ResearchRunStatus.INTERRUPTED,
        error_code="process_restart",
        error_summary=(
            "前次 attempt 的 worker 被强制终止(lease 过期回收),checkpoint"
            " 已保留;本次 attempt 按恢复通道自动续跑"
        ),
    )
    await store.checkpoint()


async def _mark_run_failed(
    store: ResearchRunStore,
    run_id: str,
    exc: Exception,
) -> None:
    """适配器构造失败时把 research_runs 置 FAILED(尽力而为,不掩盖原始错误)。"""
    from finboard_backtest.research_run import (
        ResearchRunConflictError,
        ResearchRunStatus,
    )

    error_code = getattr(exc, "code", type(exc).__name__)
    # 截断保头保尾(issue #263),头部定位上下文与尾部根因收尾均保留。
    error_summary = truncate_summary(str(exc)) or type(exc).__name__
    try:
        record = await store.get(run_id)
        if record is not None and record.status is ResearchRunStatus.QUEUED:
            await store.transition(
                run_id,
                expected=frozenset({ResearchRunStatus.QUEUED}),
                target=ResearchRunStatus.RUNNING,
            )
        await store.transition(
            run_id,
            expected=frozenset({ResearchRunStatus.RUNNING}),
            target=ResearchRunStatus.FAILED,
            error_code=error_code,
            error_summary=error_summary,
        )
        await store.checkpoint()
    except ResearchRunConflictError:
        # 状态已被并发修改(如取消),让 worker 兜底按原始错误收口。
        return


def _record_to_result(
    record: object,
) -> JobResult:
    """把 ``ResearchRunRecord`` 终态映射为 ``JobResult``。"""
    status: ResearchRunStatus = record.status  # type: ignore[attr-defined]
    run_id: str = record.manifest.run_id  # type: ignore[attr-defined]
    if status is ResearchRunStatus.COMPLETED:
        return JobResult(
            status="succeeded",
            result_ref=run_id,
        )
    if status is ResearchRunStatus.CANCELLED:
        return JobResult(status="cancelled", result_ref=run_id)
    if status is ResearchRunStatus.INTERRUPTED:
        return JobResult(
            status="retry_waiting",
            result_ref=run_id,
            error_code="interrupted",
            error_summary=getattr(record, "error_summary", None),
        )
    # FAILED / REJECTED 均视为不可重试失败(coordinator 已完成异常分流)。
    return JobResult(
        status="failed",
        result_ref=run_id,
        error_code=getattr(record, "error_code", None),
        error_summary=getattr(record, "error_summary", None),
    )


def default_store_factory(session: AsyncSession) -> ResearchRunStore:
    """默认 store 工厂:构造 ``SqlAlchemyResearchRunStore``。

    放在此处以便 CLI 直接复用;延迟导入避免 finboard-backtest 顶层依赖 finboard-app。
    """
    from finboard_app.research_run_store import SqlAlchemyResearchRunStore
    from finboard_persistence import ResearchRunRepository

    return SqlAlchemyResearchRunStore(ResearchRunRepository(session))


# JobExecutor 是 runtime_checkable Protocol,直接用类即满足结构子类型。
_: type[JobExecutor] = ResearchRunExecutor

__all__ = [
    "AdapterFactory",
    "ResearchRunExecutor",
    "StoreFactory",
    "default_store_factory",
]
