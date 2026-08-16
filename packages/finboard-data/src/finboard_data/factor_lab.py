"""因子实验室的版本化 Feature → Factor → Signal 领域契约(issue #78)。

本模块只描述离线研究产物;不依赖数据库、券商、账户、订单或持仓。核心边界:

* ``FeatureSnapshot`` 冻结指定研究数据发布在决策时点可见的特征;
* ``FactorDefinition`` 区分 alpha、风险暴露和跨市场输入;
* ``FactorSignal`` 是供策略/组合层消费的标准化研究信号;不是订单;
* 只有绑定机器验证且状态为 ``validated_oos`` 的信号可进入后续策略研究;
* ``FactorExperiment`` 冻结 IS/OOS、试验预算并保留失败/中断结论。
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import cast
from uuid import uuid4

FACTOR_LAB_SCHEMA_VERSION = "v2"


class FactorRole(StrEnum):
    """因子在研究中的职责;风险暴露不得混入 alpha 分数。"""

    ALPHA = "alpha"
    RISK = "risk"
    MARKET_INPUT = "market_input"


class FactorPreference(StrEnum):
    """原始特征值与预期收益/风险的方向关系。"""

    HIGHER = "higher"
    LOWER = "lower"
    EXPOSURE_ONLY = "exposure_only"


class FeatureFrequency(StrEnum):
    DAILY = "daily"
    REPORT = "report"
    EVENT = "event"


class FeatureMissingPolicy(StrEnum):
    FAIL_CLOSED = "fail_closed"
    EXCLUDE = "exclude"
    FORWARD_FILL = "forward_fill"
    CROSS_SECTION_MEDIAN = "cross_section_median"


class SignalDirection(StrEnum):
    LONG = "long"
    SHORT = "short"
    NEUTRAL = "neutral"


class ResearchArtifactStatus(StrEnum):
    HYPOTHESIS = "hypothesis"
    VALIDATED_OOS = "validated_oos"
    REJECTED = "rejected"


class FactorExperimentStatus(StrEnum):
    HYPOTHESIS = "hypothesis"
    RUNNING = "running"
    VALIDATED_OOS = "validated_oos"
    REJECTED = "rejected"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class FactorDefinition:
    """代码实现与研究语义一一对应的目录项。"""

    name: str
    version: str
    role: FactorRole
    preference: FactorPreference
    frequency: FeatureFrequency
    unit: str
    source_fields: tuple[str, ...]
    calculation_window: int | None
    default_transform: str
    default_neutralization: tuple[str, ...]
    available_at_rule: str
    missing_policy: FeatureMissingPolicy
    economic_hypothesis: str
    expected_failure: str
    implementation: str
    signal_eligible: bool

    def __post_init__(self) -> None:
        if not self.name or not self.version or not self.unit:
            raise ValueError("factor name/version/unit 不能为空")
        if not self.source_fields:
            raise ValueError(f"{self.name} 必须声明 source_fields")
        if self.calculation_window is not None and self.calculation_window <= 0:
            raise ValueError("calculation_window 必须大于 0")
        if not self.implementation:
            raise ValueError(f"{self.name} 未声明实现;不能进入因子目录")
        if self.signal_eligible and self.role is not FactorRole.ALPHA:
            raise ValueError("只有 alpha 因子可以直接转换为 FactorSignal")

    def _contract_payload(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "role": self.role.value,
            "preference": self.preference.value,
            "frequency": self.frequency.value,
            "unit": self.unit,
            "source_fields": list(self.source_fields),
            "calculation_window": self.calculation_window,
            "default_transform": self.default_transform,
            "default_neutralization": list(self.default_neutralization),
            "available_at_rule": self.available_at_rule,
            "missing_policy": self.missing_policy.value,
            "economic_hypothesis": self.economic_hypothesis,
            "expected_failure": self.expected_failure,
            "implementation": self.implementation,
            "signal_eligible": self.signal_eligible,
        }

    @property
    def checksum(self) -> str:
        """目录定义的稳定 checksum;任何研究语义变化都会改变。"""

        return _checksum(self._contract_payload())

    def as_dict(self) -> dict[str, object]:
        payload = self._contract_payload()
        payload["checksum"] = self.checksum
        return payload


def _definition(
    name: str,
    role: FactorRole,
    preference: FactorPreference,
    source_fields: tuple[str, ...],
    hypothesis: str,
    failure: str,
    *,
    window: int | None = None,
    frequency: FeatureFrequency = FeatureFrequency.DAILY,
    unit: str = "z_score",
    transform: str = "winsorize_zscore",
    neutralization: tuple[str, ...] = (),
    missing: FeatureMissingPolicy = FeatureMissingPolicy.EXCLUDE,
    implementation: str,
    signal_eligible: bool = True,
) -> FactorDefinition:
    return FactorDefinition(
        name=name,
        version=FACTOR_LAB_SCHEMA_VERSION,
        role=role,
        preference=preference,
        frequency=frequency,
        unit=unit,
        source_fields=source_fields,
        calculation_window=window,
        default_transform=transform,
        default_neutralization=neutralization,
        available_at_rule=(
            "逐条数据 available_at <= decision_at;日线仅在对应市场收盘后可用"
        ),
        missing_policy=missing,
        economic_hypothesis=hypothesis,
        expected_failure=failure,
        implementation=implementation,
        signal_eligible=signal_eligible,
    )


# 目录只登记当前代码中可执行的因子。residual_momentum 在模型实现完成前不得暴露。
FACTOR_LAB_CATALOG: dict[str, FactorDefinition] = {
    item.name: item
    for item in (
        _definition(
            "pb",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("daily_metrics.pb",),
            "较低估值可能提供长期价值溢价。",
            "周期顶部和价值陷阱会使低 PB 失效。",
            neutralization=("industry", "log_market_cap"),
            implementation="finboard_backtest.factors.extract:_add_daily_factors",
        ),
        _definition(
            "earnings_yield",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("daily_metrics.pe_ttm",),
            "较高盈利收益率可能提供价值溢价。",
            "周期盈利不可持续或亏损会使指标失真。",
            neutralization=("industry", "log_market_cap"),
            implementation="finboard_backtest.factors.extract:_add_daily_factors",
        ),
        _definition(
            "dividend_yield",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("daily_metrics.dividend_yield_ttm",),
            "稳定股息可能提供现金流与估值安全边际。",
            "一次性分红和盈利恶化会造成虚高。",
            neutralization=("industry",),
            implementation="finboard_backtest.factors.extract:_add_daily_factors",
        ),
        _definition(
            "roe",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("financial_indicators.return_on_equity",),
            "高资本回报可能反映持续竞争优势。",
            "高杠杆和行业结构差异会污染 ROE。",
            frequency=FeatureFrequency.REPORT,
            neutralization=("industry", "log_market_cap"),
            implementation="finboard_backtest.factors.extract:_add_financial_factors",
        ),
        _definition(
            "gross_profit_margin",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("financial_indicators.gross_profit_margin",),
            "高毛利可能反映定价权和成本优势。",
            "行业差异和收入收缩会造成误判。",
            frequency=FeatureFrequency.REPORT,
            neutralization=("industry",),
            implementation="finboard_backtest.factors.extract:_add_financial_factors",
        ),
        _definition(
            "debt_to_assets",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("financial_indicators.debt_to_assets",),
            "较低杠杆可能降低尾部财务风险。",
            "金融、地产等行业不可直接横向比较。",
            frequency=FeatureFrequency.REPORT,
            neutralization=("industry",),
            implementation="finboard_backtest.factors.extract:_add_financial_factors",
        ),
        _definition(
            "revenue_yoy",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("financial_indicators.revenue_yoy",),
            "收入增长可能反映企业扩张能力。",
            "并购、低基数和周期见顶会造成一次性增长。",
            frequency=FeatureFrequency.REPORT,
            neutralization=("industry", "log_market_cap"),
            implementation="finboard_backtest.factors.extract:_add_financial_factors",
        ),
        _definition(
            "momentum",
            FactorRole.ALPHA,
            FactorPreference.HIGHER,
            ("bars.close",),
            "中期价格趋势可能延续。",
            "市场急转和拥挤交易会导致动量崩溃。",
            window=20,
            implementation="finboard_backtest.factors.extract:_add_price_factors",
        ),
        _definition(
            "volatility_20d",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("bars.close",),
            "低波动异象可能提供更好的风险调整后收益。",
            "波动状态快速切换时历史估计会滞后。",
            window=20,
            implementation="finboard_backtest.factors.extract:_add_price_factors",
        ),
        _definition(
            "volatility_60d",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("bars.close",),
            "中期低波动暴露可能降低组合尾部风险。",
            "历史窗口会掩盖近期风险跃迁。",
            window=60,
            implementation="finboard_backtest.factors.extract:_add_price_factors",
        ),
        _definition(
            "volatility_120d",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("bars.close",),
            "长期低波动暴露可能改善回撤。",
            "长窗口会包含已经消退的风险事件。",
            window=120,
            implementation="finboard_backtest.factors.extract:_add_price_factors",
        ),
        _definition(
            "downside_volatility",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("bars.close",),
            "下行波动比全样本波动更贴近损失风险。",
            "负收益观测过少时估计不稳定。",
            window=60,
            implementation="finboard_backtest.factors.extract:_add_price_factors",
        ),
        _definition(
            "turnover_rate",
            FactorRole.ALPHA,
            FactorPreference.LOWER,
            ("daily_metrics.turnover_rate",),
            "较低换手可能对应持有者稳定性和流动性溢价。",
            "极低换手也可能表示无法成交。",
            neutralization=("industry", "log_market_cap"),
            missing=FeatureMissingPolicy.CROSS_SECTION_MEDIAN,
            implementation="finboard_backtest.factors.extract:_add_daily_factors",
        ),
        _definition(
            "market_beta",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("bars.close", "benchmark.close"),
            "市场 beta 描述系统性市场风险暴露。",
            "短窗口和结构突变会使 beta 不稳定。",
            window=60,
            transform="ols_beta",
            unit="beta",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "industry_exposure",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("industry_memberships.level1_code",),
            "行业暴露解释共同收益和集中风险。",
            "缺失或回填错误的历史行业会造成未来数据泄漏。",
            transform="one_hot",
            unit="binary",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "asset_class_exposure",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("release.instruments.asset_class",),
            "资产类别暴露解释跨资产共同风险。",
            "类别元数据错误会扭曲 sleeve 风险。",
            transform="one_hot",
            unit="binary",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "size_exposure",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("daily_metrics.total_market_cap",),
            "对数市值刻画规模风险暴露。",
            "ETF/期货与股票市值口径不可混用。",
            transform="log_zscore",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "volatility_exposure",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("bars.close",),
            "历史波动率刻画资产自身风险水平。",
            "跳跃和波动聚集使估计具有滞后性。",
            window=60,
            transform="annualized_volatility",
            unit="annualized_decimal",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "liquidity_exposure",
            FactorRole.RISK,
            FactorPreference.EXPOSURE_ONLY,
            ("daily_metrics.turnover_rate",),
            "流动性暴露刻画成交能力和冲击成本风险。",
            "换手率不能完全替代盘口深度和成交额。",
            transform="log_zscore",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:estimate_basic_risk_model",
            signal_eligible=False,
        ),
        _definition(
            "risk_free_rate",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("macro.risk_free_rate",),
            "利率影响现金收益、贴现率和资产估值。",
            "发布延迟和期限选择不一致会产生口径偏差。",
            transform="level",
            unit="decimal_rate",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
        _definition(
            "government_bond_return",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("bars.bond_total_return",),
            "国债收益反映利率和避险状态。",
            "久期和指数口径不同会影响可比性。",
            transform="return",
            unit="decimal_return",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
        _definition(
            "fx_usdcny_return",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("macro.usdcny",),
            "汇率变化影响跨境资产的人民币收益。",
            "境内外汇率和发布时间差异会造成偏差。",
            transform="return",
            unit="decimal_return",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
        _definition(
            "gold_return",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("bars.gold_total_return",),
            "黄金可提供通胀、美元和避险状态信息。",
            "人民币汇率与跟踪误差会改变本地 ETF 收益。",
            transform="return",
            unit="decimal_return",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
        _definition(
            "market_breadth",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("market.advancers", "market.decliners"),
            "上涨家数占比刻画趋势参与广度。",
            "停牌、新股和股票池变化会改变分母。",
            transform="advancers_ratio",
            unit="decimal_ratio",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
        _definition(
            "volatility_regime",
            FactorRole.MARKET_INPUT,
            FactorPreference.EXPOSURE_ONLY,
            ("benchmark.close",),
            "市场波动状态可用于检验因子失效区间。",
            "短期冲击与窗口选择会导致状态跳变。",
            window=20,
            transform="realized_volatility_regime",
            unit="annualized_decimal",
            missing=FeatureMissingPolicy.FAIL_CLOSED,
            implementation="finboard_backtest.factor_lab:build_cross_market_snapshot",
            signal_eligible=False,
        ),
    )
}


def factor_lab_catalog(
    role: FactorRole | None = None,
) -> tuple[FactorDefinition, ...]:
    """返回稳定排序且全部有真实实现的目录。"""

    return tuple(
        definition
        for name, definition in sorted(FACTOR_LAB_CATALOG.items())
        if role is None or definition.role is role
    )


def get_factor_definition(name: str) -> FactorDefinition:
    try:
        return FACTOR_LAB_CATALOG[name]
    except KeyError as exc:
        raise KeyError(
            f"未知或未实现因子: {name}; 可用: {sorted(FACTOR_LAB_CATALOG)}"
        ) from exc


@dataclass(frozen=True, slots=True)
class FeatureObservation:
    """决策时点可见的一项特征值。"""

    symbol: str
    feature_name: str
    value: float
    observed_at: datetime
    available_at: datetime
    source: str
    source_version: str
    market: str | None = None
    asset_class: str | None = None
    industry: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol or not self.source or not self.source_version:
            raise ValueError("FeatureObservation 标的、来源和版本不能为空")
        get_factor_definition(self.feature_name)
        if not math.isfinite(self.value):
            raise ValueError("FeatureObservation.value 必须为有限数")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.available_at, "available_at")

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "feature_name": self.feature_name,
            "value": self.value,
            "observed_at": self.observed_at.isoformat(),
            "available_at": self.available_at.isoformat(),
            "source": self.source,
            "source_version": self.source_version,
            "market": self.market,
            "asset_class": self.asset_class,
            "industry": self.industry,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> FeatureObservation:
        return cls(
            symbol=str(raw["symbol"]),
            feature_name=str(raw["feature_name"]),
            value=float(str(raw["value"])),
            observed_at=datetime.fromisoformat(str(raw["observed_at"])),
            available_at=datetime.fromisoformat(str(raw["available_at"])),
            source=str(raw["source"]),
            source_version=str(raw["source_version"]),
            market=_optional_text(raw.get("market")),
            asset_class=_optional_text(raw.get("asset_class")),
            industry=_optional_text(raw.get("industry")),
        )


@dataclass(frozen=True, slots=True)
class FeatureSnapshot:
    """一个数据发布在单一决策时点的不可变特征快照。"""

    snapshot_id: str
    dataset_release_id: str
    dataset_release_checksum: str
    decision_at: datetime
    published_at: datetime
    framework_version: str
    calculation_windows: dict[str, int]
    transformations: dict[str, str]
    neutralization: dict[str, tuple[str, ...]]
    code_version: str
    observations: tuple[FeatureObservation, ...]
    checksum: str
    issues: tuple[str, ...] = ()
    _payload_cache: dict[str, object] | None = field(
        default=None,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not self.snapshot_id or not self.dataset_release_id:
            raise ValueError("snapshot_id/dataset_release_id 不能为空")
        if not self.dataset_release_checksum or not self.code_version:
            raise ValueError("数据发布 checksum 和 code_version 必填")
        _require_aware(self.decision_at, "decision_at")
        _require_aware(self.published_at, "published_at")
        keys: set[tuple[str, str]] = set()
        for observation in self.observations:
            key = (observation.symbol, observation.feature_name)
            if key in keys:
                raise ValueError(f"特征快照存在重复 observation: {key}")
            keys.add(key)
            if observation.available_at > self.decision_at:
                raise PointInTimeViolationError(
                    f"{observation.symbol}/{observation.feature_name} available_at="
                    f"{observation.available_at.isoformat()} 晚于 decision_at"
                )
        if not self.observations:
            raise ValueError("FeatureSnapshot 不能为空")
        for name, window in self.calculation_windows.items():
            get_factor_definition(name)
            if window <= 0:
                raise ValueError("calculation window 必须大于 0")

    def as_dict(self) -> dict[str, object]:
        cached = self._payload_cache
        if cached is not None:
            return cached
        payload: dict[str, object] = {
            "snapshot_id": self.snapshot_id,
            "dataset_release_id": self.dataset_release_id,
            "dataset_release_checksum": self.dataset_release_checksum,
            "decision_at": self.decision_at.isoformat(),
            "published_at": self.published_at.isoformat(),
            "framework_version": self.framework_version,
            "calculation_windows": dict(sorted(self.calculation_windows.items())),
            "transformations": dict(sorted(self.transformations.items())),
            "neutralization": {
                name: list(values)
                for name, values in sorted(self.neutralization.items())
            },
            "code_version": self.code_version,
            "observations": [item.as_dict() for item in self.observations],
            "checksum": self.checksum,
            "issues": list(self.issues),
        }
        object.__setattr__(self, "_payload_cache", payload)
        return payload

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, object],
        *,
        verify_checksum: bool = True,
    ) -> FeatureSnapshot:
        neutralization_raw = cast(
            dict[str, list[object]], raw.get("neutralization", {})
        )
        snapshot = cls(
            snapshot_id=str(raw["snapshot_id"]),
            dataset_release_id=str(raw["dataset_release_id"]),
            dataset_release_checksum=str(raw["dataset_release_checksum"]),
            decision_at=datetime.fromisoformat(str(raw["decision_at"])),
            published_at=datetime.fromisoformat(str(raw["published_at"])),
            framework_version=str(raw["framework_version"]),
            calculation_windows={
                str(name): int(str(value))
                for name, value in cast(
                    dict[str, object], raw.get("calculation_windows", {})
                ).items()
            },
            transformations={
                str(name): str(value)
                for name, value in cast(
                    dict[str, object], raw.get("transformations", {})
                ).items()
            },
            neutralization={
                str(name): tuple(str(value) for value in values)
                for name, values in neutralization_raw.items()
            },
            code_version=str(raw["code_version"]),
            observations=tuple(
                FeatureObservation.from_dict(item)
                for item in cast(
                    list[dict[str, object]], raw.get("observations", [])
                )
            ),
            checksum=str(raw["checksum"]),
            issues=tuple(
                str(item) for item in cast(list[object], raw.get("issues", []))
            ),
        )
        if verify_checksum and _feature_snapshot_checksum(snapshot) != snapshot.checksum:
            raise ArtifactIntegrityError("FeatureSnapshot checksum 不一致")
        return snapshot


class FactorLabError(RuntimeError):
    """因子实验室基础错误。"""


class PointInTimeViolationError(FactorLabError):
    """输入包含决策时点之后才可见的数据。"""


class ArtifactIntegrityError(FactorLabError):
    """不可变研究产物内容漂移。"""


class SignalNotValidatedError(FactorLabError):
    """信号未通过冻结 OOS 验证;禁止进入策略层。"""


def build_feature_snapshot(
    *,
    dataset_release_id: str,
    dataset_release_checksum: str,
    decision_at: datetime,
    code_version: str,
    observations: Iterable[FeatureObservation],
    calculation_windows: dict[str, int] | None = None,
    transformations: dict[str, str] | None = None,
    neutralization: dict[str, tuple[str, ...]] | None = None,
    issues: tuple[str, ...] = (),
    published_at: datetime | None = None,
) -> FeatureSnapshot:
    """构建确定性快照并机器执行 PIT 门。"""

    _require_aware(decision_at, "decision_at")
    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (item.feature_name, item.symbol),
        )
    )
    provisional = FeatureSnapshot(
        snapshot_id="pending",
        dataset_release_id=dataset_release_id,
        dataset_release_checksum=dataset_release_checksum,
        decision_at=decision_at,
        published_at=published_at or datetime.now(UTC),
        framework_version=FACTOR_LAB_SCHEMA_VERSION,
        calculation_windows=calculation_windows or {},
        transformations=transformations or {},
        neutralization=neutralization or {},
        code_version=code_version,
        observations=ordered,
        checksum="pending",
        issues=issues,
    )
    checksum = _feature_snapshot_checksum(provisional)
    result = replace(
        provisional,
        snapshot_id=f"feature-{checksum[:16]}",
        checksum=checksum,
    )
    # 快照通常会在发布和 API 序列化阶段重复调用 as_dict;缓存这份不可变
    # 研究 payload,避免 5k 标的再次在 API 事件循环中构造/序列化大列表。
    result.as_dict()
    return result


@dataclass(frozen=True, slots=True)
class FactorSignalItem:
    """单标的标准化信号;方向只是研究偏好而不是买卖订单。"""

    symbol: str
    direction: SignalDirection
    score: float
    confidence: float
    valid_from: datetime
    valid_until: datetime
    reason: str

    def __post_init__(self) -> None:
        if not self.symbol or not self.reason:
            raise ValueError("signal symbol/reason 不能为空")
        if not math.isfinite(self.score):
            raise ValueError("signal score 必须为有限数")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("signal confidence 必须在 [0,1]")
        _require_aware(self.valid_from, "valid_from")
        _require_aware(self.valid_until, "valid_until")
        if self.valid_until <= self.valid_from:
            raise ValueError("valid_until 必须晚于 valid_from")

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "direction": self.direction.value,
            "score": self.score,
            "confidence": self.confidence,
            "valid_from": self.valid_from.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> FactorSignalItem:
        return cls(
            symbol=str(raw["symbol"]),
            direction=SignalDirection(str(raw["direction"])),
            score=float(str(raw["score"])),
            confidence=float(str(raw["confidence"])),
            valid_from=datetime.fromisoformat(str(raw["valid_from"])),
            valid_until=datetime.fromisoformat(str(raw["valid_until"])),
            reason=str(raw["reason"]),
        )


@dataclass(frozen=True, slots=True)
class FactorSignal:
    """版本化因子信号集合。"""

    signal_id: str
    factor_name: str
    factor_version: str
    feature_snapshot_id: str
    feature_snapshot_checksum: str
    candidate_universe_version: str
    research_status: ResearchArtifactStatus
    validation_experiment_id: str | None
    created_at: datetime
    items: tuple[FactorSignalItem, ...]
    checksum: str

    def __post_init__(self) -> None:
        definition = get_factor_definition(self.factor_name)
        if not definition.signal_eligible:
            raise ValueError(f"{self.factor_name} 不是 alpha 信号因子")
        if (
            not self.signal_id
            or not self.feature_snapshot_id
            or not self.feature_snapshot_checksum
            or not self.candidate_universe_version
            or not self.checksum
        ):
            raise ValueError("信号 ID、快照 lineage、候选池版本和 checksum 必填")
        if self.factor_version != definition.version:
            raise ValueError("factor_version 与目录不一致")
        if not self.items:
            raise ValueError("FactorSignal.items 不能为空")
        if len({item.symbol for item in self.items}) != len(self.items):
            raise ValueError("FactorSignal 存在重复标的")
        _require_aware(self.created_at, "created_at")
        if (
            self.research_status is ResearchArtifactStatus.VALIDATED_OOS
            and not self.validation_experiment_id
        ):
            raise ValueError("validated_oos 信号必须绑定机器验证 experiment")

    def require_strategy_usable(self) -> None:
        """策略层消费前的 fail-closed 门。"""

        if self.research_status is not ResearchArtifactStatus.VALIDATED_OOS:
            raise SignalNotValidatedError(
                f"signal={self.signal_id} status={self.research_status.value}; "
                "只有 validated_oos 可进入策略研究"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "signal_id": self.signal_id,
            "factor_name": self.factor_name,
            "factor_version": self.factor_version,
            "feature_snapshot_id": self.feature_snapshot_id,
            "feature_snapshot_checksum": self.feature_snapshot_checksum,
            "candidate_universe_version": self.candidate_universe_version,
            "research_status": self.research_status.value,
            "validation_experiment_id": self.validation_experiment_id,
            "created_at": self.created_at.isoformat(),
            "items": [item.as_dict() for item in self.items],
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(
        cls,
        raw: dict[str, object],
        *,
        verify_checksum: bool = True,
    ) -> FactorSignal:
        signal = cls(
            signal_id=str(raw["signal_id"]),
            factor_name=str(raw["factor_name"]),
            factor_version=str(raw["factor_version"]),
            feature_snapshot_id=str(raw["feature_snapshot_id"]),
            feature_snapshot_checksum=str(raw["feature_snapshot_checksum"]),
            candidate_universe_version=str(raw["candidate_universe_version"]),
            research_status=ResearchArtifactStatus(str(raw["research_status"])),
            validation_experiment_id=_optional_text(
                raw.get("validation_experiment_id")
            ),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            items=tuple(
                FactorSignalItem.from_dict(item)
                for item in cast(list[dict[str, object]], raw.get("items", []))
            ),
            checksum=str(raw["checksum"]),
        )
        if verify_checksum and _factor_signal_checksum(signal) != signal.checksum:
            raise ArtifactIntegrityError("FactorSignal checksum 不一致")
        return signal


def build_factor_signal(
    *,
    feature_snapshot: FeatureSnapshot,
    factor_name: str,
    normalized_scores: dict[str, float],
    candidate_universe_version: str,
    research_status: ResearchArtifactStatus = ResearchArtifactStatus.HYPOTHESIS,
    validation_experiment_id: str | None = None,
    confidences: dict[str, float] | None = None,
    valid_for: timedelta = timedelta(days=1),
    neutral_band: float = 1e-12,
    created_at: datetime | None = None,
) -> FactorSignal:
    """把已方向统一的标准化 alpha 分数转换为显式信号。"""

    definition = get_factor_definition(factor_name)
    if not definition.signal_eligible:
        raise ValueError(f"{factor_name} 不能转换为信号")
    if valid_for <= timedelta(0):
        raise ValueError("valid_for 必须为正")
    if not math.isfinite(neutral_band) or neutral_band < 0:
        raise ValueError("neutral_band 必须为非负有限数")
    available_symbols = {
        item.symbol
        for item in feature_snapshot.observations
        if item.feature_name == factor_name
    }
    unknown = set(normalized_scores) - available_symbols
    if unknown:
        raise ValueError(f"信号包含快照外标的: {sorted(unknown)}")
    if not normalized_scores:
        raise ValueError("normalized_scores 不能为空")
    now = created_at or datetime.now(UTC)
    _require_aware(now, "created_at")
    items: list[FactorSignalItem] = []
    for symbol, score in sorted(normalized_scores.items()):
        if not math.isfinite(score):
            raise ValueError(f"{symbol} score 不是有限数")
        if score > neutral_band:
            direction = SignalDirection.LONG
        elif score < -neutral_band:
            direction = SignalDirection.SHORT
        else:
            direction = SignalDirection.NEUTRAL
        confidence = (
            confidences[symbol]
            if confidences is not None and symbol in confidences
            else min(1.0, abs(score) / 3.0)
        )
        items.append(
            FactorSignalItem(
                symbol=symbol,
                direction=direction,
                score=score,
                confidence=confidence,
                valid_from=feature_snapshot.decision_at,
                valid_until=feature_snapshot.decision_at + valid_for,
                reason=(
                    f"{factor_name}@{definition.version} 标准化得分={score:.6f}; "
                    "方向仅供策略/组合决策;不是订单"
                ),
            )
        )
    provisional = FactorSignal(
        signal_id="pending",
        factor_name=factor_name,
        factor_version=definition.version,
        feature_snapshot_id=feature_snapshot.snapshot_id,
        feature_snapshot_checksum=feature_snapshot.checksum,
        candidate_universe_version=candidate_universe_version,
        research_status=research_status,
        validation_experiment_id=validation_experiment_id,
        created_at=now,
        items=tuple(items),
        checksum="pending",
    )
    checksum = _factor_signal_checksum(provisional)
    return replace(
        provisional,
        signal_id=f"signal-{checksum[:16]}",
        checksum=checksum,
    )


@dataclass(frozen=True, slots=True)
class FactorExperimentPlan:
    """因子实验前冻结的 IS/OOS 切分和试验预算。"""

    in_sample_start: date
    in_sample_end: date
    oos_start: date
    oos_end: date
    trial_budget: int
    benchmark_symbol: str
    transaction_cost_bps: float
    quantiles: int = 5

    def __post_init__(self) -> None:
        if self.in_sample_start >= self.in_sample_end:
            raise ValueError("IS start 必须早于 end")
        if self.oos_start < self.in_sample_end:
            raise ValueError("OOS 不能与 IS 重叠")
        if self.oos_start >= self.oos_end:
            raise ValueError("OOS start 必须早于 end")
        if self.trial_budget <= 0:
            raise ValueError("trial_budget 必须为正")
        if not self.benchmark_symbol:
            raise ValueError("benchmark_symbol 必填")
        if self.transaction_cost_bps < 0:
            raise ValueError("transaction_cost_bps 不能为负")
        if self.quantiles < 2:
            raise ValueError("quantiles 至少为 2")

    def as_dict(self) -> dict[str, object]:
        return {
            "in_sample_start": self.in_sample_start.isoformat(),
            "in_sample_end": self.in_sample_end.isoformat(),
            "oos_start": self.oos_start.isoformat(),
            "oos_end": self.oos_end.isoformat(),
            "trial_budget": self.trial_budget,
            "benchmark_symbol": self.benchmark_symbol,
            "transaction_cost_bps": self.transaction_cost_bps,
            "quantiles": self.quantiles,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> FactorExperimentPlan:
        return cls(
            in_sample_start=date.fromisoformat(str(raw["in_sample_start"])),
            in_sample_end=date.fromisoformat(str(raw["in_sample_end"])),
            oos_start=date.fromisoformat(str(raw["oos_start"])),
            oos_end=date.fromisoformat(str(raw["oos_end"])),
            trial_budget=int(str(raw["trial_budget"])),
            benchmark_symbol=str(raw["benchmark_symbol"]),
            transaction_cost_bps=float(str(raw["transaction_cost_bps"])),
            quantiles=int(str(raw.get("quantiles", 5))),
        )


@dataclass(frozen=True, slots=True)
class FactorExperiment:
    """因子实验审计根;失败、中断、拒绝都必须持久化。"""

    experiment_id: str
    hypothesis: str
    factor_names: tuple[str, ...]
    dataset_release_id: str
    dataset_release_checksum: str
    feature_snapshot_id: str
    plan: FactorExperimentPlan
    comparison_group: str
    status: FactorExperimentStatus
    validation_experiment_id: str | None
    result: dict[str, object] | None
    failure_reason: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not self.experiment_id or not self.hypothesis.strip():
            raise ValueError("experiment_id/hypothesis 不能为空")
        if not self.factor_names:
            raise ValueError("factor_names 不能为空")
        for name in self.factor_names:
            definition = get_factor_definition(name)
            if definition.role is not FactorRole.ALPHA:
                raise ValueError(f"{name} 不是 alpha 因子")
        if len(set(self.factor_names)) != len(self.factor_names):
            raise ValueError("factor_names 不能重复")
        if not self.dataset_release_id or not self.dataset_release_checksum:
            raise ValueError("实验必须绑定冻结数据发布")
        if not self.feature_snapshot_id:
            raise ValueError("实验必须绑定 FeatureSnapshot")
        if not self.comparison_group:
            raise ValueError("comparison_group 不能为空")
        _require_aware(self.created_at, "created_at")
        _require_aware(self.updated_at, "updated_at")
        if (
            self.status is FactorExperimentStatus.VALIDATED_OOS
            and (not self.validation_experiment_id or self.result is None)
        ):
            raise ValueError("validated_oos 必须绑定验证 run 和机器结果")
        if self.status in (
            FactorExperimentStatus.REJECTED,
            FactorExperimentStatus.FAILED,
            FactorExperimentStatus.INTERRUPTED,
        ) and not self.failure_reason:
            raise ValueError(f"{self.status.value} 必须记录原因")

    def as_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis": self.hypothesis,
            "factor_names": list(self.factor_names),
            "dataset_release_id": self.dataset_release_id,
            "dataset_release_checksum": self.dataset_release_checksum,
            "feature_snapshot_id": self.feature_snapshot_id,
            "plan": self.plan.as_dict(),
            "comparison_group": self.comparison_group,
            "status": self.status.value,
            "validation_experiment_id": self.validation_experiment_id,
            "result": self.result,
            "failure_reason": self.failure_reason,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> FactorExperiment:
        result = raw.get("result")
        return cls(
            experiment_id=str(raw["experiment_id"]),
            hypothesis=str(raw["hypothesis"]),
            factor_names=tuple(
                str(item)
                for item in cast(list[object], raw.get("factor_names", []))
            ),
            dataset_release_id=str(raw["dataset_release_id"]),
            dataset_release_checksum=str(raw["dataset_release_checksum"]),
            feature_snapshot_id=str(raw["feature_snapshot_id"]),
            plan=FactorExperimentPlan.from_dict(
                cast(dict[str, object], raw["plan"])
            ),
            comparison_group=str(raw["comparison_group"]),
            status=FactorExperimentStatus(str(raw["status"])),
            validation_experiment_id=_optional_text(
                raw.get("validation_experiment_id")
            ),
            result=cast(dict[str, object] | None, result),
            failure_reason=_optional_text(raw.get("failure_reason")),
            created_at=datetime.fromisoformat(str(raw["created_at"])),
            updated_at=datetime.fromisoformat(str(raw["updated_at"])),
        )


def new_factor_experiment(
    *,
    hypothesis: str,
    factor_names: tuple[str, ...],
    dataset_release_id: str,
    dataset_release_checksum: str,
    feature_snapshot_id: str,
    plan: FactorExperimentPlan,
    comparison_group: str,
    validation_experiment_id: str | None = None,
    now: datetime | None = None,
) -> FactorExperiment:
    created = now or datetime.now(UTC)
    return FactorExperiment(
        experiment_id=uuid4().hex[:16],
        hypothesis=hypothesis,
        factor_names=tuple(sorted(factor_names)),
        dataset_release_id=dataset_release_id,
        dataset_release_checksum=dataset_release_checksum,
        feature_snapshot_id=feature_snapshot_id,
        plan=plan,
        comparison_group=comparison_group,
        status=FactorExperimentStatus.HYPOTHESIS,
        validation_experiment_id=validation_experiment_id,
        result=None,
        failure_reason=None,
        created_at=created,
        updated_at=created,
    )


def update_factor_experiment(
    experiment: FactorExperiment,
    *,
    status: FactorExperimentStatus,
    result: dict[str, object] | None = None,
    failure_reason: str | None = None,
    validation_experiment_id: str | None = None,
    now: datetime | None = None,
) -> FactorExperiment:
    """单向更新实验状态;禁止把终态重新包装为赢家。"""

    terminal = {
        FactorExperimentStatus.VALIDATED_OOS,
        FactorExperimentStatus.REJECTED,
        FactorExperimentStatus.FAILED,
    }
    if experiment.status in terminal and status is not experiment.status:
        raise ValueError(f"终态 {experiment.status.value} 不可迁移")
    allowed = {
        FactorExperimentStatus.HYPOTHESIS: {
            FactorExperimentStatus.RUNNING,
            FactorExperimentStatus.REJECTED,
            FactorExperimentStatus.FAILED,
            FactorExperimentStatus.INTERRUPTED,
        },
        FactorExperimentStatus.RUNNING: {
            FactorExperimentStatus.VALIDATED_OOS,
            FactorExperimentStatus.REJECTED,
            FactorExperimentStatus.FAILED,
            FactorExperimentStatus.INTERRUPTED,
        },
        FactorExperimentStatus.INTERRUPTED: {
            FactorExperimentStatus.RUNNING,
            FactorExperimentStatus.REJECTED,
            FactorExperimentStatus.FAILED,
        },
    }
    if status is not experiment.status and status not in allowed.get(
        experiment.status, set()
    ):
        raise ValueError(
            f"非法因子实验状态迁移: {experiment.status.value}->{status.value}"
        )
    return replace(
        experiment,
        status=status,
        result=result,
        failure_reason=failure_reason,
        validation_experiment_id=(
            validation_experiment_id or experiment.validation_experiment_id
        ),
        updated_at=now or datetime.now(UTC),
    )


def _feature_snapshot_checksum(snapshot: FeatureSnapshot) -> str:
    payload = dict(snapshot.as_dict())
    payload["snapshot_id"] = ""
    payload["checksum"] = ""
    return _checksum(payload)


def _factor_signal_checksum(signal: FactorSignal) -> str:
    payload = signal.as_dict()
    payload["signal_id"] = ""
    payload["checksum"] = ""
    return _checksum(payload)


def _checksum(payload: dict[str, object]) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} 必须带时区")


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "FACTOR_LAB_CATALOG",
    "FACTOR_LAB_SCHEMA_VERSION",
    "ArtifactIntegrityError",
    "FactorDefinition",
    "FactorExperiment",
    "FactorExperimentPlan",
    "FactorExperimentStatus",
    "FactorLabError",
    "FactorPreference",
    "FactorRole",
    "FactorSignal",
    "FactorSignalItem",
    "FeatureFrequency",
    "FeatureMissingPolicy",
    "FeatureObservation",
    "FeatureSnapshot",
    "PointInTimeViolationError",
    "ResearchArtifactStatus",
    "SignalDirection",
    "SignalNotValidatedError",
    "build_factor_signal",
    "build_feature_snapshot",
    "factor_lab_catalog",
    "get_factor_definition",
    "new_factor_experiment",
    "update_factor_experiment",
]
