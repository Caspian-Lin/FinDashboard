"""universe 预检与候选池诊断(issue #186)。

纯函数、零 DB / 磁盘依赖。输入是发布 instruments 的可选字段鸭子类型(至少
暴露 ``code`` / ``market`` / ``asset_class``,以及可选的 ``list_date`` /
``delist_date`` / ``suspended_sessions`` / ``coverage_pct`` /
``present_event_types``);``finboard_data.releases.ReleasedInstrument`` /
``ReleaseInstrumentSpec``、运行时冻结 provider 的 instruments 及测试 stub
均满足。

用途:

1. ``strategy_validate``(REST+MCP)静态预检:universe 过滤条件依赖的字段
   是否存在于发布 instruments 元数据 / 可解析特征,缺失返回具名 warning。
   这是 issue 背景中「list_date 全 null → listing_days=0 → 全排除」这类
   空转的静态发现门;
2. ``run_queue``(REST+MCP)入队同步候选池非空校验:空池秒级
   ``invalid_argument``,附各过滤条件的排除统计;
3. ``signal_engine`` 运行时降级 warning 与空池根因错误复用同一聚合逻辑
   (缺失字段名不再只有泛化报错)。

判定语义:静态预览只对「元数据 / 字段依赖」下结论 —— 假设决策价与特征数据
在运行时必然可用(价格来自发布 bars;特征来自冻结快照 / 多期重算),因此
``min_price`` / ``max_price`` 与特征类字段在**空池判定**上按可满足处理,
缺失时只给出具名 warning 而非误报空池;``list_date`` / ``delist_date`` /
``suspended_sessions`` / ``coverage_pct`` / ``present_event_types`` /
市场 / 资产类别是发布元数据,按确定性事实参与空池判定。

issue #213:``market_cap`` 与 ``average_amount`` 同为「特征依赖的内建字段」
—— 运行时值来自冻结快照 / 研究发布(``daily_metrics.total_market_cap``,
单位人民币元)的 ``market_cap`` 特征观测,取值可用性取决于数据源,不能仅凭
内建名判定可解析;``exclude_st`` 不再恒为不生效 —— 按发布 instruments 的
``name_history`` 区间取决策日名称做 PIT 判定(无覆盖区间回退当前名称近似,
缺名称数据按非 ST 处理),降级路径均发具名 warning 而非静默放行。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from finboard_backtest.strategy_spec.contracts import (
    MissingDataPolicy,
    UniverseSpec,
)
from finboard_backtest.strategy_spec.universe import UniverseCandidate
from finboard_data.releases import (
    RESEARCH_RELEASE_FEATURE_NAMES as _CANONICAL_RELEASE_FEATURE_NAMES,
)
from finboard_data.releases import (
    research_release_derived_features as _canonical_release_derived_features,
)

#: 多期回放由 ``build_price_feature_snapshot`` 从冻结发布每日重算的特征名。
#: 这些特征不需要预建因子快照,视为运行时必然可用。
STANDARD_PRICE_FEATURE_NAMES = frozenset(
    {
        "momentum",
        "volatility_20d",
        "volatility_60d",
        "volatility_120d",
        "downside_volatility",
    }
)

#: ``UniverseCandidate.value()`` 的内建字段(不依赖外部特征输入)。
#: 其中 ``average_amount`` / ``market_cap`` 特殊:其值在运行时来自特征观测,
#: 取值可用性取决于冻结快照 / 研究发布 / 重算是否提供,不能仅凭「内建名」
#: 判定为可解析。
_BUILTIN_FIELDS = frozenset(
    {"listing_days", "average_amount", "price", "market_cap", "data_completeness"}
)
_FEATURE_DEPENDENT_FIELDS = frozenset({"average_amount", "market_cap"})

#: 与 ``explain_universe`` 保持一致的原因命名,便于运行时/预检输出对齐。
_REASON_MARKET = "market_not_allowed"
_REASON_ASSET_CLASS = "asset_class_not_allowed"
_REASON_EXPLICIT = "not_in_explicit_symbols"
_REASON_LISTING = "listing_age_below_minimum"
_REASON_AVERAGE_AMOUNT = "missing_average_amount"
_REASON_MISSING_MARKET_CAP = "missing_market_cap"
_REASON_SUSPENDED = "suspended"
_REASON_DELISTED = "delisted"
_REASON_ST = "st_security"
_REASON_EVENT = "excluded_event"
_REASON_COMPLETENESS = "data_completeness_below_minimum"

#: 研究数据发布(daily_metrics / financial_indicators)在运行时经
#: ``frozen_loader._load_research_features`` → ``factors/extract.py`` 派生的
#: 特征名。attached 发布让这些特征在静态预检中视为可解析,防止
#: multi_period(无因子快照)误报空池。
#: issue #253:唯一事实来源移至 ``finboard_data.releases``(发布构造时按
#: kind 冻结 ``derived_features`` 元数据),此处按字符串 kind 值投影供既有
#: 调用方兼容,两份字面量不再各自维护。
RESEARCH_RELEASE_FEATURE_NAMES: dict[str, frozenset[str]] = {
    kind.value: names
    for kind, names in _CANONICAL_RELEASE_FEATURE_NAMES.items()
}


def research_release_derived_features(kinds: Iterable[object]) -> frozenset[str]:
    """附加研究数据发布的 kind → 运行时可派生特征名。"""
    return _canonical_release_derived_features(kinds)


def _attr(instrument: object, name: str, default: Any) -> Any:
    return getattr(instrument, name, default)


def explicit_symbol_domain[T](
    spec: UniverseSpec,
    instruments: Sequence[T],
) -> tuple[list[T], tuple[str, ...]]:
    """explicit_symbols 声明时把评估域收窄为 explicit ∩ 发布标的(issue #254)。

    声明 explicit 后对发布全市场(可能数千只)做静态评估只会产出
    ``not_in_explicit_symbols`` 海量噪音;评估域应跟随声明收窄。返回
    ``(收窄后的 instruments, 声明但发布中缺失的 explicit 标的)``;未声明
    explicit 时原样返回全量与空元组。缺失标的由调用方发具名 warning
    (静默忽略声明即候选池不受控)。静态预检与运行时候选构建共用本函数,
    两边评估域一致。
    """
    explicit = set(spec.explicit_symbols)
    if not explicit:
        return list(instruments), ()
    narrowed: list[T] = []
    present: set[str] = set()
    for item in instruments:
        code = str(_attr(item, "code", ""))
        if code in explicit:
            narrowed.append(item)
            present.add(code)
    return narrowed, tuple(sorted(explicit - present))


def is_benchmark_only_instrument(instrument: object) -> bool:
    """基准数据资产判定(issue #184/#256 指数;#267 期货主连)。

    ``instrument_type=index``(指数)与 ``instrument_type=futures``
    (期货主连,换月拼接序列)都只做基准 / 研究数据、不可撮合
    (AGENTS.md #184 边界;主连不可当作可成交合约,#267),不进研究运行
    候选池;发布 instruments 里必须携带它们(同一 bars 主发布承载基准
    行情),但候选枚举必须跳过。静态预检(``static_universe_candidates``)
    与运行时候选构建(``frozen_loader._build_candidates_and_lots``)共用
    本谓词,两边口径一致。入参兼容 ORM 行(str)与 ``InstrumentType``
    枚举。
    """
    value = _attr(instrument, "instrument_type", None)
    return value is not None and value in ("index", "futures")


def name_at_decision(instrument: object, decision_date: date) -> tuple[str | None, bool]:
    """决策日 PIT 名称:优先 ``name_history`` 覆盖区间,否则回退当前 ``name``。

    ``name_history`` 条目 ``(name, valid_from, valid_to)`` 按半开区间
    ``valid_from <= decision_date < (valid_to or +∞)`` 匹配。返回
    ``(名称, 是否 PIT 精确命中)``;名称与历史皆缺时 ``(None, False)``。
    """
    for entry in _attr(instrument, "name_history", ()) or ():
        try:
            name, valid_from, valid_to = entry
        except (TypeError, ValueError):
            continue
        if (
            isinstance(name, str)
            and isinstance(valid_from, date)
            and valid_from <= decision_date
            and (
                valid_to is None
                or (isinstance(valid_to, date) and decision_date < valid_to)
            )
        ):
            return name, True
    fallback = _attr(instrument, "name", None)
    if isinstance(fallback, str) and fallback:
        return fallback, False
    return None, False


def is_st_name(name: str) -> bool:
    """与 v1 selection 一致的 ST 判定:证券名称含 ``ST``(覆盖 ST / *ST / S*ST)。"""
    return "ST" in name.upper()


def is_st_at_decision(instrument: object, decision_date: date) -> bool:
    """决策日 ST 判定(PIT 名称;缺名称数据时按非 ST 处理,配具名 warning)。"""
    name, _ = name_at_decision(instrument, decision_date)
    return name is not None and is_st_name(name)


def static_universe_candidates(
    instruments: Sequence[object],
    *,
    decision_date: date,
) -> tuple[UniverseCandidate, ...]:
    """把发布 instruments 映射为 ``UniverseSpec`` 候选的静态近似。

    与 ``signal_engine._spec_universe_candidates`` 对齐:``listing_days``
    来自 ``list_date``(缺失为 0)、``delisted`` 来自 ``delist_date``、
    ``suspended`` 来自 ``suspended_sessions``、``is_st`` 按决策日名称
    PIT 判定(issue #213)。价格 / 特征未知,按 None 参与诊断(空池判定中
    按可满足处理,见模块 docstring)。
    """
    candidates: list[UniverseCandidate] = []
    for instrument in instruments:
        # issue #256:指数只做基准数据不可撮合,不进候选池(与运行时候选
        # 构建 ``_build_candidates_and_lots`` 共用 ``is_benchmark_only_instrument``,
        # 静态预检 / 空池门控与执行期口径一致)。
        if is_benchmark_only_instrument(instrument):
            continue
        market = _attr(instrument, "market", None)
        asset_class = _attr(instrument, "asset_class", None)
        if market is None or asset_class is None:
            continue
        list_date = _attr(instrument, "list_date", None)
        delist_date = _attr(instrument, "delist_date", None)
        suspended_sessions = _attr(instrument, "suspended_sessions", 0)
        coverage = _attr(instrument, "coverage_pct", None)
        candidates.append(
            UniverseCandidate(
                symbol=str(_attr(instrument, "code", "")),
                market=market,
                asset_class=asset_class,
                listing_days=(
                    max(0, (decision_date - list_date).days)
                    if list_date is not None
                    else 0
                ),
                average_amount=None,
                price=None,
                suspended=bool(suspended_sessions > 0),
                delisted=delist_date is not None and delist_date <= decision_date,
                is_st=is_st_at_decision(instrument, decision_date),
                active_events=tuple(_attr(instrument, "present_event_types", ()) or ()),
                data_completeness=float(coverage) if coverage is not None else 1.0,
                fields={},
            )
        )
    return tuple(candidates)


def resolvable_feature_names(
    *,
    feature_graph_sources: Sequence[str] = (),
    snapshot_feature_names: Sequence[str] = (),
    research_release_kinds: Sequence[object] = (),
) -> frozenset[str]:
    """运行时特征可解析名称集合。

    由四部分构成:多期重算的标准价格特征、规格特征图声明的 source、
    冻结因子快照的观测特征名(single_shot 由快照提供,多期为空)、
    附加研究数据发布可派生的特征名(issue #213,防止 multi_period 误报)。
    """
    names = set(STANDARD_PRICE_FEATURE_NAMES)
    names.update(feature_graph_sources)
    names.update(snapshot_feature_names)
    names.update(research_release_derived_features(research_release_kinds))
    return frozenset(names)


def _field_resolvable(
    field: str,
    *,
    available_features: frozenset[str],
) -> bool:
    if field in _BUILTIN_FIELDS:
        # ``price`` / ``listing_days`` / ``data_completeness`` 运行时恒有。
        return field not in _FEATURE_DEPENDENT_FIELDS or field in available_features
    return field in available_features


@dataclass(frozen=True, slots=True)
class UniversePrecheckWarning:
    """一条具名 universe 预检 warning(issue #186)。

    ``code`` 是稳定具名事件(``universe_listing_days_unavailable`` 等),
    ``condition`` 是触发它的 universe 条件(``min_listing_days``),
    ``field`` 是缺失或不可解析的数据字段名(``list_date``)。
    """

    code: str
    condition: str
    field: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "condition": self.condition,
            "field": self.field,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class UniversePoolPreview:
    """候选池静态预览结果:空池判定 + 排除统计 + 具名 warnings。

    ``excluded_by_condition`` 是『排除原因 → 被排除标的数』的聚合(与
    ``explain_universe`` 的原因命名一致)。``missing_fields`` 是导致排除的
    缺失字段名(去重、排序),供错误信息直接指向根因。

    issue #254:声明 ``explicit_symbols`` 时评估域收窄为 explicit ∩ 发布
    标的——``total_candidates`` 只覆盖该交集;``explicit_total`` 是声明数,
    ``explicit_missing`` 是声明但发布中缺失的标的(有界预览)。
    """

    total_candidates: int
    included: int
    excluded_by_condition: dict[str, int]
    missing_fields: tuple[str, ...]
    warnings: tuple[UniversePrecheckWarning, ...]
    explicit_total: int = 0
    explicit_missing: tuple[str, ...] = ()

    @property
    def excluded(self) -> int:
        return self.total_candidates - self.included

    @property
    def is_empty(self) -> bool:
        """候选池是否为空。

        仅当候选可评估(``total_candidates > 0``)时下结论:清单为空说明发布
        本身缺少 instruments,属发布级问题,不由本预览判空。声明了
        ``explicit_symbols`` 时例外(issue #254):评估域已收窄为 explicit ∩
        发布,交集为空意味着声明的标的全部不可交易,直接判空。
        """
        if self.explicit_total > 0:
            return self.included == 0
        return self.total_candidates > 0 and self.included == 0

    def as_dict(self) -> dict[str, object]:
        return {
            "total_candidates": self.total_candidates,
            "included": self.included,
            "excluded": self.excluded,
            "is_empty": self.is_empty,
            "excluded_by_condition": dict(sorted(self.excluded_by_condition.items())),
            "missing_fields": list(self.missing_fields),
            "warnings": [item.as_dict() for item in self.warnings],
            "explicit_total": self.explicit_total,
            "explicit_missing": list(self.explicit_missing[:20]),
        }


def _metadata_warnings(
    spec: UniverseSpec,
    instruments: Sequence[object],
    *,
    available_features: frozenset[str],
    decision_date: date,
) -> list[UniversePrecheckWarning]:
    """universe 条件依赖字段的缺失诊断(具名 warning,不抛错)。

    issue #254:声明 ``explicit_symbols`` 时评估域收窄为 explicit ∩ 发布
    标的(全市场评估只在未声明时进行);声明但发布中缺失的标的发具名
    warning,不做静默忽略。
    """
    warnings: list[UniversePrecheckWarning] = []
    instruments, missing_explicit = explicit_symbol_domain(spec, instruments)
    if missing_explicit:
        preview = "、".join(missing_explicit[:10])
        if len(missing_explicit) > 10:
            preview += f" 等 {len(missing_explicit)} 个"
        warnings.append(
            UniversePrecheckWarning(
                code="universe_explicit_symbol_missing",
                condition="explicit_symbols",
                field="dataset_release_instruments",
                message=(
                    f"explicit_symbols 声明的 {len(missing_explicit)} 个标的不在"
                    f"发布 instruments 中(评估域按 explicit ∩ 发布收窄): "
                    f"{preview};请核对代码书写或扩大数据发布覆盖范围"
                ),
            )
        )
    total = len(instruments)
    if total == 0:
        return warnings

    # issue #256:指数基准资产不进候选池(不可撮合),且无 list_date / 名称等
    # 主数据上游;元数据缺失统计只服务可撮合候选,把指数计入会让每个含基准
    # 指数的混发发布永久产出噪音 warning。
    instruments = [
        item for item in instruments if not is_benchmark_only_instrument(item)
    ]
    total = len(instruments)
    if total == 0:
        return warnings

    missing_listing = sum(
        1 for item in instruments if _attr(item, "list_date", None) is None
    )
    if spec.min_listing_days > 0 and missing_listing:
        all_missing = missing_listing == total
        warnings.append(
            UniversePrecheckWarning(
                code="universe_listing_days_unavailable",
                condition="min_listing_days",
                field="list_date",
                message=(
                    f"min_listing_days={spec.min_listing_days} 依赖 list_date;"
                    f"发布 instruments 的 list_date {missing_listing}/{total}"
                    f"{'全部为空' if all_missing else '为空'}"
                    f"(listing_days 将按 0 计),"
                    f"{'将过滤全部标的' if all_missing else '受影响的标的可能被过滤'}"
                ),
            )
        )

    missing_delist = sum(
        1 for item in instruments if _attr(item, "delist_date", None) is None
    )
    if spec.exclude_delisted and missing_delist == total:
        warnings.append(
            UniversePrecheckWarning(
                code="universe_delist_metadata_unavailable",
                condition="exclude_delisted",
                field="delist_date",
                message=(
                    "exclude_delisted 依赖 delist_date;发布 instruments 的 "
                    "delist_date 全部为空,退市过滤不生效"
                ),
            )
        )

    if spec.exclude_st:
        unknown = 0
        approximate = 0
        for item in instruments:
            name, pit_exact = name_at_decision(item, decision_date)
            if name is None:
                unknown += 1
            elif not pit_exact:
                approximate += 1
        if unknown == total:
            warnings.append(
                UniversePrecheckWarning(
                    code="universe_st_filter_inactive",
                    condition="exclude_st",
                    field="st_marker",
                    message=(
                        "exclude_st 依赖 ST 标记;发布 instruments 无名称数据"
                        "(name / name_history 均缺),ST 过滤不生效"
                    ),
                )
            )
        elif unknown or approximate:
            parts: list[str] = []
            if approximate:
                parts.append(
                    f"{approximate}/{total} 个标的决策日名称无 name_history"
                    "覆盖、回退当前名称近似判定"
                )
            if unknown:
                parts.append(
                    f"{unknown}/{total} 个标的缺名称数据、按非 ST 处理"
                    "(ST 过滤对这些标的不生效)"
                )
            warnings.append(
                UniversePrecheckWarning(
                    code="universe_st_pit_approximate",
                    condition="exclude_st",
                    field="name_history",
                    message=(
                        f"exclude_st 按名称历史 PIT 判定;{';'.join(parts)}"
                    ),
                )
            )

    if (
        spec.min_average_amount is not None
        and "average_amount" not in available_features
    ):
        warnings.append(
            UniversePrecheckWarning(
                code="universe_average_amount_unavailable",
                condition="min_average_amount",
                field="average_amount",
                message=(
                    f"min_average_amount={spec.min_average_amount} 依赖 "
                    "average_amount 特征;当前冻结快照/发布未提供该特征,"
                    "将按缺失过滤标的"
                ),
            )
        )

    if (
        (spec.min_market_cap is not None or spec.max_market_cap is not None)
        and "market_cap" not in available_features
    ):
        conditions = "/".join(
            name
            for name, value in (
                ("min_market_cap", spec.min_market_cap),
                ("max_market_cap", spec.max_market_cap),
            )
            if value is not None
        )
        warnings.append(
            UniversePrecheckWarning(
                code="universe_market_cap_unavailable",
                condition=conditions,
                field="market_cap",
                message=(
                    f"{conditions} 依赖 market_cap 特征"
                    "(daily_metrics.total_market_cap,单位人民币元);"
                    "当前冻结快照/发布未提供该特征,将按缺失过滤标的"
                ),
            )
        )

    non_builtin_required = set(spec.required_data_fields) - _BUILTIN_FIELDS
    for field in sorted(non_builtin_required):
        if not _field_resolvable(field, available_features=available_features):
            warnings.append(
                UniversePrecheckWarning(
                    code="universe_required_field_unavailable",
                    condition="required_data_fields",
                    field=field,
                    message=(
                        f"required_data_fields 声明字段 {field!r},但发布字段 / "
                        "冻结特征均未提供该字段,该条件将按缺失过滤标的"
                    ),
                )
            )

    rank = spec.ranking
    if (
        rank is not None
        and not _field_resolvable(rank.field, available_features=available_features)
    ):
        policy = (
            "将过滤全部标的"
            if spec.missing_data_policy is MissingDataPolicy.EXCLUDE
            else "缺失标的将无法参与排名"
        )
        warnings.append(
            UniversePrecheckWarning(
                code="universe_ranking_field_unavailable",
                condition="ranking.field",
                field=rank.field,
                message=(
                    f"ranking 按字段 {rank.field!r} 排序,但发布字段 / "
                    f"冻结特征均未提供该字段,{policy}"
                ),
            )
        )
    return warnings


def universe_filter_warnings(
    spec: UniverseSpec,
    instruments: Sequence[object],
    *,
    available_features: frozenset[str],
    decision_date: date,
) -> tuple[UniversePrecheckWarning, ...]:
    """运行时 / 静态共用的过滤降级 warning(待生效与否的具名声明)。

    ``signal_engine`` 在每次决策时用真实特征集合调用本函数并逐条
    ``logger.warning``,对齐快照链路的「降级为不生效」语义(issue #186)。
    """
    return tuple(
        _metadata_warnings(
            spec,
            instruments,
            available_features=available_features,
            decision_date=decision_date,
        )
    )


def preview_universe_pool(
    spec: UniverseSpec,
    instruments: Sequence[object],
    *,
    decision_date: date,
    available_features: frozenset[str] = frozenset(STANDARD_PRICE_FEATURE_NAMES),
) -> UniversePoolPreview:
    """评估发布 instruments 在给定决策日下的静态候选池。

    只对确定性元数据条件下结论(见模块 docstring);``price`` /
    ``average_amount`` / ``market_cap`` 等运行时数据按可满足处理
    (特征不可解析时按缺失参与空池判定,对齐运行时行为),缺失只产生 warning。

    issue #254:声明 ``explicit_symbols`` 时评估域收窄为 explicit ∩ 发布
    标的——排除统计只反映声明域内的过滤,不再产出全市场的
    ``not_in_explicit_symbols`` 噪音;声明但发布中缺失的标的发具名 warning。
    """
    domain, explicit_missing = explicit_symbol_domain(spec, instruments)
    candidates = static_universe_candidates(domain, decision_date=decision_date)
    warnings = _metadata_warnings(
        spec,
        instruments,
        available_features=available_features,
        decision_date=decision_date,
    )

    excluded_events = set(spec.excluded_event_types)
    list_date_by_symbol = {
        str(_attr(item, "code", "")): _attr(item, "list_date", None)
        for item in domain
    }
    missing_fields: set[str] = set()
    exclusions: Counter[str] = Counter()
    included = 0

    for candidate in candidates:
        reasons: list[str] = []
        if candidate.market not in spec.markets:
            reasons.append(_REASON_MARKET)
        if candidate.asset_class not in spec.asset_classes:
            reasons.append(_REASON_ASSET_CLASS)
        if candidate.listing_days < spec.min_listing_days:
            reasons.append(_REASON_LISTING)
            if list_date_by_symbol.get(candidate.symbol) is None:
                missing_fields.add("list_date")
        if (
            spec.min_average_amount is not None
            and "average_amount" not in available_features
        ):
            reasons.append(_REASON_AVERAGE_AMOUNT)
            missing_fields.add("average_amount")
        if (
            (spec.min_market_cap is not None or spec.max_market_cap is not None)
            and "market_cap" not in available_features
        ):
            reasons.append(_REASON_MISSING_MARKET_CAP)
            missing_fields.add("market_cap")
        if spec.exclude_suspended and candidate.suspended:
            reasons.append(_REASON_SUSPENDED)
        if spec.exclude_delisted and candidate.delisted:
            reasons.append(_REASON_DELISTED)
        if spec.exclude_st and candidate.is_st:
            reasons.append(_REASON_ST)
        if excluded_events.intersection(candidate.active_events):
            reasons.append(_REASON_EVENT)
        if candidate.data_completeness < spec.min_data_completeness:
            reasons.append(_REASON_COMPLETENESS)
        for field in spec.required_data_fields:
            if not _field_resolvable(field, available_features=available_features):
                reasons.append(f"missing_required_field:{field}")
                missing_fields.add(field)
        rank = spec.ranking
        if (
            rank is not None
            and not _field_resolvable(rank.field, available_features=available_features)
            and spec.missing_data_policy is MissingDataPolicy.EXCLUDE
        ):
            reasons.append(f"missing_ranking_field:{rank.field}")
            missing_fields.add(rank.field)

        if not reasons:
            included += 1
        exclusions.update(reasons)

    return UniversePoolPreview(
        total_candidates=len(candidates),
        included=included,
        excluded_by_condition=dict(exclusions),
        missing_fields=tuple(sorted(missing_fields)),
        warnings=tuple(warnings),
        explicit_total=len(spec.explicit_symbols),
        explicit_missing=explicit_missing,
    )


def describe_empty_pool(
    preview: UniversePoolPreview,
    *,
    decision_date: date,
) -> str:
    """把空池预览格式化为可读的失败信息(入队拒绝 / 运行时错误共用)。"""
    if preview.explicit_total > 0 and preview.total_candidates == 0:
        return (
            f"explicit_symbols 声明的 {preview.explicit_total} 个标的均不在"
            f"数据发布 instruments 中(决策日 {decision_date.isoformat()}),"
            f"候选池为空。缺失标的: "
            f"{'、'.join(preview.explicit_missing[:20]) or '无'}。"
            "请核对代码书写或扩大数据发布覆盖范围。"
        )
    stats = "、".join(
        f"{reason}={count}"
        for reason, count in sorted(preview.excluded_by_condition.items())
    )
    missing = "、".join(preview.missing_fields) if preview.missing_fields else "无"
    explicit_note = ""
    if preview.explicit_total > 0:
        explicit_note = (
            f"(explicit_symbols 声明 {preview.explicit_total} 个,"
            f"其中 {len(preview.explicit_missing)} 个不在发布中)"
        )
    return (
        f"候选池为空(决策日 {decision_date.isoformat()} 共 "
        f"{preview.total_candidates} 个候选标的全部被过滤){explicit_note}。"
        f"排除统计: {stats or '无'};缺失字段: {missing}。"
        "请检查数据集发布是否含 instrument 元数据(如 list_date,可通过 "
        "data_sync 的 profiles 回填),或放宽 universe 过滤条件。"
    )


__all__ = [
    "RESEARCH_RELEASE_FEATURE_NAMES",
    "STANDARD_PRICE_FEATURE_NAMES",
    "UniversePoolPreview",
    "UniversePrecheckWarning",
    "describe_empty_pool",
    "explicit_symbol_domain",
    "is_benchmark_only_instrument",
    "is_st_at_decision",
    "is_st_name",
    "name_at_decision",
    "preview_universe_pool",
    "research_release_derived_features",
    "resolvable_feature_names",
    "static_universe_candidates",
    "universe_filter_warnings",
]
