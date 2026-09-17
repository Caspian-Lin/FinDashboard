"""research_run 断点续算的 artifact 读回与决策重建(issue #314)。

配套 #294 的自动重试:决策级 checkpoint(每决策 13 artifact 落库后 commit)
此前只保证写入幂等去重(artifact_id + checksum),不省计算 —— 重试/恢复时
``decisions=[]`` 从零迭代适配器,全部决策重算。本模块把「已完成决策」从
``research_run_artifacts`` 读回:

* :func:`completed_decision_prefix` 取**从 0 开始的最长连续前缀** —— 每个决策
  必须既有全部 13 个 stage artifact、逐 artifact 复验 ``checksum``,又能无损
  重建为 :class:`DecisionBundle`;任何缺失 / 不一致 / 重建失败都在该决策处
  截断前缀(fail-closed 兜底,宁重算不漂移);
* 重建出的 bundle 交给适配器 :meth:`resume_from` 种子化后**原样产出**,
  coordinator 既有校验(``_validate_decision``)与幂等持久化
  (``_persist_decision`` + artifact 去重)对它们照常生效 —— 重建内容与落库
  内容漂移会命中既有「checkpoint 内容冲突」语义,零新增放行通道。

序列化保真::func:`rebuild_decision` 是 runner ``_persist_decision`` 载荷布局
(``to_json_value``)的逆变换。Decimal 存 str、datetime/date 存 ISO 文本、
枚举存 value、float 经 canonical JSON 最短表示 —— 全部字段逐值可逆;任何
不可逆(未知 stage、缺字段、类型漂移)一律抛错并按截断兜底。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import cast

import structlog

from finboard_backtest.research_run.contracts import (
    CapitalTierOutcome,
    ConstraintOutcome,
    DecisionBundle,
    FeatureValue,
    JsonValue,
    LedgerSnapshot,
    NormalizedSignal,
    RebalanceInstruction,
    ResearchArtifact,
    ResearchFill,
    ResearchFillAction,
    ResearchOrder,
    ResearchOrderStatus,
    ResearchPipelineEvidence,
    ResearchPosition,
    ResearchPositionSide,
    ResearchRiskState,
    ResearchRunStage,
    RiskExitOutcome,
    TargetPosition,
    UniverseCandidate,
    stable_checksum,
)

logger = structlog.get_logger(__name__)

#: 决策级 artifact 的 stage 全集(runner ``_DECISION_STAGES`` 的镜像;顺序无关,
#: 重建按 stage 取载荷。与 runner 的一致性由 ``tests/unit/research_run/
#: test_issue_314_checkpoint_resume.py`` 导入期断言锁定,防两处漂移)。
DECISION_ARTIFACT_STAGES: frozenset[ResearchRunStage] = frozenset(
    {
        ResearchRunStage.UNIVERSE,
        ResearchRunStage.FEATURES,
        ResearchRunStage.SIGNALS,
        ResearchRunStage.TARGETS_BEFORE_CONSTRAINTS,
        ResearchRunStage.CONSTRAINTS,
        ResearchRunStage.TARGETS_AFTER_CONSTRAINTS,
        ResearchRunStage.RISK_EXITS,
        ResearchRunStage.TARGETS_AFTER_RISK,
        ResearchRunStage.CAPITAL_FEASIBILITY,
        ResearchRunStage.REBALANCE_PLAN,
        ResearchRunStage.ORDERS,
        ResearchRunStage.FILLS,
        ResearchRunStage.LEDGER,
    }
)

_DECISION_ARTIFACT_STAGE_COUNT = len(DECISION_ARTIFACT_STAGES)


def decision_artifact_index(run_id: str, artifact_id: str) -> int | None:
    """从 artifact_id 解析决策序号;非决策级 artifact(REPORT 等)返回 None。

    artifact_id 由 runner ``_persist_decision`` 生成:
    ``f"{run_id}:A:{decision_index:08d}:{stage.value}"``。
    """

    prefix = f"{run_id}:A:"
    if not artifact_id.startswith(prefix):
        return None
    index_text = artifact_id[len(prefix) :].split(":", 1)[0]
    if len(index_text) != 8 or not index_text.isdigit():
        return None
    return int(index_text)


def group_decision_artifacts(
    run_id: str,
    artifacts: Sequence[ResearchArtifact],
) -> dict[int, dict[ResearchRunStage, ResearchArtifact]]:
    """按决策序号归组决策级 artifact(忽略 REPORT / 无法解析的行)。"""

    grouped: dict[int, dict[ResearchRunStage, ResearchArtifact]] = {}
    for artifact in artifacts:
        if artifact.run_id != run_id or artifact.decision_id is None:
            continue
        index = decision_artifact_index(run_id, artifact.artifact_id)
        if index is None or artifact.stage not in DECISION_ARTIFACT_STAGES:
            continue
        grouped.setdefault(index, {})[artifact.stage] = artifact
    return grouped


def completed_decision_prefix(
    run_id: str,
    artifacts: Sequence[ResearchArtifact],
) -> list[DecisionBundle]:
    """读回「已完整落库」的决策前缀(issue #314 入口)。

    只返回从 0 开始的最长连续前缀;截断原因(缺 stage / checksum 不一致 /
    重建失败)记 WARNING 后按截断处理 —— 被截断的决策由适配器照常重算,
    既有幂等去重与冲突语义继续兜底。
    """

    grouped = group_decision_artifacts(run_id, artifacts)
    prefix: list[DecisionBundle] = []
    index = 0
    while index in grouped:
        by_stage = grouped[index]
        if len(by_stage) != _DECISION_ARTIFACT_STAGE_COUNT:
            logger.warning(
                "research_run.resume_incomplete_decision",
                run_id=run_id,
                decision_index=index,
                stages=sorted(item.value for item in by_stage),
                message="决策 artifact 不完整,断点续算前缀在该决策处截断",
            )
            break
        try:
            _verify_stage_checksums(run_id, index, by_stage)
            prefix.append(rebuild_decision(run_id, index, by_stage))
        except Exception as exc:
            logger.warning(
                "research_run.resume_readback_failed",
                run_id=run_id,
                decision_index=index,
                error=str(exc),
                message="决策 artifact 读回/重建失败,断点续算前缀在该决策处截断",
            )
            break
        index += 1
    return prefix


async def iter_completed_decision_prefix(
    run_id: str,
    artifacts: AsyncIterator[ResearchArtifact],
) -> AsyncIterator[DecisionBundle]:
    """``completed_decision_prefix`` 的流式形态(#470 前半场)。

    输入须按 ``sequence`` 升序(store 实现保证):决策级 artifact 的
    sequence = 决策序号 x 13 + stage 序,升序流天然按决策分块。逐决策
    「凑齐 13 stage → 复验 checksum → 重建 bundle → yield」,任一时刻
    在途的只有当前决策的 13 份载荷 —— 前缀 payload(全历史 run 数 GB)不再
    全量物化。截断语义与列表版完全一致(缺 stage / checksum 不一致 / 重建
    失败在该决策处截断,记同名 WARNING),截断即停止消费上游流(调用方
    aclose 释放游标)。
    """

    index = 0
    current: dict[ResearchRunStage, ResearchArtifact] = {}

    def _truncate_incomplete(message_index: int) -> None:
        logger.warning(
            "research_run.resume_incomplete_decision",
            run_id=run_id,
            decision_index=message_index,
            stages=sorted(item.value for item in current),
            message="决策 artifact 不完整,断点续算前缀在该决策处截断",
        )

    async for artifact in artifacts:
        if artifact.run_id != run_id or artifact.decision_id is None:
            continue
        row_index = decision_artifact_index(run_id, artifact.artifact_id)
        if row_index is None or artifact.stage not in DECISION_ARTIFACT_STAGES:
            continue
        if row_index < index:
            # 防御:sequence 升序流不应回看已产出决策;真出现即脏行,忽略
            continue
        if row_index > index:
            # 升序流出现跳号 = 当前决策 stage 缺失,截断
            _truncate_incomplete(index)
            return
        current[artifact.stage] = artifact
        if len(current) != _DECISION_ARTIFACT_STAGE_COUNT:
            continue
        try:
            _verify_stage_checksums(run_id, index, current)
            bundle = rebuild_decision(run_id, index, current)
        except Exception as exc:
            logger.warning(
                "research_run.resume_readback_failed",
                run_id=run_id,
                decision_index=index,
                error=str(exc),
                message="决策 artifact 读回/重建失败,断点续算前缀在该决策处截断",
            )
            return
        yield bundle
        index += 1
        current = {}
    if current:
        _truncate_incomplete(index)


def _verify_stage_checksums(
    run_id: str,
    index: int,
    by_stage: dict[ResearchRunStage, ResearchArtifact],
) -> None:
    """逐 artifact 复验载荷校验和(存储损坏的读回防线)。"""

    for stage, artifact in sorted(by_stage.items(), key=lambda item: item[0].value):
        if stable_checksum(artifact.payload) != artifact.checksum:
            raise ValueError(
                f"artifact {artifact.artifact_id}(run={run_id}"
                f" decision#{index} stage={stage.value})载荷校验和不一致"
            )


# ---------------------------------------------------------------------------
# 载荷 → typed DecisionBundle(to_json_value 的逆变换)
# ---------------------------------------------------------------------------


def _mapping(value: object, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是对象,收到 {type(value)!r}")
    return cast(dict[str, JsonValue], value)


def _field(mapping: dict[str, JsonValue], key: str, label: str) -> JsonValue:
    if key not in mapping:
        raise ValueError(f"{label} 缺少字段 {key}")
    return mapping[key]


def _as_str(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是字符串,收到 {type(value)!r}")
    return value


def _as_optional_str(value: object, label: str) -> str | None:
    return None if value is None else _as_str(value, label)


def _as_bool(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} 必须是布尔值,收到 {type(value)!r}")
    return value


def _as_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 必须是整数,收到 {type(value)!r}")
    return value


def _as_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} 必须是数值,收到 {type(value)!r}")
    return float(value)


def _as_optional_float(value: object, label: str) -> float | None:
    return None if value is None else _as_float(value, label)


def _as_decimal(value: object, label: str) -> Decimal:
    return Decimal(_as_str(value, label))


def _as_date(value: object, label: str) -> date:
    return date.fromisoformat(_as_str(value, label))


def _as_datetime(value: object, label: str) -> datetime:
    parsed = datetime.fromisoformat(_as_str(value, label))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} 必须带时区")
    return parsed


def _as_str_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组,收到 {type(value)!r}")
    return tuple(_as_str(item, label) for item in value)


def _as_date_dict(value: object, label: str) -> dict[str, date]:
    return {
        _as_str(key, f"{label} 键"): _as_date(item, f"{label}[{key}]")
        for key, item in _mapping(value, label).items()
    }


def _as_float_dict(value: object, label: str) -> dict[str, float]:
    return {
        _as_str(key, f"{label} 键"): _as_float(item, f"{label}[{key}]")
        for key, item in _mapping(value, label).items()
    }


def rebuild_decision(
    run_id: str,
    decision_index: int,
    by_stage: dict[ResearchRunStage, ResearchArtifact],
) -> DecisionBundle:
    """按 runner ``_persist_decision`` 的载荷布局重建一个决策。"""

    label = f"run={run_id} decision#{decision_index}"

    def stage_payload(stage: ResearchRunStage) -> dict[str, JsonValue]:
        artifact = by_stage[stage]
        if artifact.run_id != run_id:
            raise ValueError(f"{label} artifact 归属 run 不一致")
        return _mapping(artifact.payload, f"{label} {stage.value} payload")

    universe = stage_payload(ResearchRunStage.UNIVERSE)
    features = stage_payload(ResearchRunStage.FEATURES)
    signals = stage_payload(ResearchRunStage.SIGNALS)
    targets_before = stage_payload(ResearchRunStage.TARGETS_BEFORE_CONSTRAINTS)
    constraints = stage_payload(ResearchRunStage.CONSTRAINTS)
    targets_after_constraints = stage_payload(
        ResearchRunStage.TARGETS_AFTER_CONSTRAINTS
    )
    risk_exits = stage_payload(ResearchRunStage.RISK_EXITS)
    targets_after_risk = stage_payload(ResearchRunStage.TARGETS_AFTER_RISK)
    feasibility = stage_payload(ResearchRunStage.CAPITAL_FEASIBILITY)
    plan = stage_payload(ResearchRunStage.REBALANCE_PLAN)
    orders = stage_payload(ResearchRunStage.ORDERS)
    fills = stage_payload(ResearchRunStage.FILLS)
    ledger_stage = stage_payload(ResearchRunStage.LEDGER)

    return DecisionBundle(
        business_date=_as_date(
            _field(universe, "business_date", f"{label} universe"),
            f"{label} business_date",
        ),
        decision_at=_as_datetime(
            _field(universe, "decision_at", f"{label} universe"),
            f"{label} decision_at",
        ),
        candidates=tuple(
            _rebuild_candidate(item, f"{label} candidates[{position}]")
            for position, item in enumerate(
                _require_list(_field(universe, "candidates", f"{label} universe"), label)
            )
        ),
        features=tuple(
            _rebuild_feature(item, f"{label} features[{position}]")
            for position, item in enumerate(
                _require_list(_field(features, "features", f"{label} features"), label)
            )
        ),
        signals=tuple(
            _rebuild_signal(item, f"{label} signals[{position}]")
            for position, item in enumerate(
                _require_list(_field(signals, "signals", f"{label} signals"), label)
            )
        ),
        targets_before_constraints=_rebuild_targets(
            _field(targets_before, "targets", f"{label} targets_before"), label
        ),
        constraints=tuple(
            _rebuild_constraint(item, f"{label} constraints[{position}]")
            for position, item in enumerate(
                _require_list(
                    _field(constraints, "constraints", f"{label} constraints"), label
                )
            )
        ),
        targets_after_constraints=_rebuild_targets(
            _field(targets_after_constraints, "targets", f"{label} targets_after_c"),
            label,
        ),
        risk_exits=tuple(
            _rebuild_risk_exit(item, f"{label} risk_exits[{position}]")
            for position, item in enumerate(
                _require_list(
                    _field(risk_exits, "outcomes", f"{label} risk_exits"), label
                )
            )
        ),
        targets_after_risk=_rebuild_targets(
            _field(targets_after_risk, "targets", f"{label} targets_after_risk"), label
        ),
        risk_state=_rebuild_risk_state(
            _field(risk_exits, "state", f"{label} risk_exits"), label
        ),
        capital_feasibility=tuple(
            _rebuild_tier(item, f"{label} tiers[{position}]")
            for position, item in enumerate(
                _require_list(
                    _field(feasibility, "tiers", f"{label} feasibility"), label
                )
            )
        ),
        rebalance_plan=tuple(
            _rebuild_instruction(item, f"{label} instructions[{position}]")
            for position, item in enumerate(
                _require_list(_field(plan, "instructions", f"{label} plan"), label)
            )
        ),
        orders=tuple(
            _rebuild_order(item, f"{label} orders[{position}]")
            for position, item in enumerate(
                _require_list(_field(orders, "orders", f"{label} orders"), label)
            )
        ),
        fills=tuple(
            _rebuild_fill(item, f"{label} fills[{position}]")
            for position, item in enumerate(
                _require_list(_field(fills, "fills", f"{label} fills"), label)
            )
        ),
        positions=tuple(
            _rebuild_position(item, f"{label} positions[{position}]")
            for position, item in enumerate(
                _require_list(
                    _field(ledger_stage, "positions", f"{label} ledger"), label
                )
            )
        ),
        ledger=_rebuild_ledger(
            _field(ledger_stage, "ledger", f"{label} ledger"), label
        ),
        pipeline_evidence=(
            _rebuild_evidence(
                _field(ledger_stage, "pipeline_evidence", f"{label} ledger"), label
            )
        ),
        decision_id=(
            by_stage[ResearchRunStage.UNIVERSE].decision_id
            or f"{run_id}:D:{decision_index:08d}"
        ),
    )


def _require_list(value: object, label: str) -> list[JsonValue]:
    if not isinstance(value, list):
        raise ValueError(f"{label} 必须是数组,收到 {type(value)!r}")
    return cast(list[JsonValue], value)


def _rebuild_candidate(value: object, label: str) -> UniverseCandidate:
    item = _mapping(value, label)
    return UniverseCandidate(
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        included=_as_bool(_field(item, "included", label), f"{label}.included"),
        reasons=_as_str_list(_field(item, "reasons", label), f"{label}.reasons"),
        asset_class=_as_str(_field(item, "asset_class", label), f"{label}.asset_class"),
        market=_as_str(_field(item, "market", label), f"{label}.market"),
    )


def _rebuild_feature(value: object, label: str) -> FeatureValue:
    item = _mapping(value, label)
    raw_value = _field(item, "value", label)
    return FeatureValue(
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        feature_id=_as_str(_field(item, "feature_id", label), f"{label}.feature_id"),
        value=None if raw_value is None else _as_float(raw_value, f"{label}.value"),
        source_artifact_ids=_as_str_list(
            _field(item, "source_artifact_ids", label), f"{label}.source_artifact_ids"
        ),
        available_at=_as_datetime(
            _field(item, "available_at", label), f"{label}.available_at"
        ),
    )


def _rebuild_signal(value: object, label: str) -> NormalizedSignal:
    item = _mapping(value, label)
    return NormalizedSignal(
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        score=_as_float(_field(item, "score", label), f"{label}.score"),
        action=_as_str(_field(item, "action", label), f"{label}.action"),
        rule_id=_as_str(_field(item, "rule_id", label), f"{label}.rule_id"),
        factor_snapshot_id=_as_optional_str(
            _field(item, "factor_snapshot_id", label), f"{label}.factor_snapshot_id"
        ),
        rationale=_as_str(_field(item, "rationale", label), f"{label}.rationale"),
    )


def _rebuild_targets(value: object, label: str) -> tuple[TargetPosition, ...]:
    return tuple(
        _rebuild_target(item, f"{label}.targets[{position}]")
        for position, item in enumerate(_require_list(value, label))
    )


def _rebuild_target(value: object, label: str) -> TargetPosition:
    item = _mapping(value, label)
    return TargetPosition(
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        weight=_as_float(_field(item, "weight", label), f"{label}.weight"),
        position_side=ResearchPositionSide(
            _as_str(_field(item, "position_side", label), f"{label}.position_side")
        ),
    )


def _rebuild_constraint(value: object, label: str) -> ConstraintOutcome:
    item = _mapping(value, label)
    return ConstraintOutcome(
        constraint=_as_str(_field(item, "constraint", label), f"{label}.constraint"),
        passed=_as_bool(_field(item, "passed", label), f"{label}.passed"),
        before_value=_as_optional_float(
            _field(item, "before_value", label), f"{label}.before_value"
        ),
        after_value=_as_optional_float(
            _field(item, "after_value", label), f"{label}.after_value"
        ),
        limit=_as_optional_float(_field(item, "limit", label), f"{label}.limit"),
        reason=_as_str(_field(item, "reason", label), f"{label}.reason"),
        hard=_as_bool(_field(item, "hard", label), f"{label}.hard"),
        # issue #452:存量 payload 与 symbol=None 的行均无 symbol 键,缺键回退 None。
        symbol=_as_optional_str(item.get("symbol"), f"{label}.symbol"),
    )


def _rebuild_risk_exit(value: object, label: str) -> RiskExitOutcome:
    item = _mapping(value, label)
    return RiskExitOutcome(
        rule_type=_as_str(_field(item, "rule_type", label), f"{label}.rule_type"),
        symbol=_as_optional_str(_field(item, "symbol", label), f"{label}.symbol"),
        triggered=_as_bool(_field(item, "triggered", label), f"{label}.triggered"),
        metric=_as_optional_float(_field(item, "metric", label), f"{label}.metric"),
        threshold=_as_optional_float(
            _field(item, "threshold", label), f"{label}.threshold"
        ),
        before_weight=_as_float(
            _field(item, "before_weight", label), f"{label}.before_weight"
        ),
        after_weight=_as_float(
            _field(item, "after_weight", label), f"{label}.after_weight"
        ),
        reason=_as_str(_field(item, "reason", label), f"{label}.reason"),
    )


def _rebuild_risk_state(value: object, label: str) -> ResearchRiskState:
    item = _mapping(value, label)
    return ResearchRiskState(
        cooldown_until=_as_date_dict(
            _field(item, "cooldown_until", label), f"{label}.cooldown_until"
        ),
        opened_on=_as_date_dict(
            _field(item, "opened_on", label), f"{label}.opened_on"
        ),
        high_water_prices=_as_float_dict(
            _field(item, "high_water_prices", label), f"{label}.high_water_prices"
        ),
        portfolio_equity_high_water=_as_decimal(
            _field(item, "portfolio_equity_high_water", label),
            f"{label}.portfolio_equity_high_water",
        ),
        portfolio_drawdown=_as_float(
            _field(item, "portfolio_drawdown", label), f"{label}.portfolio_drawdown"
        ),
        portfolio_paused=_as_bool(
            _field(item, "portfolio_paused", label), f"{label}.portfolio_paused"
        ),
        state_version=_as_str(
            _field(item, "state_version", label), f"{label}.state_version"
        ),
    )


def _rebuild_tier(value: object, label: str) -> CapitalTierOutcome:
    item = _mapping(value, label)
    return CapitalTierOutcome(
        tier=_as_str(_field(item, "tier", label), f"{label}.tier"),
        capital=_as_decimal(_field(item, "capital", label), f"{label}.capital"),
        feasible=_as_bool(_field(item, "feasible", label), f"{label}.feasible"),
        cash_utilization=_as_float(
            _field(item, "cash_utilization", label), f"{label}.cash_utilization"
        ),
        tracking_error=_as_float(
            _field(item, "tracking_error", label), f"{label}.tracking_error"
        ),
        unfillable_symbols=_as_str_list(
            _field(item, "unfillable_symbols", label), f"{label}.unfillable_symbols"
        ),
        capacity_pressure=_as_float(
            _field(item, "capacity_pressure", label), f"{label}.capacity_pressure"
        ),
        margin_required=_as_decimal(
            _field(item, "margin_required", label), f"{label}.margin_required"
        ),
        estimated_costs=_as_decimal(
            _field(item, "estimated_costs", label), f"{label}.estimated_costs"
        ),
        reasons=_as_str_list(_field(item, "reasons", label), f"{label}.reasons"),
        input_checksum=_as_str(
            _field(item, "input_checksum", label), f"{label}.input_checksum"
        ),
    )


def _rebuild_instruction(value: object, label: str) -> RebalanceInstruction:
    item = _mapping(value, label)
    return RebalanceInstruction(
        instruction_id=_as_str(
            _field(item, "instruction_id", label), f"{label}.instruction_id"
        ),
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        action=ResearchFillAction(
            _as_str(_field(item, "action", label), f"{label}.action")
        ),
        target_quantity=_as_decimal(
            _field(item, "target_quantity", label), f"{label}.target_quantity"
        ),
        current_quantity=_as_decimal(
            _field(item, "current_quantity", label), f"{label}.current_quantity"
        ),
        delta_quantity=_as_decimal(
            _field(item, "delta_quantity", label), f"{label}.delta_quantity"
        ),
        lot_size=_as_int(_field(item, "lot_size", label), f"{label}.lot_size"),
        estimated_value=_as_decimal(
            _field(item, "estimated_value", label), f"{label}.estimated_value"
        ),
        reason=_as_str(_field(item, "reason", label), f"{label}.reason"),
    )


def _rebuild_order(value: object, label: str) -> ResearchOrder:
    item = _mapping(value, label)
    return ResearchOrder(
        research_order_id=_as_str(
            _field(item, "research_order_id", label), f"{label}.research_order_id"
        ),
        instruction_id=_as_str(
            _field(item, "instruction_id", label), f"{label}.instruction_id"
        ),
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        action=ResearchFillAction(
            _as_str(_field(item, "action", label), f"{label}.action")
        ),
        quantity=_as_decimal(_field(item, "quantity", label), f"{label}.quantity"),
        status=ResearchOrderStatus(
            _as_str(_field(item, "status", label), f"{label}.status")
        ),
        reject_reason=_as_optional_str(
            _field(item, "reject_reason", label), f"{label}.reject_reason"
        ),
    )


def _rebuild_fill(value: object, label: str) -> ResearchFill:
    item = _mapping(value, label)
    return ResearchFill(
        research_fill_id=_as_str(
            _field(item, "research_fill_id", label), f"{label}.research_fill_id"
        ),
        research_order_id=_as_str(
            _field(item, "research_order_id", label), f"{label}.research_order_id"
        ),
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        action=ResearchFillAction(
            _as_str(_field(item, "action", label), f"{label}.action")
        ),
        quantity=_as_decimal(_field(item, "quantity", label), f"{label}.quantity"),
        price=_as_decimal(_field(item, "price", label), f"{label}.price"),
        commission=_as_decimal(
            _field(item, "commission", label), f"{label}.commission"
        ),
        tax=_as_decimal(_field(item, "tax", label), f"{label}.tax"),
        slippage=_as_decimal(_field(item, "slippage", label), f"{label}.slippage"),
        filled_at=_as_datetime(
            _field(item, "filled_at", label), f"{label}.filled_at"
        ),
    )


def _rebuild_position(value: object, label: str) -> ResearchPosition:
    item = _mapping(value, label)
    return ResearchPosition(
        symbol=_as_str(_field(item, "symbol", label), f"{label}.symbol"),
        position_side=ResearchPositionSide(
            _as_str(_field(item, "position_side", label), f"{label}.position_side")
        ),
        quantity=_as_decimal(_field(item, "quantity", label), f"{label}.quantity"),
        average_price=_as_decimal(
            _field(item, "average_price", label), f"{label}.average_price"
        ),
        market_price=_as_decimal(
            _field(item, "market_price", label), f"{label}.market_price"
        ),
        market_value=_as_decimal(
            _field(item, "market_value", label), f"{label}.market_value"
        ),
        realized_pnl=_as_decimal(
            _field(item, "realized_pnl", label), f"{label}.realized_pnl"
        ),
        unrealized_pnl=_as_decimal(
            _field(item, "unrealized_pnl", label), f"{label}.unrealized_pnl"
        ),
        margin_used=_as_decimal(
            _field(item, "margin_used", label), f"{label}.margin_used"
        ),
    )


def _rebuild_ledger(value: object, label: str) -> LedgerSnapshot:
    item = _mapping(value, label)
    return LedgerSnapshot(
        cash=_as_decimal(_field(item, "cash", label), f"{label}.cash"),
        market_value=_as_decimal(
            _field(item, "market_value", label), f"{label}.market_value"
        ),
        margin_used=_as_decimal(
            _field(item, "margin_used", label), f"{label}.margin_used"
        ),
        realized_pnl=_as_decimal(
            _field(item, "realized_pnl", label), f"{label}.realized_pnl"
        ),
        unrealized_pnl=_as_decimal(
            _field(item, "unrealized_pnl", label), f"{label}.unrealized_pnl"
        ),
        equity=_as_decimal(_field(item, "equity", label), f"{label}.equity"),
        fees_paid=_as_decimal(_field(item, "fees_paid", label), f"{label}.fees_paid"),
        tax_paid=_as_decimal(_field(item, "tax_paid", label), f"{label}.tax_paid"),
        slippage_paid=_as_decimal(
            _field(item, "slippage_paid", label), f"{label}.slippage_paid"
        ),
        fill_shortfall=_as_decimal(
            _field(item, "fill_shortfall", label), f"{label}.fill_shortfall"
        ),
    )


def _rebuild_evidence(value: object, label: str) -> ResearchPipelineEvidence | None:
    if value is None:
        return None
    item = _mapping(value, label)
    return ResearchPipelineEvidence(
        manifest_input_checksum=_as_str(
            _field(item, "manifest_input_checksum", label),
            f"{label}.manifest_input_checksum",
        ),
        input_checksum=_as_str(
            _field(item, "input_checksum", label), f"{label}.input_checksum"
        ),
        output_checksum=_as_str(
            _field(item, "output_checksum", label), f"{label}.output_checksum"
        ),
        hard_constraints_passed=_as_bool(
            _field(item, "hard_constraints_passed", label),
            f"{label}.hard_constraints_passed",
        ),
        pipeline_version=_as_str(
            _field(item, "pipeline_version", label), f"{label}.pipeline_version"
        ),
    )


__all__ = [
    "DECISION_ARTIFACT_STAGES",
    "completed_decision_prefix",
    "decision_artifact_index",
    "group_decision_artifacts",
    "iter_completed_decision_prefix",
    "rebuild_decision",
]
