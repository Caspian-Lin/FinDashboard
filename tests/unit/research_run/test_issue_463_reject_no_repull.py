"""research_run 拒绝路径去除全量重拉(issue #463 下半场收口,#304 前缀口径)。

RR-bff0 活体取证(py-spy 钉死):全市场 556 期 run 在决策 2 被硬约束拒绝后,
``compute_partial_evidence`` 经 ``_ensure_full_captures`` 无条件把
``iter_decision_inputs`` 从头重迭代一遍 —— 重建 loader / close 矩阵 / 价格
特征预计算(run 级结构双份驻留),556 期单核串行要 3-6 小时,期间 phase
指纹冻结会撞 #306 僵尸击杀形成「重试 → 再拒绝 → 再重拉」死循环;而该 run
无 u_ 用户因子,screen 产物为 None,重拉毫无产出。新语义:

* 证据一律来自**已拉取的前缀捕获**(PR #467 拉取即登记,失败期次在内),
  拒绝即时 finalize —— ``iter_decision_inputs`` /
  ``iter_decision_load_contexts`` 不再第二次进入(计数器断言);
* marker 的 ``decision_date`` = 失败期日期(前缀捕获含失败期);
* 引用 u_ 因子的 run(screen 有真实产出)screen 按前缀口径产出,marker
  warnings 具名标注 ``factor_screen_prefix_scope``;无 u_ 因子 screen 为
  None,零额外动作;
* ``_fast_path`` / 空捕获守卫行为不变。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

import finboard_backtest.research_run.signal_engine as signal_engine_module
from finboard_backtest.research_run import (
    InMemoryResearchRunStore,
    ResearchRunCoordinator,
    ResearchRunStatus,
)
from finboard_backtest.research_run.contracts import ResearchRunManifest
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
)
from finboard_backtest.research_run.user_code_engine import UserCodeStrategyAdapter

from .test_issue_304_partial_evidence import (
    DECISION_AT,
    DECISION_AT_2,
    SYMBOLS,
    U_FACTOR,
    _adapter,
    _closes,
    _closes_provider,
    _manifest,
    _spec,
)
from .test_signal_engine import _obs, _StubInstrument, _StubProvider, _StubRelease, _StubSnapshot

pytestmark = pytest.mark.asyncio

#: 决策阶段数(期 1 完整落库的 stage artifact 数,#304 同口径)
_DECISION_STAGES = 13
#: C/D/E/F 的退市日 —— 落在两期决策日之间(期 1 在市、期 2 被
#: ``exclude_delisted`` 剔出候选池)
_DELIST_DATE = date(2024, 1, 15)


def _snapshot(
    snapshot_id: str,
    decision_at: datetime,
    *,
    alpha_seed: float | None,
) -> _StubSnapshot:
    """固定形态快照(与 #304 ``_snapshot`` 同形状)。

    ``alpha_seed`` 非 None 时附带 u_ 用户因子观测(screen 有真实产出的 run,
    无 u_ 因子时 screen 产物为 None)。
    """

    at = datetime(2023, 12, 29, tzinfo=UTC)
    observations = []
    for index, symbol in enumerate(SYMBOLS):
        observations.append(_obs(symbol, "pb", 1.0 + index * 0.1, available_at=at))
        observations.append(_obs(symbol, "momentum", 0.05 * index, available_at=at))
        observations.append(
            _obs(symbol, "volatility_20d", 0.3 - 0.03 * index, available_at=at)
        )
        if alpha_seed is not None:
            observations.append(
                _obs(symbol, U_FACTOR, alpha_seed - index, available_at=at)
            )
    return _StubSnapshot(
        snapshot_id=snapshot_id,
        decision_at=decision_at,
        observations=tuple(observations),
    )


def _delisting_provider(*, n_days: int = 60) -> _StubProvider:
    """后 4 只标的在两期之间退市的 stub 发布。

    期 2(2024-02-01)universe 的 ``exclude_delisted`` 把它们剔出候选池:
    rank 分母仍是特征截面 6 只,但信号只对 included 产出 —— 复合得分
    头部 A/B/C 中 C 已不在市,买池收窄到 2 只,等权 1/2 超过
    ``max_risk_contribution`` 0.35 数学不可行(#304 同失败形态;n=2 时
    至少一只的风险贡献 ≥ 1/2,确定性触发)。快照缺观测收不缩池:
    single_shot 的 PIT 观测按「最新可见」合并,期 1 快照的观测在期 2
    决策时点仍然可见;universe 元数据(delist_date)才是逐期生效的闸门。"""
    return _StubProvider(
        release=_StubRelease(
            "release-v1",
            tuple(
                _StubInstrument(code=symbol, delist_date=_DELIST_DATE)
                if symbol in SYMBOLS[2:]
                else _StubInstrument(code=symbol)
                for symbol in SYMBOLS
            ),
        ),
        closes_by_symbol=_closes(n_days=n_days),
    )


class _PullCounter:
    """monkeypatch 计数包装:``iter_decision_inputs`` /
    ``iter_decision_load_contexts`` 工厂入口次数(每次进入 = 一次输入流
    拉取的启动;拒绝路径的证据补算不得再启动第二次)。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.inputs_entries = 0
        self.contexts_entries = 0
        real_inputs = signal_engine_module.iter_decision_inputs
        real_contexts = signal_engine_module.iter_decision_load_contexts
        counter = self

        def counting_inputs(*args: Any, **kwargs: Any) -> Any:
            counter.inputs_entries += 1
            return real_inputs(*args, **kwargs)

        def counting_contexts(*args: Any, **kwargs: Any) -> Any:
            counter.contexts_entries += 1
            return real_contexts(*args, **kwargs)

        monkeypatch.setattr(
            signal_engine_module, "iter_decision_inputs", counting_inputs
        )
        monkeypatch.setattr(
            signal_engine_module, "iter_decision_load_contexts", counting_contexts
        )


async def _reject_at_second_period(
    *,
    alpha_seed: float | None,
    run_id: str,
    idempotency_key: str,
) -> tuple[
    Any,
    InMemoryResearchRunStore,
    ResearchRunManifest,
    SignalEnginePipelineAdapter,
]:
    """真实 coordinator 端到端:期 1 正常构建落库,期 2 硬约束拒绝。

    期 2 时点 C/D/E/F 已退市 —— universe ``exclude_delisted`` 剔出后买池
    收窄到 2 只,风险贡献约束数学不可行;失败发生在输入已拉取(前缀捕获
    含期 2)、决策未产出之时。"""
    spec = _spec(rank_threshold=0.5)
    manifest = _manifest(
        spec,
        run_id=run_id,
        idempotency_key=idempotency_key,
        snapshot_ids=("factor-v1", "factor-v2"),
    )
    snapshots = {
        "factor-v1": _snapshot("factor-v1", DECISION_AT, alpha_seed=alpha_seed),
        "factor-v2": _snapshot(
            "factor-v2",
            DECISION_AT_2,
            alpha_seed=None if alpha_seed is None else alpha_seed - 3.0,
        ),
    }
    adapter = _adapter(manifest, _delisting_provider(), snapshots)
    store = InMemoryResearchRunStore()
    record = await ResearchRunCoordinator(store).execute(manifest, adapter)
    return record, store, manifest, adapter


class TestRejectNoRepull:
    async def test_repull_helper_is_gone(self) -> None:
        """结构性守卫:全量重拉方法与状态字段已删除(grep 全仓无消费点)。"""
        assert not hasattr(SignalEnginePipelineAdapter, "_ensure_full_captures")
        assert not hasattr(SignalEnginePipelineAdapter, "_captures_complete")

    async def test_no_user_factor_rejection_finalizes_without_repull(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """核心回归:无 u_ 因子的 run 期 2 被拒 → 拒绝即时 finalize,
        输入流只拉取一次(证据来自前缀捕获,无第二次进入)。"""
        counter = _PullCounter(monkeypatch)
        record, store, manifest, adapter = await _reject_at_second_period(
            alpha_seed=None,
            run_id="RR-issue463norepu01",
            idempotency_key="issue463-norepull",
        )

        # 终态语义(#91 fail-closed 不动):期 1 完整落库 + partial report。
        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"
        artifacts = await store.list_artifacts(manifest.run_id)
        stages = [item.stage.value for item in artifacts]
        assert stages.count("report") == 1
        assert len(stages) == _DECISION_STAGES + 1  # 期 1 的 13 stage + report

        # marker 定位失败期:前缀捕获含失败期,decision_date = 期 2 日期。
        report_payload = artifacts[-1].payload["report"]
        assert isinstance(report_payload, dict)
        assert report_payload["partial"] is True
        failure = report_payload["constraint_failure"]
        assert isinstance(failure, dict)
        assert failure["completed_decisions"] == 1
        assert failure["failed_decision_index"] == 2
        assert failure["decision_date"] == DECISION_AT_2.date().isoformat()
        # 无 u_ 因子:screen 为 None,marker 无任何 warnings(含 prefix_scope)。
        assert "warnings" not in failure
        assert record.result is not None
        assert record.result.factor_screen is None
        assert adapter._factor_screen is None

        # 核心:输入流工厂只进入一次 —— 拒绝路径没有第二次全量拉取。
        assert counter.inputs_entries == 1
        assert counter.contexts_entries == 1

    async def test_user_factor_rejection_keeps_prefix_scope_screen(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """u_ 因子分档:screen 按前缀口径产出(含失败期),warnings 具名
        标注 factor_screen_prefix_scope;同样没有第二次拉取。"""
        counter = _PullCounter(monkeypatch)
        record, store, manifest, adapter = await _reject_at_second_period(
            alpha_seed=6.0,
            run_id="RR-issue463upfx0001",
            idempotency_key="issue463-user-prefix",
        )

        assert record.status is ResearchRunStatus.REJECTED
        assert record.error_code == "hard_constraint_rejected"

        artifacts = await store.list_artifacts(manifest.run_id)
        report_payload = artifacts[-1].payload["report"]
        assert isinstance(report_payload, dict)
        failure = report_payload["constraint_failure"]
        assert isinstance(failure, dict)
        assert failure["completed_decisions"] == 1
        assert failure["failed_decision_index"] == 2
        assert failure["decision_date"] == DECISION_AT_2.date().isoformat()

        # screen 有真实产出:前缀口径 = 期 1 + 失败期 2,非全期次不存在歧义。
        warnings = failure.get("warnings")
        assert isinstance(warnings, list)
        prefix_scopes = [
            item
            for item in warnings
            if isinstance(item, dict) and item.get("factor_screen_prefix_scope")
        ]
        assert len(prefix_scopes) == 1
        assert prefix_scopes[0]["screen_periods"] == 2
        assert record.result is not None
        assert record.result.factor_screen is not None
        assert record.result.factor_screen["n_periods"] == 2
        factors = record.result.factor_screen["factors"]
        assert isinstance(factors, dict)
        assert U_FACTOR in factors
        assert adapter._factor_screen is record.result.factor_screen

        # 无第二次拉取(u_ 分档同样不重拉)。
        assert counter.inputs_entries == 1
        assert counter.contexts_entries == 1

    async def test_fast_path_and_empty_capture_guards_unchanged(self) -> None:
        """守卫行为不变:#314 快速路径与空捕获(尚未拉取任何输入)均返回
        None —— 拒绝路径零证据,与既有语义一致。"""
        spec = _spec(rank_threshold=0.5)
        manifest = _manifest(
            spec,
            run_id="RR-issue463guard001",
            idempotency_key="issue463-guards",
        )
        snapshots = {"factor-v1": _snapshot("factor-v1", DECISION_AT, alpha_seed=6.0)}

        fresh = _adapter(manifest, _closes_provider(), snapshots)
        # 空捕获:输入流从未启动。
        assert fresh._business_dates == []
        assert (
            await fresh.compute_partial_evidence(manifest, completed_decisions=0)
            is None
        )
        assert fresh._input_iterator is None

        fast_path = _adapter(manifest, _closes_provider(), snapshots)
        fast_path._fast_path = True
        assert (
            await fast_path.compute_partial_evidence(manifest, completed_decisions=0)
            is None
        )


class TestUserCodePrefixScope:
    async def test_user_code_marker_carries_prefix_scope(self, manifest_factory) -> None:
        """user_code 同构:screen 有真实产出时 marker.warnings 具名标注
        strategy_screen_prefix_scope(前缀口径可见,该路径本就无重拉)。"""
        from finboard_backtest.research_run.factor_screen import _period_cross_section

        manifest = manifest_factory("user_code")
        adapter = UserCodeStrategyAdapter(
            manifest=manifest,
            release_provider_factory=lambda _id: _closes_provider(),  # type: ignore[arg-type]
            snapshot_provider=None,  # type: ignore[arg-type]
        )
        # 直接注入逐期捕获产物(等价决策循环对前两期的捕获:失败期次在
        # _build_decision 前已登记,#463 拉取即捕获)。
        adapter._context_dates = [DECISION_AT.date(), DECISION_AT_2.date()]
        adapter._screen_periods = [
            _period_cross_section(_user_code_input(DECISION_AT)),
            _period_cross_section(_user_code_input(DECISION_AT_2)),
        ]
        adapter._target_weights = [
            {SYMBOLS[0]: 0.5, SYMBOLS[1]: 0.5},
            {SYMBOLS[-1]: 0.4, SYMBOLS[-2]: 0.4},
        ]

        marker = await adapter.compute_partial_evidence(manifest, completed_decisions=1)

        assert marker is not None
        assert marker["failed_decision_index"] == 2
        assert marker["decision_date"] == DECISION_AT_2.date().isoformat()
        warnings = marker.get("warnings")
        assert isinstance(warnings, list)
        prefix_scopes = [
            item
            for item in warnings
            if isinstance(item, dict) and item.get("strategy_screen_prefix_scope")
        ]
        assert len(prefix_scopes) == 1
        assert prefix_scopes[0]["screen_periods"] == 2
        assert adapter._strategy_screen is not None


def _user_code_input(decision_at: datetime) -> Any:
    """user_code screen 投影所需的最小决策输入(#304 user_code 用例同形状)。"""

    from finboard_backtest.portfolio.contracts import AssetLotInfo
    from finboard_backtest.research_run.contracts import (
        FeatureValue,
        NormalizedSignal,
        UniverseCandidate,
    )
    from finboard_backtest.research_run.portfolio_pipeline import (
        PortfolioDecisionInput,
    )

    features = tuple(
        FeatureValue(
            symbol=symbol,
            feature_id=U_FACTOR,
            value=6.0 - index,
            source_artifact_ids=("factor-v1",),
            available_at=decision_at,
        )
        for index, symbol in enumerate(SYMBOLS)
    )
    return PortfolioDecisionInput(
        business_date=decision_at.date(),
        decision_at=decision_at,
        execution_at=decision_at.replace(hour=16),
        candidates=tuple(
            UniverseCandidate(symbol, True, ("ok",), "equity", "a_share")
            for symbol in SYMBOLS
        ),
        features=features,
        signals=tuple(
            NormalizedSignal(
                symbol, 1.0, "buy", "user_code:decide", rationale="t"
            )
            for symbol in SYMBOLS
        ),
        prices=dict.fromkeys(SYMBOLS, 10.0),
        execution_prices=dict.fromkeys(SYMBOLS, 10.0),
        lot_info={symbol: AssetLotInfo(code=symbol) for symbol in SYMBOLS},
        input_artifact_ids=("factor-v1",),
    )


__all__ = [
    "TestRejectNoRepull",
    "TestUserCodePrefixScope",
]
