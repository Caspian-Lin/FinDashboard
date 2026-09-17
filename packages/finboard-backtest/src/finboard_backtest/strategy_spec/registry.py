"""统一研究策略注册表与 #60-#64 兼容模板。"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from typing import Any

from finboard_backtest.convertible_double_low.config import (
    CONVERTIBLE_DOUBLE_LOW_VERSION,
)
from finboard_backtest.etf_rotation.config import ETF_ROTATION_VERSION
from finboard_backtest.futures_tsmom.config import FUTURES_TSMOM_VERSION
from finboard_backtest.mean_reversion.config import MEAN_REVERSION_VERSION
from finboard_backtest.strategy_spec.compiler import (
    ResolvedStrategyPlan,
    compile_strategy_spec,
)
from finboard_backtest.strategy_spec.contracts import (
    AllocationMethod,
    ConstraintName,
    ExecutionModel,
    FeatureGraph,
    FeatureKind,
    FeatureNode,
    FeatureOperator,
    LegacyCompatibility,
    PortfolioConstraintRef,
    PortfolioPolicy,
    RankingDirection,
    ResearchStrategySpec,
    RiskExitPolicy,
    RiskExitRule,
    RiskExitType,
    SignalAction,
    SignalComparator,
    SignalRule,
    SignalRules,
    StrategySpecError,
    UniverseRanking,
    UniverseSpec,
    ValidationPlanSpec,
)
from finboard_shared.types import AssetClass, Market


@dataclass(frozen=True, slots=True)
class StrategyCapability:
    kind: str
    name: str
    description: str
    issue: int
    asset_classes: tuple[AssetClass, ...]
    supports_short: bool
    supports_no_code_template: bool = True
    produces_target_weights: bool = True
    can_execute_on_publish: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "issue": self.issue,
            "asset_classes": [item.value for item in self.asset_classes],
            "supports_short": self.supports_short,
            "supports_no_code_template": self.supports_no_code_template,
            "produces_target_weights": self.produces_target_weights,
            "can_execute_on_publish": self.can_execute_on_publish,
        }


_REGISTRY: dict[str, StrategyCapability] = {
    "multi_factor": StrategyCapability(
        kind="multi_factor",
        name="多因子选股",
        description="价值、质量、低风险、流动性、动量及市场输入的复合评分。",
        issue=60,
        asset_classes=(AssetClass.EQUITY,),
        supports_short=False,
    ),
    "etf_rotation": StrategyCapability(
        kind="etf_rotation",
        name="多资产 ETF 轮动",
        description="绝对趋势、相对动量和逆波动率配权。",
        issue=61,
        asset_classes=(
            AssetClass.EQUITY,
            AssetClass.FIXED_INCOME,
            AssetClass.COMMODITY,
        ),
        supports_short=False,
    ),
    "mean_reversion": StrategyCapability(
        kind="mean_reversion",
        name="ETF 均值回归",
        description="带市场状态过滤、持有期和冷却期的短周期均值回归。",
        issue=62,
        asset_classes=(AssetClass.EQUITY,),
        supports_short=False,
    ),
    "convertible_double_low": StrategyCapability(
        kind="convertible_double_low",
        name="可转债双低",
        description="价格、溢价和事件风险约束的可转债组合。",
        issue=63,
        asset_classes=(AssetClass.CONVERTIBLE,),
        supports_short=False,
    ),
    "futures_tsmom": StrategyCapability(
        kind="futures_tsmom",
        name="期货时间序列动量",
        description="多空趋势信号、波动率缩放、保证金和展期约束。",
        issue=64,
        asset_classes=(AssetClass.DERIVATIVE,),
        supports_short=True,
    ),
    "ma_cross": StrategyCapability(
        kind="ma_cross",
        name="双均线交叉",
        description="现有均线策略的无代码研究规格兼容模板。",
        issue=29,
        asset_classes=(AssetClass.EQUITY,),
        supports_short=False,
    ),
    "user_code": StrategyCapability(
        kind="user_code",
        name="用户代码策略(沙箱 decide)",
        description=(
            "agent 提交的策略代码经沙箱逐决策日执行 decide(ctx) → 目标权重,"
            "输出复用统一组合风控/资金可行性/撮合管线;代码引用 research_code "
            "artifact(kind=strategy,须 active)。"
        ),
        issue=218,
        asset_classes=(AssetClass.EQUITY,),
        supports_short=False,
        # 代码本体走 MCP 研究代码通道(#215),web 无代码模板不适用。
        supports_no_code_template=False,
        produces_target_weights=True,
    ),
}


def list_strategy_capabilities() -> tuple[StrategyCapability, ...]:
    return tuple(_REGISTRY.values())


def get_strategy_capability(kind: str) -> StrategyCapability:
    capability = _REGISTRY.get(kind)
    if capability is None:
        raise StrategySpecError(f"未注册的研究策略类型: {kind}")
    return capability


def compile_registered_strategy_spec(
    raw: ResearchStrategySpec | dict[str, Any],
    *,
    disabled_factors: frozenset[str] = frozenset(),
    available_dataset_release_ids: frozenset[str] | None = None,
    user_factor_sources: Collection[str] = frozenset(),
    user_code_sources: Collection[str] = frozenset(),
) -> ResolvedStrategyPlan:
    plan = compile_strategy_spec(
        raw,
        disabled_factors=disabled_factors,
        available_dataset_release_ids=available_dataset_release_ids,
        user_factor_sources=user_factor_sources,
        user_code_sources=user_code_sources,
    )
    capability = get_strategy_capability(plan.spec.strategy_kind)
    if not set(plan.spec.universe.asset_classes).issubset(capability.asset_classes):
        raise StrategySpecError(
            f"{capability.kind} 不支持资产类别: "
            f"{sorted(item.value for item in plan.spec.universe.asset_classes)}"
        )
    if (
        SignalAction.SELL in plan.spec.portfolio_policy.investable_actions
        and not capability.supports_short
    ):
        raise StrategySpecError(f"{capability.kind} 不支持把 sell 信号映射为空头目标仓位")
    return plan


def build_strategy_template(
    kind: str,
    *,
    strategy_id: str,
    dataset_release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    """把现有研究框架的默认配置适配成统一规格。"""

    get_strategy_capability(kind)
    builders = {
        "multi_factor": _multi_factor_template,
        "etf_rotation": _etf_rotation_template,
        "mean_reversion": _mean_reversion_template,
        "convertible_double_low": _convertible_template,
        "futures_tsmom": _futures_template,
        "ma_cross": _ma_cross_template,
    }
    spec = builders[kind](strategy_id, dataset_release_ids)
    compile_registered_strategy_spec(spec)
    return spec


def _validation(release_ids: tuple[str, ...]) -> ValidationPlanSpec:
    return ValidationPlanSpec(
        dataset_release_ids=release_ids,
        train_start=date(2018, 1, 1),
        train_end=date(2021, 12, 31),
        validation_start=date(2022, 1, 1),
        validation_end=date(2022, 12, 31),
        test_start=date(2023, 1, 1),
        test_end=date(2024, 12, 31),
        benchmark_symbol="510300.SH",
    )


def _constraint(
    name: ConstraintName,
    rationale: str,
    limit: float | None = None,
) -> PortfolioConstraintRef:
    return PortfolioConstraintRef(name=name, limit=limit, rationale=rationale)


def _risk_policy(
    *,
    max_holding_days: int | None = None,
    cooldown_days: int | None = None,
) -> RiskExitPolicy:
    return RiskExitPolicy(
        rules=(
            RiskExitRule(
                rule_type=RiskExitType.PRICE_STOP_LOSS,
                enabled=False,
                rationale="默认关闭个券固定止损。启用前须在样本外验证阈值稳定性。",
            ),
            RiskExitRule(
                rule_type=RiskExitType.VOLATILITY_STOP,
                enabled=False,
                rationale="默认关闭波动止损。避免未验证的追涨杀跌。",
            ),
            RiskExitRule(
                rule_type=RiskExitType.TAKE_PROFIT,
                enabled=False,
                rationale="默认关闭固定止盈。由反向信号退出。",
            ),
            RiskExitRule(
                rule_type=RiskExitType.MAX_HOLDING_DAYS,
                enabled=max_holding_days is not None,
                days=max_holding_days,
                rationale=(
                    "限制短周期信号暴露时间。"
                    if max_holding_days is not None
                    else "趋势策略不设固定持有期。由反向信号退出。"
                ),
            ),
            RiskExitRule(
                rule_type=RiskExitType.PORTFOLIO_DRAWDOWN_DERISK,
                enabled=True,
                threshold=0.15,
                target_gross_exposure=0.5,
                rationale="组合回撤达到 15% 时把研究目标总敞口降至 50%。",
            ),
            RiskExitRule(
                rule_type=RiskExitType.COOLDOWN,
                enabled=cooldown_days is not None,
                days=cooldown_days,
                rationale=(
                    "退出后设置冷却期。避免频繁反复开仓。"
                    if cooldown_days is not None
                    else "默认不启用冷却期。由调仓频率控制交易节奏。"
                ),
            ),
        )
    )


def _long_only_policy(
    method: AllocationMethod,
    *,
    max_positions: int,
    max_weight: float,
    extra_constraints: tuple[PortfolioConstraintRef, ...] = (),
) -> PortfolioPolicy:
    return PortfolioPolicy(
        allocation_method=method,
        max_positions=max_positions,
        max_target_weight=max_weight,
        constraint_refs=(
            _constraint(ConstraintName.LONG_ONLY, "研究框架只允许多头目标权重。"),
            _constraint(ConstraintName.LOT_SIZE, "目标仓位须通过资产手数可行性检查。"),
            _constraint(ConstraintName.LIQUIDITY, "目标仓位受成交量参与率约束。", 0.1),
            _constraint(ConstraintName.TURNOVER, "单日目标换手上限。", 0.3),
            *extra_constraints,
        ),
    )


def _universe(
    *,
    asset_classes: tuple[AssetClass, ...],
    selection_limit: int,
    ranking_field: str | None = None,
    ranking_direction: RankingDirection = RankingDirection.TOP,
    min_amount: float = 5_000_000,
    min_price: float | None = None,
    max_price: float | None = None,
    events: tuple[str, ...] = (),
    required_data_fields: tuple[str, ...] = ("price", "average_amount"),
) -> UniverseSpec:
    return UniverseSpec(
        markets=(Market.FUTURE,) if asset_classes == (AssetClass.DERIVATIVE,) else (Market.A_SHARE,),
        asset_classes=asset_classes,
        min_average_amount=None if min_amount == 0 else min_amount,
        min_price=min_price,
        max_price=max_price,
        excluded_event_types=events,
        ranking=(
            UniverseRanking(field=ranking_field, direction=ranking_direction)
            if ranking_field is not None
            else None
        ),
        selection_limit=selection_limit,
        required_data_fields=required_data_fields,
    )


def _multi_factor_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="pb",
            label="市净率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="pb",
        ),
        FeatureNode(
            node_id="pb_rank",
            label="市净率排名",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.CROSS_SECTION_RANK,
            inputs=("pb",),
        ),
        FeatureNode(
            node_id="value_score",
            label="价值得分",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.NEGATE,
            inputs=("pb_rank",),
        ),
        FeatureNode(
            node_id="momentum",
            label="动量",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="momentum",
        ),
        FeatureNode(
            node_id="volatility",
            label="20 日波动率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="volatility_20d",
        ),
        FeatureNode(
            node_id="low_risk_score",
            label="低风险得分",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.NEGATE,
            inputs=("volatility",),
        ),
        FeatureNode(
            node_id="composite",
            label="复合得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("value_score", "momentum", "low_risk_score"),
            weights=(0.3, 0.4, 0.3),
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="多因子选股模板",
        description="价值、动量和低风险复合得分转为多头目标权重。",
        strategy_kind="multi_factor",
        universe=_universe(
            asset_classes=(AssetClass.EQUITY,),
            selection_limit=20,
            min_amount=0,
            required_data_fields=("price",),
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("composite",)),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="top_score_buy",
                    feature_id="composite",
                    comparator=SignalComparator.RANK_TOP,
                    threshold=0.2,
                    action=SignalAction.BUY,
                    rationale="复合得分前 20% 纳入目标仓位。",
                ),
                SignalRule(
                    rule_id="bottom_score_sell",
                    feature_id="composite",
                    comparator=SignalComparator.RANK_BOTTOM,
                    threshold=0.5,
                    action=SignalAction.SELL,
                    priority=10,
                    rationale="复合得分落入后 50% 时退出已有多头。",
                ),
            )
        ),
        portfolio_policy=_long_only_policy(
            AllocationMethod.EQUAL_WEIGHT,
            max_positions=20,
            max_weight=0.08,
        ),
        risk_exit_policy=_risk_policy(),
        execution_model=ExecutionModel(),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=60,
            legacy_kind="multi_factor",
            legacy_config_version="v1",
        ),
    )


def _etf_rotation_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="close",
            label="收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="sma_200",
            label="200 日均线",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.SIMPLE_MOVING_AVERAGE,
            inputs=("close",),
            window=200,
        ),
        FeatureNode(
            node_id="trend",
            label="绝对趋势",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.SUBTRACT,
            inputs=("close", "sma_200"),
        ),
        FeatureNode(
            node_id="ret_63",
            label="3 月收益",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=63,
        ),
        FeatureNode(
            node_id="ret_126",
            label="6 月收益",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=126,
        ),
        FeatureNode(
            node_id="ret_252",
            label="12 月收益",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=252,
        ),
        FeatureNode(
            node_id="momentum",
            label="多周期相对动量",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("ret_63", "ret_126", "ret_252"),
            weights=(0.333333, 0.333333, 0.333334),
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="多资产 ETF 轮动模板",
        description="通过绝对趋势过滤后选择相对动量较强的多资产 ETF。",
        strategy_kind="etf_rotation",
        universe=_universe(
            asset_classes=(
                AssetClass.EQUITY,
                AssetClass.FIXED_INCOME,
                AssetClass.COMMODITY,
            ),
            selection_limit=5,
            ranking_field="momentum",
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("trend", "momentum")),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="trend_positive",
                    feature_id="trend",
                    comparator=SignalComparator.GREATER_THAN,
                    threshold=0,
                    action=SignalAction.BUY,
                    rationale="价格位于长期均线上方才允许纳入。",
                ),
                SignalRule(
                    rule_id="trend_negative",
                    feature_id="trend",
                    comparator=SignalComparator.LESS_THAN_OR_EQUAL,
                    threshold=0,
                    action=SignalAction.SELL,
                    priority=10,
                    rationale="绝对趋势转负时退出风险资产。",
                ),
            )
        ),
        portfolio_policy=_long_only_policy(
            AllocationMethod.INVERSE_VOLATILITY,
            max_positions=5,
            max_weight=0.3,
            extra_constraints=(
                _constraint(ConstraintName.ASSET_CLASS_CAP, "单资产大类权重上限。", 0.6),
            ),
        ),
        risk_exit_policy=_risk_policy(),
        execution_model=ExecutionModel(),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=61,
            legacy_kind="etf_rotation",
            legacy_config_version=ETF_ROTATION_VERSION,
        ),
    )


def _mean_reversion_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="close",
            label="收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="zscore",
            label="20 日价格 Z 分数",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.ZSCORE,
            inputs=("close",),
            window=20,
        ),
        FeatureNode(
            node_id="regime_sma",
            label="200 日状态均线",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.SIMPLE_MOVING_AVERAGE,
            inputs=("close",),
            window=200,
        ),
        FeatureNode(
            node_id="regime",
            label="趋势状态",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.SUBTRACT,
            inputs=("close", "regime_sma"),
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="ETF 均值回归模板",
        description="仅在允许的趋势状态中买入短期超跌 ETF。以回归信号退出。",
        strategy_kind="mean_reversion",
        universe=_universe(
            asset_classes=(AssetClass.EQUITY,),
            selection_limit=5,
            ranking_field="average_amount",
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("zscore", "regime")),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="oversold_buy",
                    feature_id="zscore",
                    comparator=SignalComparator.LESS_THAN_OR_EQUAL,
                    threshold=-2,
                    action=SignalAction.BUY,
                    rationale="价格低于滚动均值两个标准差时产生超跌买入信号。",
                ),
                SignalRule(
                    rule_id="mean_exit",
                    feature_id="zscore",
                    comparator=SignalComparator.GREATER_THAN_OR_EQUAL,
                    threshold=-0.5,
                    action=SignalAction.SELL,
                    priority=10,
                    rationale="价格回归均值附近时退出。",
                ),
            )
        ),
        portfolio_policy=_long_only_policy(
            AllocationMethod.EQUAL_WEIGHT,
            max_positions=5,
            max_weight=0.2,
        ),
        risk_exit_policy=_risk_policy(max_holding_days=10, cooldown_days=5),
        execution_model=ExecutionModel(),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=62,
            legacy_kind="mean_reversion",
            legacy_config_version=MEAN_REVERSION_VERSION,
        ),
    )


def _convertible_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="price",
            label="转债价格",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="premium",
            label="转股溢价率",
            kind=FeatureKind.FACTOR,
            operator=FeatureOperator.IDENTITY,
            source="premium_rate",
        ),
        FeatureNode(
            node_id="price_rank",
            label="价格排名",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.CROSS_SECTION_RANK,
            inputs=("price",),
        ),
        FeatureNode(
            node_id="premium_rank",
            label="溢价率排名",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.CROSS_SECTION_RANK,
            inputs=("premium",),
        ),
        FeatureNode(
            node_id="double_low",
            label="双低得分",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("price_rank", "premium_rank"),
            weights=(0.5, 0.5),
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="可转债双低模板",
        description="排除强赎等事件风险后选择价格和溢价率综合排名较低的转债。",
        strategy_kind="convertible_double_low",
        universe=_universe(
            asset_classes=(AssetClass.CONVERTIBLE,),
            selection_limit=20,
            ranking_field="double_low",
            ranking_direction=RankingDirection.BOTTOM,
            min_price=100,
            max_price=130,
            events=("forced_redemption", "sell_back", "maturity"),
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("double_low",)),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="double_low_buy",
                    feature_id="double_low",
                    comparator=SignalComparator.RANK_BOTTOM,
                    threshold=0.2,
                    action=SignalAction.BUY,
                    rationale="双低综合排名前 20%(数值最低)纳入。",
                ),
                SignalRule(
                    rule_id="double_low_exit",
                    feature_id="double_low",
                    comparator=SignalComparator.RANK_TOP,
                    threshold=0.5,
                    action=SignalAction.SELL,
                    priority=10,
                    rationale="双低得分恶化至后半区时退出。",
                ),
            )
        ),
        portfolio_policy=_long_only_policy(
            AllocationMethod.EQUAL_WEIGHT,
            max_positions=20,
            max_weight=0.1,
        ),
        risk_exit_policy=_risk_policy(),
        execution_model=ExecutionModel(
            commission_rate=0.0002,
            minimum_commission=0,
            sell_tax_rate=0,
        ),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=63,
            legacy_kind="convertible_double_low",
            legacy_config_version=CONVERTIBLE_DOUBLE_LOW_VERSION,
        ),
    )


def _futures_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="close",
            label="原始合约收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="ret_63",
            label="3 月动量",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=63,
        ),
        FeatureNode(
            node_id="ret_252",
            label="12 月动量",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.RETURN,
            inputs=("close",),
            window=252,
        ),
        FeatureNode(
            node_id="tsmom",
            label="多周期时间序列动量",
            kind=FeatureKind.COMPOSITE,
            operator=FeatureOperator.WEIGHTED_SUM,
            inputs=("ret_63", "ret_252"),
            weights=(0.5, 0.5),
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="期货时间序列动量模板",
        description="使用未拼接原始合约的多周期动量决定多空目标风险敞口。",
        strategy_kind="futures_tsmom",
        universe=_universe(
            asset_classes=(AssetClass.DERIVATIVE,),
            selection_limit=20,
            min_amount=0,
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("tsmom",)),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="positive_tsmom",
                    feature_id="tsmom",
                    comparator=SignalComparator.GREATER_THAN,
                    threshold=0,
                    action=SignalAction.BUY,
                    rationale="时间序列动量为正时建立多头目标。",
                ),
                SignalRule(
                    rule_id="negative_tsmom",
                    feature_id="tsmom",
                    comparator=SignalComparator.LESS_THAN,
                    threshold=0,
                    action=SignalAction.SELL,
                    rationale="时间序列动量为负时建立空头目标。",
                ),
            )
        ),
        portfolio_policy=PortfolioPolicy(
            allocation_method=AllocationMethod.VOLATILITY_SCALED,
            investable_actions=(SignalAction.BUY, SignalAction.SELL),
            max_positions=20,
            max_target_weight=0.3,
            target_gross_exposure=1.5,
            target_net_exposure=0,
            cash_buffer=0.2,
            constraint_refs=(
                _constraint(ConstraintName.MARGIN, "保证金占用不超过权益的 80%。", 0.8),
                _constraint(ConstraintName.LIQUIDITY, "成交量参与率上限。", 0.1),
                _constraint(ConstraintName.ASSET_CLASS_CAP, "单市场名义暴露上限。", 0.4),
            ),
        ),
        risk_exit_policy=_risk_policy(),
        execution_model=ExecutionModel(
            commission_rate=0.000023,
            minimum_commission=0,
            sell_tax_rate=0,
            slippage_bps=2,
        ),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=64,
            legacy_kind="futures_tsmom",
            legacy_config_version=FUTURES_TSMOM_VERSION,
        ),
    )


def _ma_cross_template(
    strategy_id: str,
    release_ids: tuple[str, ...],
) -> ResearchStrategySpec:
    nodes = (
        FeatureNode(
            node_id="close",
            label="收盘价",
            kind=FeatureKind.MARKET_INPUT,
            operator=FeatureOperator.IDENTITY,
            source="close",
        ),
        FeatureNode(
            node_id="ma_short",
            label="5 日均线",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.SIMPLE_MOVING_AVERAGE,
            inputs=("close",),
            window=5,
        ),
        FeatureNode(
            node_id="ma_long",
            label="20 日均线",
            kind=FeatureKind.TRANSFORM,
            operator=FeatureOperator.SIMPLE_MOVING_AVERAGE,
            inputs=("close",),
            window=20,
        ),
    )
    return ResearchStrategySpec(
        strategy_id=strategy_id,
        name="双均线交叉模板",
        description="短均线上穿长均线时纳入。反向交叉时退出。",
        strategy_kind="ma_cross",
        universe=_universe(
            asset_classes=(AssetClass.EQUITY,),
            selection_limit=20,
            ranking_field="average_amount",
        ),
        feature_graph=FeatureGraph(nodes=nodes, outputs=("ma_short", "ma_long")),
        signal_rules=SignalRules(
            rules=(
                SignalRule(
                    rule_id="golden_cross",
                    feature_id="ma_short",
                    reference_feature_id="ma_long",
                    comparator=SignalComparator.CROSS_ABOVE,
                    action=SignalAction.BUY,
                    rationale="短均线上穿长均线。",
                ),
                SignalRule(
                    rule_id="death_cross",
                    feature_id="ma_short",
                    reference_feature_id="ma_long",
                    comparator=SignalComparator.CROSS_BELOW,
                    action=SignalAction.SELL,
                    priority=10,
                    rationale="短均线下穿长均线。",
                ),
            )
        ),
        portfolio_policy=_long_only_policy(
            AllocationMethod.EQUAL_WEIGHT,
            max_positions=20,
            max_weight=0.1,
        ),
        risk_exit_policy=_risk_policy(),
        execution_model=ExecutionModel(),
        validation_plan=_validation(release_ids),
        compatibility=LegacyCompatibility(
            issue=29,
            legacy_kind="ma_cross",
            legacy_config_version="v1",
        ),
    )


__all__ = [
    "StrategyCapability",
    "build_strategy_template",
    "compile_registered_strategy_spec",
    "get_strategy_capability",
    "list_strategy_capabilities",
]
