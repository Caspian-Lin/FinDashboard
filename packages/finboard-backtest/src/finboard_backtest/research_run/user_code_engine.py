"""``user_code`` 策略引擎 —— 沙箱 decide 逐决策接入 #91 targets 管线(issue #218)。

执行模型(v1 逐日决策函数,用户决策 2026-08-28:不做事件驱动 on_bar):

1. 决策日推导 / PIT 特征 / universe 过滤 / 价格序列与协方差:与
   ``multi_factor`` 信号引擎**共用同一加载路径**
   (:func:`build_decision_load_contexts`)—— 决策时点、候选池、执行元数据
   的口径完全一致,报告可同屏对比;
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

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
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
    EquityPoint,
    NormalizedSignal,
    ResearchConstraintViolationError,
    ResearchExecutionMode,
    ResearchRunManifest,
    ResearchRunReport,
    execution_mode_for,
    stable_checksum,
)
from finboard_backtest.research_run.frozen_loader import (
    FeatureSnapshotProvider,
    ReleaseProviderFactory,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _constraints_from_manifest,
    _current_weights_view,
    _PipelineState,
)
from finboard_backtest.research_run.signal_engine import (
    DecisionLoadContext,
    LoadChunkProbe,
    _bars_release_ref,
    _load_benchmark_curve,
    build_daily_equity_curve,
    build_decision_load_contexts,
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
    ) -> None:
        super().__init__(strategy_kind="user_code", decision_inputs=())
        self._manifest = manifest
        self._release_provider_factory = release_provider_factory
        self._snapshot_provider = snapshot_provider
        self._settings_factory = settings_factory
        # issue #306:加载期分块探针(run status / cancel 轮询 + 进度上报)。
        self._chunk_probe = chunk_probe
        self._contexts: tuple[DecisionLoadContext, ...] | None = None
        self._sandbox: StrategySandboxCaller | None = None
        self._decision_records: list[dict[str, Any]] = []
        self._screen_inputs: list[PortfolioDecisionInput] = []
        self._target_weights: list[Mapping[str, float]] = []
        self._strategy_screen: dict[str, Any] | None = None
        self._equity_curve: tuple[EquityPoint, ...] = ()
        self._benchmark_curve: tuple[tuple[date, Decimal], ...] = ()

    @property
    def execution_mode(self) -> ResearchExecutionMode:
        return execution_mode_for(self._manifest.parameters)

    async def _ensure_loaded(self) -> tuple[Any, StrategySandboxCaller]:
        if self._contexts is None or self._sandbox is None:
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
            self._contexts = await build_decision_load_contexts(
                self._manifest,
                release_provider_factory=self._release_provider_factory,
                snapshot_provider=self._snapshot_provider,
                chunk_probe=self._chunk_probe,
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
        return self._contexts, self._sandbox

    async def decisions(
        self,
        manifest: ResearchRunManifest,
    ) -> AsyncIterator[DecisionBundle]:
        contexts, sandbox = await self._ensure_loaded()
        state = _PipelineState(
            cash=manifest.initial_capital,
            equity_high_water=manifest.initial_capital,
        )
        constraints = _constraints_from_manifest(manifest)
        conflict_policy = (
            SignalConflictPolicy.NEUTRALIZE
            if manifest.strategy_spec.signal_rules.conflict_policy
            is SpecSignalConflictPolicy.NEUTRALIZE
            else SignalConflictPolicy.NET
        )
        constraints_echo = _constraints_echo(manifest)
        collected: list[DecisionBundle] = []

        for index, loaded in enumerate(contexts):
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
            self._screen_inputs.append(item)
            try:
                decision = self._build_decision(
                    manifest=manifest,
                    item=item,
                    index=index,
                    state=state,
                    constraints=constraints,
                    allocation_method=_DIRECT_WEIGHTS_METHOD,
                    conflict_policy=conflict_policy,
                    # direct_weights 路径不消费 target_gross(builder 显式
                    # 跳过重缩放);传规格声明值仅为满足管线签名。
                    target_gross=manifest.strategy_spec.portfolio_policy.target_gross_exposure,
                )
            except (AllocationError, SizingError, ValueError) as exc:
                raise ResearchConstraintViolationError(str(exc)) from exc
            yield decision
            collected.append(decision)

        # screen 是治理/展示证据,不改变组合管线的机械结果。若计算因数据
        # 缺失失败,保留带 issues 的空证据,让晋级门明确 fail-closed。
        try:
            from finboard_backtest.research_run.factor_screen import (
                build_strategy_screen,
            )

            self._strategy_screen = await build_strategy_screen(
                manifest,
                self._screen_inputs,
                self._target_weights,
                self._release_provider_factory,
            )
        except Exception as exc:
            logger.warning(
                "user_code.strategy_screen_failed",
                error=str(exc),
                run_id=manifest.run_id,
            )
            self._strategy_screen = {
                "n_periods": len(self._screen_inputs),
                "issues": [f"strategy_screen_computation_failed: {exc}"],
            }

        # 多期回放:决策全部产出后按冻结行情构建每日权益曲线(与信号引擎
        # 同一函数、同一口径,报告可同屏对比);基准曲线两种模式都加载。
        if self.execution_mode is ResearchExecutionMode.MULTI_PERIOD and collected:
            release_ref = _bars_release_ref(manifest, self._release_provider_factory)
            provider = self._release_provider_factory(release_ref.artifact_id)
            self._equity_curve = await build_daily_equity_curve(provider, manifest, collected)
        self._benchmark_curve = await _load_benchmark_curve(
            manifest, self._release_provider_factory
        )

    def build_report(
        self,
        manifest: ResearchRunManifest,
        decisions: Sequence[DecisionBundle],
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
