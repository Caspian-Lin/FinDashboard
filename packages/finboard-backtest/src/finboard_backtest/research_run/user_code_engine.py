"""``user_code`` 策略引擎 —— 沙箱 decide 逐决策接入 #91 targets 管线(issue #218)。

执行模型(v1 逐日决策函数,用户决策 2026-08-28:不做事件驱动 on_bar):

1. 决策日推导 / PIT 特征 / universe 过滤 / 价格序列与协方差:与
   ``multi_factor`` 信号引擎**共用同一加载路径**
   (:func:`iter_decision_load_contexts`,issue #463 起逐期流式拉取)——
   决策时点、候选池、执行元数据的口径完全一致,报告可同屏对比;
2. 每个决策日:引擎回显**当前组合权重**(上一决策成交后的实际账本状态,
   :func:`_current_weights_view` 只读视图)→ 沙箱一次性容器执行
   ``strategy.decide(ctx)``(PIT 由挂载物理隔离保证,#216 原语)→
   targets 权重映射为 :class:`NormalizedSignal`(score=权重);
3. 组装 ``PortfolioDecisionInput`` 后走父类
   :meth:`PortfolioPipelineAdapter._build_decision`(allocation_method=
   ``direct_weights``):组合硬约束(long-only 负权重截断 / 单资产 / sleeve /
   gross 上限)、风险退出、三档资金可行性、sizing、撮合、账本全部复用
   #91 管线 —— **策略只出目标权重,不触任何订单语义**;
4. report 附 ``sandbox_provenance``(code commit / 镜像 digest / 逐决策
   targets checksum,issue #218 验收)。

越权处理口径(issue #218 需求 3):decide 输出的候选池外 / 缺价格执行
元数据的标的在信号映射层丢弃并记 warning;负权重与超上限权重不在本层
截断 —— 交由组合管线的约束投影逐项审计(``constraints`` 阶段可见
before/after),单一截断权威。

边界:纯离线研究域 —— 不连 broker、不下单、不改持仓;沙箱容器无网络
无凭证。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from typing import Any

import structlog

from finboard_backtest.portfolio.allocators import AllocationError
from finboard_backtest.portfolio.builder import SignalConflictPolicy
from finboard_backtest.portfolio.sizing import SizingError
from finboard_backtest.research_run.contracts import (
    DecisionBundle,
    DecisionLedgerRecord,
    DecisionLedgerView,
    EquityPoint,
    JsonValue,
    NormalizedSignal,
    ResearchConstraintViolationError,
    ResearchExecutionMode,
    ResearchRunManifest,
    ResearchRunReport,
    decision_ledger_record,
    execution_mode_for,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FactorSeriesProvider,
    FeatureSnapshotProvider,
    ReleaseProviderFactory,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _constraints_from_manifest,
    _current_weights_view,
    _execution_model_from_manifest,
    _PipelineState,
    _risk_exit_policy_from_manifest,
)
from finboard_backtest.research_run.signal_engine import (
    DecisionLoadContext,
    LoadChunkProbe,
    LoadPhaseReporter,
    _bars_release_ref,
    _load_benchmark_curve,
    build_daily_equity_curve,
    iter_decision_load_contexts,
)
from finboard_backtest.research_sandbox.strategy_exec import (
    StrategyDecisionOutcome,
    StrategySandboxCaller,
    UserCodeExecutionError,
)
from finboard_backtest.strategy_spec.contracts import (
    SignalConflictPolicy as SpecSignalConflictPolicy,
)

logger = structlog.get_logger(__name__)

#: user_code 决策信号的固定 rule_id / rationale 前缀(审计可辨识)。
_USER_CODE_RULE_ID = "user_code:decide"
_DIRECT_WEIGHTS_METHOD = "direct_weights"


def _constraints_echo(manifest: ResearchRunManifest) -> dict[str, Any]:
    """挂载清单里的约束只读视图(来自 portfolio_policy + overrides)。"""
    constraints = _constraints_from_manifest(manifest)
    return {
        "max_weight_per_asset": float(constraints.max_weight_per_asset),
        "long_only": bool(constraints.long_only),
        "max_gross_exposure": float(constraints.max_leverage),
        "min_cash_buffer": float(constraints.min_cash_buffer),
    }


def targets_to_signals(
    weights: Mapping[str, float],
    signalable: frozenset[str],
    *,
    decision_at: date,
) -> tuple[NormalizedSignal, ...]:
    """decide 输出权重 → 标准化信号(score=权重;池外标的丢弃并记 warning)。

    全部 ``signalable`` 标的都产出信号(未提及 = 权重 0 → 清仓/观望),
    保证输入非空;负权重原样传递,由组合管线按 long_only 截断并审计。
    """
    outside = sorted(set(weights) - set(signalable))
    if outside:
        logger.warning(
            "user_code.targets_outside_signalable",
            decision_date=decision_at.isoformat(),
            symbols=outside[:10],
            count=len(outside),
            message="decide 输出了候选池外/缺价格执行元数据的标的,已丢弃",
        )
    return tuple(
        NormalizedSignal(
            symbol=symbol,
            score=float(weights.get(symbol, 0.0)),
            action="buy" if weights.get(symbol, 0.0) > 0 else "neutral",
            rule_id=_USER_CODE_RULE_ID,
            rationale=(
                f"user_code decide 目标权重 {weights.get(symbol, 0.0):.6f}"
                f"({decision_at.isoformat()};池外标的已丢弃 {len(outside)} 个)"
            ),
        )
        for symbol in sorted(signalable)
    )


class UserCodeStrategyAdapter(PortfolioPipelineAdapter):
    """``user_code`` 规格的沙箱策略适配器(惰性加载,首决策时初始化)。

    工厂同步构造;首个 ``decisions()`` 迭代时才解析代码 / 创建沙箱调用方
    (失败落在 coordinator 异常分流,run 状态正确迁移 FAILED)。
    """

    def __init__(
        self,
        *,
        manifest: ResearchRunManifest,
        release_provider_factory: ReleaseProviderFactory,
        snapshot_provider: FeatureSnapshotProvider,
        settings_factory: Callable[[], Any] | None = None,
        chunk_probe: LoadChunkProbe | None = None,
        series_provider: FactorSeriesProvider | None = None,
        precompute_phase_reporter: LoadPhaseReporter | None = None,
        precompute_cancel_probe: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        super().__init__(strategy_kind="user_code", decision_inputs=())
        self._manifest = manifest
        self._release_provider_factory = release_provider_factory
        self._snapshot_provider = snapshot_provider
        self._settings_factory = settings_factory
        # issue #306:加载期分块探针(run status / job cancel 轮询 + 进度上报)。
        self._chunk_probe = chunk_probe
        # issue #360:因子序列工件读取回调(未声明 series 的 run 为 None,
        # 加载器走纯快照路径,历史行为不变)。
        self._series_provider = series_provider
        # issue #450:预计算段 phase 进度上报器(只写 phase 文本)。
        self._precompute_phase_reporter = precompute_phase_reporter
        # issue #450 追续:预计算段取消探针(只查不报)。
        self._precompute_cancel_probe = precompute_cancel_probe
        # issue #463:流式决策上下文(惰性创建于 _ensure_loaded,逐期拉取,
        # 不再全量物化);决策日与 screen 投影在逐期消费时捕获(#304 partial
        # 证据失败期也在内)。
        self._context_stream: AsyncIterator[DecisionLoadContext] | None = None
        self._context_dates: list[date] = []
        self._sandbox: StrategySandboxCaller | None = None
        self._decision_records: list[dict[str, Any]] = []
        self._screen_periods: list[dict[str, Any]] = []
        self._target_weights: list[Mapping[str, float]] = []
        self._strategy_screen: dict[str, Any] | None = None
        self._equity_curve: tuple[EquityPoint, ...] = ()
        self._benchmark_curve: tuple[tuple[date, Decimal], ...] = ()

    @property
    def execution_mode(self) -> ResearchExecutionMode:
        return execution_mode_for(self._manifest.parameters)

    async def _ensure_loaded(
        self,
    ) -> tuple[AsyncIterator[DecisionLoadContext], StrategySandboxCaller]:
        """惰性初始化:流式决策上下文生成器 + 沙箱调用方(#463)。

        上下文生成器在首次 ``__anext__`` 前不执行任何加载(异步生成器惰性
        语义),加载失败落在决策循环首拉处 —— 与 coordinator 的
        decision_load 失败分流语义一致。调用方负责在消费结束后关闭生成器
        (finally → 特征进程池即时释放)。
        """
        if self._context_stream is None or self._sandbox is None:
            spec = self._manifest.strategy_spec
            if spec.code_artifact is None:
                raise ValueError("user_code 规格 manifest 未冻结 code_artifact(入队路径异常)")
            settings = self._settings_factory() if self._settings_factory is not None else None
            if settings is None:
                raise UserCodeExecutionError(
                    "sandbox_disabled",
                    "user_code 策略执行需要 settings(沙箱镜像/资源限制/代码仓库"
                    "路径);worker 未注入 settings_factory",
                )
            self._context_stream = iter_decision_load_contexts(
                self._manifest,
                release_provider_factory=self._release_provider_factory,
                snapshot_provider=self._snapshot_provider,
                chunk_probe=self._chunk_probe,
                series_provider=self._series_provider,
                precompute_phase_reporter=self._precompute_phase_reporter,
                precompute_cancel_probe=self._precompute_cancel_probe,
            )
            self._sandbox = await StrategySandboxCaller.create(
                settings=settings,
                artifact_name=spec.code_artifact.name,
                commit=spec.code_artifact.commit,
                release_provider_factory=self._release_provider_factory,
                dataset_release_ids=[ref.artifact_id for ref in self._manifest.dataset_releases],
                run_id=self._manifest.run_id,
                params=self._manifest.parameters,
            )
        return self._context_stream, self._sandbox

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        from finboard_backtest.research_run.factor_screen import (
            _period_cross_section,
        )

        context_stream, sandbox = await self._ensure_loaded()
        state = _PipelineState(
            cash=manifest.initial_capital,
            equity_high_water=manifest.initial_capital,
        )
        constraints = _constraints_from_manifest(manifest)
        # issue #303:risk_config.overrides 解析为生效风险退出策略 —— user_code
        # 的 direct_weights 目标同样经风险退出,与 multi_factor 管线同口径。
        try:
            risk_exit_policy = _risk_exit_policy_from_manifest(manifest)
        except ValueError as exc:
            raise ResearchConstraintViolationError(str(exc)) from exc
        # issue #482:fee_config.overrides 解析为生效执行模型 —— user_code 的
        # direct_weights 目标同样经组合管线 sizing / 结算,与 multi_factor
        # 管线同口径。
        try:
            execution_model = _execution_model_from_manifest(manifest)
        except ValueError as exc:
            raise ResearchConstraintViolationError(str(exc)) from exc
        conflict_policy = (
            SignalConflictPolicy.NEUTRALIZE
            if manifest.strategy_spec.signal_rules.conflict_policy
            is SpecSignalConflictPolicy.NEUTRALIZE
            else SignalConflictPolicy.NET
        )
        constraints_echo = _constraints_echo(manifest)
        collected: list[DecisionLedgerRecord] = []
        index = 0
        try:
            # issue #463:逐期拉取决策上下文(不再全量物化),每期流程与
            # 流式化前一致:回显权重 → 沙箱 decide → 权重映射信号 → 组装
            # 输入(捕获 screen 投影与决策日)→ 管线构建 → yield。
            while True:
                try:
                    loaded = await context_stream.__anext__()
                except StopAsyncIteration:
                    break
                context = loaded.context
                # 引擎回显当前权重:decide 看到上一决策成交后的真实账本状态。
                current_weights = _current_weights_view(
                    state, prices=context.prices, lot_info=context.lot_info
                )
                outcome: StrategyDecisionOutcome = await sandbox.decide(
                    decision_index=index,
                    decision_at=context.decision_at,
                    symbols=tuple(sorted(loaded.signalable)),
                    current_weights=current_weights,
                    strategy_constraints=constraints_echo,
                )
                self._decision_records.append(outcome.record)
                self._target_weights.append(dict(outcome.weights))
                signals = targets_to_signals(
                    outcome.weights, loaded.signalable, decision_at=context.business_date
                )
                item = PortfolioDecisionInput(
                    business_date=context.business_date,
                    decision_at=context.decision_at,
                    execution_at=context.execution_at,
                    # issue #254:candidates 语义与 multi_factor 引擎对齐 —— 用
                    # universe 过滤后的候选池(loaded.candidates),不再把发布全
                    # ready 标的(context.candidates,全部 included=True)透传。
                    # decide 输出的池外标的仍由 targets_to_signals 丢弃 + 具名
                    # warning 兜底(#218),但候选池本身受 UniverseSpec 控制。
                    candidates=loaded.candidates,
                    features=loaded.features,
                    signals=signals,
                    prices=context.prices,
                    execution_prices=context.execution_prices,
                    lot_info=context.lot_info,
                    input_artifact_ids=context.input_artifact_ids,
                    covariance=loaded.covariance,
                )
                # issue #463:逐期捕获 screen 投影与决策日(流式化前等价位置
                # 为 _screen_inputs 逐期 append;不再驻留全量输入)。
                self._screen_periods.append(_period_cross_section(item))
                self._context_dates.append(context.business_date)
                try:
                    decision = self._build_decision(
                        manifest=manifest,
                        item=item,
                        index=index,
                        state=state,
                        constraints=constraints,
                        risk_exit_policy=risk_exit_policy,
                        execution_model=execution_model,
                        allocation_method=_DIRECT_WEIGHTS_METHOD,
                        conflict_policy=conflict_policy,
                        # direct_weights 路径不消费 target_gross(builder 显式
                        # 跳过重缩放);传规格声明值仅为满足管线签名。
                        target_gross=manifest.strategy_spec.portfolio_policy.target_gross_exposure,
                    )
                except (AllocationError, SizingError, ValueError) as exc:
                    raise ResearchConstraintViolationError(str(exc)) from exc
                yield decision
                # issue #473:append 账本级记录 —— yield 出去的仍是完整
                # bundle;列表只留落库后的账本投影(equity 曲线 / report
                # 消费审计见 contracts.DecisionLedgerView,candidates 不驻留)。
                collected.append(decision_ledger_record(decision))
                index += 1
        finally:
            # issue #463:上下文生成器随本生成器退出(耗尽 / 关闭 / 抛错)
            # 即关闭,级联收尾加载生成器 → 特征进程池(即时释放)。
            if isinstance(context_stream, AsyncGenerator):
                await context_stream.aclose()

        # screen 是治理/展示证据,不改变组合管线的机械结果。若计算因数据
        # 缺失失败,保留带 issues 的空证据,让晋级门明确 fail-closed。
        # issue #304:组合阶段拒绝时由 compute_partial_evidence 走同一补算。
        await self._compute_strategy_screen(manifest)

        # 多期回放:决策全部产出后按冻结行情构建每日权益曲线(与信号引擎
        # 同一函数、同一口径,报告可同屏对比);基准曲线两种模式都加载。
        if self.execution_mode is ResearchExecutionMode.MULTI_PERIOD and collected:
            release_ref = _bars_release_ref(manifest, self._release_provider_factory)
            provider = self._release_provider_factory(release_ref.artifact_id)
            self._equity_curve = await build_daily_equity_curve(provider, manifest, collected)
        self._benchmark_curve = await _load_benchmark_curve(
            manifest, self._release_provider_factory
        )

    async def _compute_strategy_screen(self, manifest: ResearchRunManifest) -> str | None:
        """user_code 策略 screen(issue #219/#304):尽力而为,幂等。

        成功把结果暂存 ``self._strategy_screen`` 并返回 None;计算异常时记
        具名 warning、保留带 issues 的空证据(让晋级门明确 fail-closed)并
        返回失败原因。已有结果不重算。
        """
        if self._strategy_screen is not None:
            return None
        try:
            from finboard_backtest.research_run.factor_screen import (
                build_strategy_screen_from_periods,
            )

            self._strategy_screen = await build_strategy_screen_from_periods(
                manifest,
                self._screen_periods,
                self._target_weights,
                self._release_provider_factory,
            )
            return None
        except Exception as exc:
            logger.warning(
                "user_code.strategy_screen_failed",
                error=str(exc),
                run_id=manifest.run_id,
            )
            self._strategy_screen = {
                "n_periods": len(self._screen_periods),
                "issues": [f"strategy_screen_computation_failed: {exc}"],
            }
            return f"strategy_screen_computation_failed: {exc}"

    async def compute_partial_evidence(
        self,
        manifest: ResearchRunManifest,
        *,
        completed_decisions: int,
    ) -> dict[str, JsonValue] | None:
        """组合阶段硬约束拒绝后的部分证据补算(issue #304,与信号引擎同构)。

        strategy_screen 的输入(逐期捕获的 ``_screen_periods`` /
        ``_target_weights``,#463:失败期次的投影 / 权重在 ``_build_decision``
        前就已捕获)与组合阶段结果无关;这里基于已捕获的**前缀**数据尽力
        补算并暂存(#463 前缀口径:拒绝路径不重拉任何输入,user_code 流式
        消费本就只捕获到失败期次),``build_report`` 照常携带 —— screen 有
        真实产出时 marker.warnings 追加 ``strategy_screen_prefix_scope`` 具名
        条目说明覆盖 0..N 期而非全期次。返回 partial 标记(失败决策 1-based
        定位 + 补算 warning),尚无已捕获上下文(未开始逐期消费)时返回
        None。
        """
        if not self._context_dates:
            return None
        marker: dict[str, JsonValue] = {
            "completed_decisions": completed_decisions,
            "failed_decision_index": completed_decisions + 1,
        }
        if completed_decisions < len(self._context_dates):
            marker["decision_date"] = self._context_dates[
                completed_decisions
            ].isoformat()
        warnings: list[JsonValue] = []
        screen_failure = await self._compute_strategy_screen(manifest)
        if screen_failure is not None:
            warnings.append(screen_failure)
        elif self._strategy_screen is not None:
            # screen 有真实产出时,其口径是已捕获的前缀期次(0..N)而非
            # 全期次 —— 拒绝路径的证据照实标注(与信号引擎同构)。
            warnings.append(
                {
                    "strategy_screen_prefix_scope": True,
                    "screen_periods": len(self._screen_periods),
                    "message": (
                        "strategy_screen 基于拒绝时已拉取的前缀期次(0..N)计算,"
                        "不代表全期次口径(issue #463:拒绝路径不再重拉输入)"
                    ),
                }
            )
        if warnings:
            marker["warnings"] = warnings
        return marker

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionLedgerView],
        *,
        equity_curve: tuple[EquityPoint, ...] = (),
        benchmark_curve: tuple[tuple[date, Decimal], ...] = (),
    ) -> ResearchRunReport:
        delegate = PortfolioPipelineAdapter(
            strategy_kind=self.strategy_kind,
            decision_inputs=(),
        )
        report = delegate.build_report(
            manifest,
            decisions,
            equity_curve=equity_curve or self._equity_curve,
            benchmark_curve=benchmark_curve or self._benchmark_curve,
        )
        if self._sandbox is not None:
            provenance_header = self._sandbox.provenance_header(
                mode=self.execution_mode.value,
                decision_count=len(self._decision_records),
            )
            provenance = {
                **provenance_header,
                "dataset_release_ids": [ref.artifact_id for ref in manifest.dataset_releases],
                "dataset_release_checksums": {
                    ref.artifact_id: ref.checksum for ref in manifest.dataset_releases
                },
                "parameters": manifest.parameters,
                "parameters_checksum": stable_checksum(manifest.parameters),
                "output_checksum": stable_checksum(
                    [record.get("targets_checksum") for record in self._decision_records]
                ),
                "audit_refs": {
                    "code": {
                        "kind": "strategy",
                        "name": manifest.strategy_spec.code_artifact.name
                        if manifest.strategy_spec.code_artifact
                        else None,
                        "commit": self._sandbox.commit,
                        "checksum": provenance_header.get("code_checksum"),
                    },
                    "data": {
                        "release_ids": [
                            ref.artifact_id for ref in manifest.dataset_releases
                        ],
                        "release_checksums": {
                            ref.artifact_id: ref.checksum
                            for ref in manifest.dataset_releases
                        },
                    },
                    "parameters": {
                        "checksum": stable_checksum(manifest.parameters),
                    },
                    "output": {
                        "checksum": stable_checksum(
                            [
                                record.get("targets_checksum")
                                for record in self._decision_records
                            ]
                        ),
                        "decision_count": len(self._decision_records),
                    },
                    "container": {
                        "image": self._sandbox.image,
                        "image_digest": self._sandbox.image_digest,
                        "archive_dir": provenance_header.get("artifact_dir"),
                        "resource_limits": provenance_header.get("resource_limits", {}),
                    },
                },
                "decisions": self._decision_records,
            }
            report = replace(
                report,
                strategy_screen=self._strategy_screen,
                sandbox_provenance=provenance,
            )
        return report


__all__ = [
    "UserCodeStrategyAdapter",
    "targets_to_signals",
]
