"""universe 预检与候选池诊断单元测试(issue #186)。

覆盖验收:预检命中(list_date 全 null 空池)/ 未命中(元数据齐备不误报)/
空池统计(多条件聚合 + 缺失字段具名)/ 价格字段不误报空池。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from finboard_backtest.strategy_spec import (
    MissingDataPolicy,
    UniverseRanking,
    UniverseSpec,
)
from finboard_backtest.strategy_spec.universe_precheck import (
    describe_empty_pool,
    is_st_at_decision,
    name_at_decision,
    preview_universe_pool,
    resolvable_feature_names,
)
from finboard_shared.types import AssetClass, Market


@dataclass(frozen=True)
class _Instrument:
    """最小编码的发布 instruments 鸭子类型(与 ReleasedInstrument 同字段)。"""

    code: str
    market: Market
    asset_class: AssetClass
    list_date: date | None
    delist_date: date | None = None
    suspended_sessions: int = 0
    coverage_pct: Decimal = Decimal("1.0")
    present_event_types: tuple[str, ...] = ()
    name: str | None = None
    name_history: tuple[tuple[str, date, date | None], ...] = ()


def _spec(**kwargs: object) -> UniverseSpec:
    defaults: dict[str, object] = {
        "markets": (Market.A_SHARE,),
        "asset_classes": (AssetClass.EQUITY,),
        "min_listing_days": 60,
        "selection_limit": 20,
    }
    defaults.update(kwargs)
    return UniverseSpec(**defaults)  # type: ignore[arg-type]


DECISION_DATE = date(2024, 6, 30)

_INST = tuple(
    _Instrument(
        code=code,
        market=Market.A_SHARE,
        asset_class=AssetClass.EQUITY,
        list_date=date(2020, 1, 1),
    )
    for code in ("600001.SH", "600002.SH", "600003.SH")
)


def test_empty_pool_when_list_date_all_null_min_listing_days_active() -> None:
    """预检命中:list_date 全 null + min_listing_days>0 → 空池,缺失字段指向 list_date。"""
    instruments = tuple(
        _Instrument(
            code=code,
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=None,
        )
        for code in ("600001.SH", "600002.SH", "600003.SH")
    )
    preview = preview_universe_pool(
        _spec(min_listing_days=60),
        instruments,
        decision_date=DECISION_DATE,
    )
    assert preview.total_candidates == 3
    assert preview.included == 0
    assert preview.is_empty is True
    assert preview.missing_fields == ("list_date",)
    assert preview.excluded_by_condition["listing_age_below_minimum"] == 3
    codes = {warning.code for warning in preview.warnings}
    assert "universe_listing_days_unavailable" in codes
    message = describe_empty_pool(preview, decision_date=DECISION_DATE)
    assert "list_date" in message
    assert "listing_age_below_minimum=3" in message


def test_pool_not_empty_when_metadata_complete() -> None:
    """预检未命中:list_date / 属性齐备时池非空,不产生任何缺失字段。"""
    preview = preview_universe_pool(
        _spec(min_listing_days=60),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 3
    assert preview.is_empty is False
    assert preview.missing_fields == ()
    assert not [w for w in preview.warnings if w.code == "universe_listing_days_unavailable"]


def test_min_listing_days_zero_does_not_empty_pool_on_null_list_date() -> None:
    """min_listing_days=0 时 list_date 缺失不影响空池,但不产生 listing warning。"""
    instruments = tuple(
        _Instrument(
            code=code,
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=None,
        )
        for code in ("600001.SH", "600002.SH")
    )
    preview = preview_universe_pool(
        _spec(min_listing_days=0),
        instruments,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 2
    assert preview.is_empty is False
    assert not [w for w in preview.warnings if w.code == "universe_listing_days_unavailable"]


def test_empty_pool_stats_aggregate_multiple_conditions() -> None:
    """空池统计:多过滤条件聚合计数,缺失字段去重具名。"""
    instruments = (
        _Instrument(
            code="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=None,
        ),
        _Instrument(
            code="600002.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=None,
            suspended_sessions=3,
        ),
        # 期货市场 + 缺失平均成交额特征。
        _Instrument(
            code="IF2609",
            market=Market.FUTURE,
            asset_class=AssetClass.DERIVATIVE,
            list_date=None,
        ),
    )
    preview = preview_universe_pool(
        _spec(
            min_listing_days=60,
            min_average_amount=5_000_000,
        ),
        instruments,
        decision_date=DECISION_DATE,
    )
    assert preview.is_empty is True
    stats = preview.excluded_by_condition
    assert stats["listing_age_below_minimum"] == 3
    assert stats["market_not_allowed"] == 1
    assert stats["asset_class_not_allowed"] == 1
    assert stats["missing_average_amount"] == 3
    assert stats["suspended"] == 1
    assert set(preview.missing_fields) == {"list_date", "average_amount"}


def test_average_amount_provided_does_not_exclude() -> None:
    """available_features 含 average_amount 时,min_average_amount 不产生排除。"""
    preview = preview_universe_pool(
        _spec(min_average_amount=5_000_000),
        _INST,
        decision_date=DECISION_DATE,
        available_features=frozenset({"average_amount", "momentum"}),
    )
    assert preview.included == 3
    assert "missing_average_amount" not in preview.excluded_by_condition


def test_price_filters_never_empties_pool_statically() -> None:
    """min/max_price 依赖决策日 close(运行时恒有),静态预览不误报空池。"""
    preview = preview_universe_pool(
        _spec(min_price=5.0, max_price=20.0),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 3
    assert preview.is_empty is False
    assert "missing_price" not in preview.excluded_by_condition


def test_st_and_delisted_filters_report_degradation_warnings() -> None:
    """exclude_st / exclude_delisted 在元数据缺失时产生具名降级 warning。"""
    preview = preview_universe_pool(
        _spec(exclude_st=True, exclude_delisted=True),
        _INST,
        decision_date=DECISION_DATE,
    )
    codes = {warning.code for warning in preview.warnings}
    assert "universe_st_filter_inactive" in codes
    assert "universe_delist_metadata_unavailable" in codes
    # delist_date 全空不判定为空池(缺失 ≠ 已退市)。
    assert preview.is_empty is False


def test_required_data_fields_unavailable_excludes_and_warns() -> None:
    """required_data_fields 引用无数据源字段 → 产生排除与具名 warning。"""
    preview = preview_universe_pool(
        _spec(required_data_fields=("pe_ttm",)),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 0
    assert preview.excluded_by_condition.get("missing_required_field:pe_ttm") == 3
    assert preview.missing_fields == ("pe_ttm",)
    codes = {warning.code for warning in preview.warnings}
    assert "universe_required_field_unavailable" in codes


def test_ranking_field_unavailable_exclude_policy_empties_pool() -> None:
    """ranking.field 无数据源 + missing_data_policy=exclude → 空池。"""
    preview = preview_universe_pool(
        _spec(
            ranking=UniverseRanking(field="average_amount"),
            missing_data_policy=MissingDataPolicy.EXCLUDE,
        ),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 0
    assert preview.excluded_by_condition.get("missing_ranking_field:average_amount") == 3
    assert "average_amount" in preview.missing_fields
    codes = {warning.code for warning in preview.warnings}
    assert "universe_ranking_field_unavailable" in codes


def test_ranking_field_unavailable_rank_worst_keeps_pool() -> None:
    """ranking.field 无数据源 + missing_data_policy=rank_worst → 不空池但 warning。"""
    preview = preview_universe_pool(
        _spec(
            ranking=UniverseRanking(field="average_amount"),
            missing_data_policy=MissingDataPolicy.RANK_WORST,
        ),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.is_empty is False
    assert "missing_ranking_field:average_amount" not in preview.excluded_by_condition
    assert any(
        warning.code == "universe_ranking_field_unavailable"
        for warning in preview.warnings
    )


def test_explicit_symbols_narrow_evaluation_domain() -> None:
    """explicit_symbols 声明后评估域收窄为 explicit ∩ 发布(#254)。

    声明域之外的标的不再参与静态评估:total_candidates 只计交集,
    排除统计不再产出全市场 ``not_in_explicit_symbols`` 噪音。
    """
    preview = preview_universe_pool(
        _spec(min_listing_days=0, explicit_symbols=("600001.SH",)),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.total_candidates == 1
    assert preview.included == 1
    assert preview.is_empty is False
    assert "not_in_explicit_symbols" not in preview.excluded_by_condition
    assert preview.explicit_total == 1
    assert preview.explicit_missing == ()
    assert not [w for w in preview.warnings if w.code == "universe_explicit_symbol_missing"]


def test_explicit_symbols_missing_from_release_warns() -> None:
    """声明但发布中缺失的 explicit 标的发具名 warning,不静默忽略(#254)。"""
    preview = preview_universe_pool(
        _spec(
            min_listing_days=0,
            explicit_symbols=("600001.SH", "688999.SH", "688998.SH"),
        ),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.total_candidates == 1
    assert preview.explicit_total == 3
    assert preview.explicit_missing == ("688998.SH", "688999.SH")
    warning = next(
        w for w in preview.warnings if w.code == "universe_explicit_symbol_missing"
    )
    assert warning.condition == "explicit_symbols"
    assert "688999.SH" in warning.message


def test_explicit_symbols_all_missing_judged_empty() -> None:
    """声明的 explicit 标的全部不在发布中:交集为空 → 判空池(#254)。"""
    preview = preview_universe_pool(
        _spec(min_listing_days=0, explicit_symbols=("688999.SH",)),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.total_candidates == 0
    assert preview.is_empty is True
    message = describe_empty_pool(preview, decision_date=DECISION_DATE)
    assert "explicit_symbols 声明的 1 个标的均不在" in message
    assert "688999.SH" in message


def test_explicit_symbols_missing_field_warnings_cover_declared_domain_only() -> None:
    """list_date 缺失统计只覆盖声明域:声明域外缺失不再计入(#254)。"""
    instruments = (
        _Instrument(
            code="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
        ),
        _Instrument(
            code="600002.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=None,
        ),
    )
    preview = preview_universe_pool(
        _spec(min_listing_days=60, explicit_symbols=("600001.SH",)),
        instruments,
        decision_date=DECISION_DATE,
    )
    # 600002.SH 声明域之外:其 list_date 缺失不触发 warning、不进缺失字段
    assert preview.included == 1
    assert preview.missing_fields == ()
    assert not [
        w for w in preview.warnings if w.code == "universe_listing_days_unavailable"
    ]


def test_excluded_event_types_and_coverage_filters() -> None:
    """事件与数据完整性过滤参与空池判定。"""
    instruments = (
        _Instrument(
            code="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            present_event_types=("forced_redemption",),
        ),
        _Instrument(
            code="600002.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            coverage_pct=Decimal("0.5"),
        ),
    )
    preview = preview_universe_pool(
        _spec(
            min_listing_days=0,
            excluded_event_types=("forced_redemption",),
            min_data_completeness=0.95,
        ),
        instruments,
        decision_date=DECISION_DATE,
    )
    assert preview.is_empty is True
    assert preview.excluded_by_condition["excluded_event"] == 1
    assert preview.excluded_by_condition["data_completeness_below_minimum"] == 1


def test_empty_instrument_manifest_is_not_judged_empty() -> None:
    """发布清单为空(无 instruments)属发布级问题,预览不做空池判定。"""
    preview = preview_universe_pool(
        _spec(),
        (),
        decision_date=DECISION_DATE,
    )
    assert preview.total_candidates == 0
    assert preview.is_empty is False


def test_resolvable_feature_names_unions_all_sources() -> None:
    """特征可解析集合 = 标准价格特征 + 规格 source + 快照观测名。"""
    names = resolvable_feature_names(
        feature_graph_sources=("pb", "momentum"),
        snapshot_feature_names=("custom_factor", "volatility_20d"),
    )
    assert "momentum" in names
    assert "volatility_20d" in names
    assert "pb" in names
    assert "custom_factor" in names
    assert "average_amount" not in names


# ---- issue #213:市值过滤与 ST 真实判定 ----


def test_market_cap_unavailable_warns_and_empties_pool_statically() -> None:
    """min_market_cap 声明但 market_cap 特征无数据源:具名 warning + 按缺失排除。"""
    preview = preview_universe_pool(
        _spec(min_market_cap=1e10),
        _INST,
        decision_date=DECISION_DATE,
    )
    assert preview.is_empty is True
    assert preview.excluded_by_condition["missing_market_cap"] == 3
    assert preview.missing_fields == ("market_cap",)
    warning = next(
        w for w in preview.warnings if w.code == "universe_market_cap_unavailable"
    )
    assert warning.field == "market_cap"
    assert warning.condition == "min_market_cap"


def test_market_cap_available_does_not_exclude_or_warn() -> None:
    """market_cap 在可解析特征集合中(快照/研究发布提供):不排除、不 warning。"""
    preview = preview_universe_pool(
        _spec(min_market_cap=1e10, max_market_cap=5e10),
        _INST,
        decision_date=DECISION_DATE,
        available_features=frozenset({"market_cap"}),
    )
    assert preview.included == 3
    assert "missing_market_cap" not in preview.excluded_by_condition
    assert not [w for w in preview.warnings if w.code == "universe_market_cap_unavailable"]


def test_st_pit_exact_from_name_history_excludes_without_warning() -> None:
    """name_history 覆盖决策日:PIT 精确判定 ST,静态排除且无降级 warning。"""
    instruments = (
        _Instrument(
            code="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            name="正常股份",
            name_history=(("正常股份", date(2020, 1, 1), None),),
        ),
        _Instrument(
            code="600002.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            name="ST问题股份",
            name_history=(("正常股份", date(2020, 1, 1), date(2024, 1, 1)), ("ST问题股份", date(2024, 1, 1), None)),
        ),
    )
    preview = preview_universe_pool(
        _spec(exclude_st=True),
        instruments,
        decision_date=DECISION_DATE,
    )
    assert preview.included == 1
    assert preview.excluded_by_condition["st_security"] == 1
    codes = {w.code for w in preview.warnings}
    assert "universe_st_filter_inactive" not in codes
    assert "universe_st_pit_approximate" not in codes


def test_st_fallback_current_name_reports_pit_approximate_warning() -> None:
    """name_history 不覆盖决策日:回退当前名称近似判定,发具名降级 warning。"""
    instruments = (
        _Instrument(
            code="600001.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            name="正常股份",
        ),
        _Instrument(
            code="600002.SH",
            market=Market.A_SHARE,
            asset_class=AssetClass.EQUITY,
            list_date=date(2020, 1, 1),
            name="ST问题股份",
        ),
    )
    preview = preview_universe_pool(
        _spec(exclude_st=True),
        instruments,
        decision_date=DECISION_DATE,
    )
    # 当前名称近似:ST 名称仍被排除,但发 PIT 近似 warning 而非静默。
    assert preview.included == 1
    assert preview.excluded_by_condition["st_security"] == 1
    warning = next(w for w in preview.warnings if w.code == "universe_st_pit_approximate")
    assert warning.field == "name_history"
    assert "2/2" in warning.message


def test_st_all_names_missing_keeps_inactive_warning() -> None:
    """name/name_history 全缺(测试 stub / 极端发布):ST 过滤不生效 warning。"""
    preview = preview_universe_pool(
        _spec(exclude_st=True),
        _INST,  # stub 无 name / name_history 属性
        decision_date=DECISION_DATE,
    )
    codes = {w.code for w in preview.warnings}
    assert "universe_st_filter_inactive" in codes
    assert "universe_st_pit_approximate" not in codes
    # 缺名称按非 ST 处理,不空池。
    assert preview.is_empty is False


def test_name_at_decision_interval_and_fallback_semantics() -> None:
    """PIT 名称区间语义:valid_from <= d < valid_to(半开),回退当前名称。"""
    instrument = _Instrument(
        code="600001.SH",
        market=Market.A_SHARE,
        asset_class=AssetClass.EQUITY,
        list_date=date(2020, 1, 1),
        name="ST样本股份",
        name_history=(
            ("样本股份", date(2020, 1, 1), date(2024, 1, 1)),
            ("ST样本股份", date(2024, 1, 1), None),
        ),
    )
    assert name_at_decision(instrument, date(2022, 6, 1)) == ("样本股份", True)
    # 边界:valid_to 排他,2024-01-01 落入第二条。
    assert name_at_decision(instrument, date(2024, 1, 1)) == ("ST样本股份", True)
    # PIT 正确性:历史说非 ST 时,即使当前名称是 ST 也不排除。
    assert is_st_at_decision(instrument, date(2022, 6, 1)) is False
    assert is_st_at_decision(instrument, date(2024, 6, 30)) is True

    no_history = _Instrument(
        code="600002.SH",
        market=Market.A_SHARE,
        asset_class=AssetClass.EQUITY,
        list_date=date(2020, 1, 1),
        name="ST样本股份",
    )
    assert name_at_decision(no_history, date(2022, 6, 1)) == ("ST样本股份", False)

    no_name = _Instrument(
        code="600003.SH",
        market=Market.A_SHARE,
        asset_class=AssetClass.EQUITY,
        list_date=date(2020, 1, 1),
    )
    assert name_at_decision(no_name, date(2022, 6, 1)) == (None, False)
    assert is_st_at_decision(no_name, date(2022, 6, 1)) is False


def test_resolvable_feature_names_includes_research_release_kinds() -> None:
    """attached 研究数据发布的派生特征计入可解析集合(防 multi_period 误报)。"""
    names = resolvable_feature_names(
        research_release_kinds=("daily_metrics", "financial_indicators")
    )
    assert {"market_cap", "pb", "turnover_rate"} <= names
    assert {"roe", "gross_profit_margin"} <= names
    # 未附加研究发布时不计入。
    assert "market_cap" not in resolvable_feature_names()
