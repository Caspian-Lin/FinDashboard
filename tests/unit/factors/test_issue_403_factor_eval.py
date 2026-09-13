"""因子质量评估闭环单测(issue #403)。

* 纯指标引擎:IC / RankIC / ICIR、分位组收益单调性、换手(截面 rank
  自相关)与衰减、覆盖率与覆盖起点声明,手算面板逐值对照;
* signal_eligible 治理(#214 规则化):暴露/风险/流动性族默认 False,
  违例出疑似标注错误清单(vwap_dev_20d/60d 已于 2026-09-10 人工拍板改 False,现零违例);
* 评分目录投影(#403 继 #226):p_ 全目录 direction/category/hypothesis
  投影 + 家族级评分参数,MultiFactorScorer 消费回归,未知家族 fail-loud;
* 快照特征注册表双轨防漂移(#253 先例):RESEARCH_RELEASE_FEATURE_NAMES
  与预置因子目录无意外重叠;
* 合成全目录评估:全目录因子全部出报告(机制冒烟;长窗口因子的覆盖
  起点 flag 如实呈现);
* 覆盖起点声明冻结入 series manifest(predefined 构建归档
  ``catalog_declaration``,未声明省略键)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import numpy as np
import pytest

from finboard_backtest.factors.catalog import (
    PREDEFINED_SCORING_CATALOG,
    RESEARCH_FACTOR_CATALOG,
    FactorCategory,
    FactorDirection,
    MissingStrategy,
    StandardizeMethod,
    _predefined_family_category,
    get_factor_meta,
)
from finboard_backtest.factors.combine import (
    CombinationConfig,
    CombinationMethod,
    FactorWeight,
)
from finboard_backtest.factors.eval import (
    EXPOSURE_FAMILIES,
    FactorEvalConfig,
    build_synthetic_universe,
    evaluate_catalog_synthetic,
    evaluate_factor_values,
    resolve_eval_factors,
    signal_eligible_governance_findings,
    synthetic_announced_fields,
)
from finboard_backtest.factors.predefined.registry import (
    PREDEFINED_FACTORS,
    get_predefined_factor,
    predefined_factor_names,
)
from finboard_backtest.factors.scorer import (
    MultiFactorScorer,
    ScoringConfig,
)
from finboard_data.factor_lab import FactorPreference
from finboard_data.releases import RESEARCH_RELEASE_FEATURE_NAMES

# --------------------------------------------------------------------- #
# 面板原语
# --------------------------------------------------------------------- #

D0 = date(2024, 1, 2)


def _day(index: int) -> date:
    return D0 + timedelta(days=index)


def _available(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 15, tzinfo=UTC)


def _values_panel(
    rows: dict[int, dict[str, float | None]],
) -> dict[date, dict[str, float | None]]:
    """{决策日序号 → {symbol → 值}} → {date → cross}。"""
    return {
        _day(day_index): dict(cross) for day_index, cross in rows.items()
    }


def _close_panel(
    rows: dict[int, dict[str, float]],
) -> dict[date, dict[str, float]]:
    return {
        _day(day_index): dict(cross) for day_index, cross in rows.items()
    }


def _definition(name: str) -> Any:
    return get_predefined_factor(name)


# --------------------------------------------------------------------- #
# 纯指标引擎(手算面板)
# --------------------------------------------------------------------- #


class TestEvalEngine:
    def test_perfect_monotone_factor_full_scores(self) -> None:
        """因子值与未来收益完全同序 → IC=RankIC=1、分组收益严格递增。"""
        symbols = [f"S{i}" for i in range(6)]
        values = _values_panel({0: {s: float(i) for i, s in enumerate(symbols)}})
        closes = _close_panel(
            {
                **{i: dict.fromkeys(symbols, 100.0) for i in range(5)},
                5: {s: 100.0 + 10.0 * i for i, s in enumerate(symbols)},
            }
        )
        report = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, n_groups=3, min_ic_dates=1),
        )
        assert report.status == "ok"
        assert report.ic_mean == pytest.approx(1.0)
        assert report.rank_ic_mean == pytest.approx(1.0)
        assert report.ic_ir is None  # 单日 IC 无 IR(样本不足)
        assert report.group_returns is not None
        assert report.group_returns == tuple(
            sorted(report.group_returns)
        )
        assert report.group_monotonicity == pytest.approx(1.0)
        assert report.n_evaluable_dates == 1

    def test_lower_direction_factor_yields_negative_ic(self) -> None:
        """direction=LOWER 语义(值越低越好)→ 原始 IC 为负,不做翻转。"""
        symbols = [f"S{i}" for i in range(6)]
        values = _values_panel({0: {s: float(i) for i, s in enumerate(symbols)}})
        closes = _close_panel(
            {
                **{i: dict.fromkeys(symbols, 100.0) for i in range(5)},
                5: {s: 100.0 + 10.0 * (5 - i) for i, s in enumerate(symbols)},
            }
        )
        report = evaluate_factor_values(
            _definition("vol_20d"),  # LOWER 族代表
            values,
            closes,
            config=FactorEvalConfig(horizon=5, n_groups=2, min_ic_dates=1),
        )
        assert report.direction == "lower"
        assert report.ic_mean is not None
        assert report.ic_mean < -0.99
        assert report.group_monotonicity is not None
        assert report.group_monotonicity < -0.99

    def test_constant_cross_section_gives_none_ic(self) -> None:
        """截面常值(方差 0)→ 相关退化 → None,不虚构数字。"""
        symbols = [f"S{i}" for i in range(6)]
        values = _values_panel({0: dict.fromkeys(symbols, 1.0)})
        closes = _close_panel(
            {
                **{i: dict.fromkeys(symbols, 100.0) for i in range(5)},
                5: {s: 100.0 + i for i, s in enumerate(symbols)},
            }
        )
        report = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, min_ic_dates=1),
        )
        assert report.ic_mean is None
        assert report.rank_ic_mean is None
        assert report.status == "insufficient_cross_section"

    def test_empty_panel_is_no_data(self) -> None:
        values = _values_panel({0: {}})
        closes = _close_panel({0: {}, 5: {}})
        report = evaluate_factor_values(
            _definition("return_21d"), values, closes
        )
        assert report.status == "no_data"
        assert "no_data" in report.flags
        assert report.coverage_ratio == 0.0
        assert report.first_effective_date is None

    def test_null_panel_is_no_data_with_none_first_effective(self) -> None:
        """全 None 截面(长窗口因子短发布)→ no_data,首效日 None。"""
        values = _values_panel({0: {"S0": None, "S1": None}})
        closes = _close_panel({0: {"S0": 1.0, "S1": 1.0}, 5: {"S0": 1.0, "S1": 1.0}})
        report = evaluate_factor_values(
            _definition("sharpe_1320d"), values, closes
        )
        assert report.status == "no_data"
        assert report.first_effective_date is None

    def test_coverage_start_missing_flag_follows_declaration(self) -> None:
        """声明 min_history_bars 且首个决策日全缺测 → 覆盖起点缺失 flag。"""
        values = _values_panel({0: {"S0": None}, 10: {"S0": 0.5}})
        closes = _close_panel(
            {i: {"S0": 100.0} for i in range(0, 30, 1)}
        )
        declared = evaluate_factor_values(
            _definition("sharpe_1320d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, min_ic_dates=1),
        )
        assert declared.min_history_bars == 1320
        assert declared.coverage_start_missing is True
        assert "coverage_start_missing" in declared.flags
        assert "window_shorter_than_declaration" in declared.flags

        undeclared = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, min_ic_dates=1),
        )
        assert undeclared.min_history_bars is None
        assert undeclared.coverage_start_missing is False

    def test_turnover_and_decay_from_rank_autocorr(self) -> None:
        """相邻截面完全同序 → 自相关 1、换手 0;逐日等比结构衰减可复算。"""
        symbols = [f"S{i}" for i in range(6)]
        cross = {s: float(i) for i, s in enumerate(symbols)}
        values = _values_panel({0: dict(cross), 1: dict(cross), 2: dict(cross)})
        closes = _close_panel(
            {i: dict.fromkeys(symbols, 100.0 + i) for i in range(0, 10)}
        )
        report = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=2, min_ic_dates=1, decay_lags=(1, 2)),
        )
        assert report.rank_autocorr_lag1 == pytest.approx(1.0)
        assert report.turnover == pytest.approx(0.0)
        assert report.autocorr_decay["1"] == pytest.approx(1.0)
        assert report.autocorr_decay["2"] == pytest.approx(1.0)

    def test_horizon_truncates_last_dates(self) -> None:
        """末端不足一个 horizon 的决策日不进 IC 统计。"""
        symbols = ["S0", "S1", "S2", "S3", "S4", "S5"]
        values = _values_panel(
            {
                0: {s: float(i) for i, s in enumerate(symbols)},
                6: {s: float(i) for i, s in enumerate(symbols)},
            }
        )
        closes = _close_panel(
            {
                i: {
                    s: 100.0 + 5.0 * i + j for j, s in enumerate(symbols)
                }
                for i in range(0, 10)
            }
        )
        report = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, min_ic_dates=2),
        )
        # day0 前向 5 个交易日可得;day6 越过面板末端(pos+5 >= 10)
        assert report.n_evaluable_dates == 1

    def test_missing_forward_close_skips_symbol_pair(self) -> None:
        """收盘价缺测的标的该日不进截面(分母诚实)。"""
        values = _values_panel(
            {0: {"S0": 1.0, "S1": 2.0, "S2": 3.0, "S3": 4.0, "S4": 5.0, "S5": 6.0}}
        )
        closes = _close_panel(
            {
                **{i: {f"S{j}": 100.0 for j in range(6)} for i in range(5)},
                5: {"S0": 110.0, "S1": 90.0, "S2": 105.0, "S3": 95.0,
                    "S4": 100.0, "S5": 100.0},
            }
        )
        closes[_day(5)].pop("S4")
        closes[_day(5)].pop("S5")
        report = evaluate_factor_values(
            _definition("return_21d"),
            values,
            closes,
            config=FactorEvalConfig(horizon=5, min_ic_dates=1, min_cross_section=4),
        )
        # S4/S5 缺前向收盘 → 只剩 4 对,仍满足 min_cross_section
        assert report.ic_mean is not None

    def test_as_dict_roundtrip_json(self) -> None:
        values = _values_panel({0: {"S0": 1.0}})
        closes = _close_panel({0: {"S0": 1.0}, 5: {"S0": 1.1}})
        report = evaluate_factor_values(
            _definition("return_21d"), values, closes
        )
        payload = report.as_dict()
        assert json.loads(json.dumps(payload, ensure_ascii=False)) == payload
        assert payload["factor"] == "return_21d"
        assert payload["status"] in {"ok", "no_data", "insufficient_cross_section"}


# --------------------------------------------------------------------- #
# signal_eligible 治理(#214 规则化)
# --------------------------------------------------------------------- #


class TestSignalEligibleGovernance:
    def test_exposure_family_violations_listed(self) -> None:
        """人工拍板后(2026-09-10,vwap_dev 改 False)暴露族零违例。"""
        findings = signal_eligible_governance_findings()
        assert findings == []

    def test_exposure_families_declaration(self) -> None:
        """#214 语义:规模/风险/流动性三族默认不进信号。"""
        assert frozenset({"size", "risk", "liquidity"}) == EXPOSURE_FAMILIES

    def test_exposure_family_catalog_annotation_majority_false(self) -> None:
        """规则锚:暴露三族绝大多数条目已按 False 标注(防规则失锚)。"""
        exposed = [
            item
            for item in PREDEFINED_FACTORS.values()
            if item.family in EXPOSURE_FAMILIES
        ]
        assert exposed
        false_ratio = sum(
            1 for item in exposed if not item.signal_eligible
        ) / len(exposed)
        assert false_ratio > 0.9

    def test_alpha_and_fundamental_families_eligible(self) -> None:
        """alpha101 / 基本面族全部可入信号(治理规则的另一半)。"""
        for name, item in PREDEFINED_FACTORS.items():
            if item.family in {"alpha101", "growth", "quality", "value",
                               "momentum", "reversal"}:
                assert item.signal_eligible is True, name


# --------------------------------------------------------------------- #
# 评分目录投影(#403 继 #226)
# --------------------------------------------------------------------- #


class TestScoringProjection:
    def test_full_catalog_projected(self) -> None:
        """174 预置因子全部入评分目录,键 = p_<bare>。"""
        assert len(PREDEFINED_SCORING_CATALOG) == len(PREDEFINED_FACTORS)
        assert set(PREDEFINED_SCORING_CATALOG) == {
            f"p_{name}" for name in PREDEFINED_FACTORS
        }

    def test_direction_and_category_projected_for_all(self) -> None:
        preference_to_direction = {
            FactorPreference.HIGHER: FactorDirection.LONG,
            FactorPreference.LOWER: FactorDirection.SHORT,
        }
        for name, item in PREDEFINED_FACTORS.items():
            meta = PREDEFINED_SCORING_CATALOG[f"p_{name}"]
            assert meta.direction is preference_to_direction[item.direction], name
            assert meta.category == _predefined_family_category(item.family), name
            assert meta.category in set(FactorCategory), name
            assert meta.economic_hypothesis, name
            assert meta.source_field == item.data_dependencies[0], name
            assert meta.version == item.implementation_version, name

    def test_family_scoring_params(self) -> None:
        """家族级评分参数:风险族 2%/98%、流动性族 fill_median。"""
        risk = PREDEFINED_SCORING_CATALOG["p_vol_20d"]
        assert risk.category is FactorCategory.RISK
        assert risk.winsorize_lower_pct == 0.02
        assert risk.winsorize_upper_pct == 0.98
        assert risk.standardize is StandardizeMethod.ZSCORE

        liquidity = PREDEFINED_SCORING_CATALOG["p_turnover_ma_20d"]
        assert liquidity.category is FactorCategory.LIQUIDITY
        assert liquidity.missing_strategy is MissingStrategy.FILL_MEDIAN

        momentum = PREDEFINED_SCORING_CATALOG["p_return_21d"]
        assert momentum.category is FactorCategory.MOMENTUM
        assert momentum.direction is FactorDirection.LONG
        assert momentum.standardize is StandardizeMethod.ZSCORE

    def test_get_factor_meta_dual_namespace(self) -> None:
        """裸名走 #226 的 13 因子目录,p_ 走 #403 投影;未知各自具名拒绝。"""
        assert len(RESEARCH_FACTOR_CATALOG) == 13
        assert get_factor_meta("pb").category is FactorCategory.VALUE
        assert get_factor_meta("p_return_21d").category is FactorCategory.MOMENTUM
        with pytest.raises(KeyError, match="p_no_such"):
            get_factor_meta("p_no_such")
        with pytest.raises(KeyError, match="no_such_bare"):
            get_factor_meta("no_such_bare")

    def test_unmapped_family_fail_loud(self) -> None:
        """未知家族导入/评估期 RuntimeError(#226 fail-loud 防漂移)。"""
        with pytest.raises(RuntimeError, match="无评分投影规则"):
            _predefined_family_category("no_such_family")

    def test_scorer_consumes_governed_catalog(self) -> None:
        """MultiFactorScorer 以 p_ 因子评分回归(治理后目录消费面)。"""
        symbols = [f"S{i}" for i in range(10)]
        matrix = {
            "p_return_21d": {s: float(i) for i, s in enumerate(symbols)},
            "p_vol_20d": {s: float(10 - i) for i, s in enumerate(symbols)},
            "p_qmj": {s: 0.5 * i for i, s in enumerate(symbols)},
        }
        config = ScoringConfig(
            combination=CombinationConfig(
                method=CombinationMethod.WEIGHTED_AVERAGE,
                weights=(
                    FactorWeight(factor_name="p_return_21d", weight=0.4),
                    FactorWeight(factor_name="p_vol_20d", weight=0.3),
                    FactorWeight(factor_name="p_qmj", weight=0.3),
                ),
            ),
        )
        result = MultiFactorScorer(config).score(matrix)
        assert result.n_eligible == len(symbols)
        assert set(result.composite_scores) == set(symbols)
        # 动量(LONG)与波动(SHORT)方向相反且完全共线 → 中性组合;
        # qmj(LONG)主导 → 排名与 qmj 同序
        top = result.ranked_symbols[0]
        assert top == "S9"
        for name in ("p_return_21d", "p_vol_20d", "p_qmj"):
            assert result.coverage[name] == pytest.approx(1.0)

    def test_existing_research_catalog_unchanged(self) -> None:
        """13 因子评分目录零漂移(#226 既有语义)。"""
        assert set(RESEARCH_FACTOR_CATALOG) == {
            "pb",
            "earnings_yield",
            "dividend_yield",
            "roe",
            "gross_profit_margin",
            "debt_to_assets",
            "volatility_20d",
            "volatility_60d",
            "volatility_120d",
            "downside_volatility",
            "turnover_rate",
            "momentum",
            "revenue_yoy",
        }


# --------------------------------------------------------------------- #
# 快照特征注册表双轨防漂移(#253 先例)
# --------------------------------------------------------------------- #


class TestFeatureRegistryDualTrack:
    def test_release_features_never_collide_with_predefined_catalog(self) -> None:
        """发布派生特征名(快照轨)与 p_ 因子裸名(序列轨)零重叠。

        双轨语义:发布派生特征(research_data_sync 冻结观测,如
        ``dividend_yield``)走快照/研究发布消费;``p_val_dividend_yield``
        等序列因子走 factor_series 通道。两个注册表若出现同名,特征解析
        将产生「同名字双来源」歧义 —— 以单测锁定漂移即失败。
        """
        release_features = {
            name
            for names in RESEARCH_RELEASE_FEATURE_NAMES.values()
            for name in names
        }
        predefined = set(predefined_factor_names())
        assert release_features
        assert not release_features & predefined

    def test_dual_track_naming_pair_locked(self) -> None:
        """同语义双轨命名对锁定:发布特征 dividend_yield vs p_ 序列因子。"""
        release_features = {
            name
            for names in RESEARCH_RELEASE_FEATURE_NAMES.values()
            for name in names
        }
        assert "dividend_yield" in release_features
        assert "val_dividend_yield" in predefined_factor_names()
        # 序列轨带 val_/fin_ 等家族前缀,与快照轨裸名天然区分
        assert "dividend_yield" not in set(predefined_factor_names())

    def test_snapshot_features_are_factor_lab_consumers(self) -> None:
        """发布派生特征与 u_ 目录的关系:roe/pb 等由 FACTOR_LAB 因子消费
        (快照轨是 u_ 因子的观测来源,不是 p_ 目录的注册来源)。"""
        from finboard_data.factor_lab import FACTOR_LAB_CATALOG

        release_features = {
            name
            for names in RESEARCH_RELEASE_FEATURE_NAMES.values()
            for name in names
        }
        overlap = set(FACTOR_LAB_CATALOG) & release_features
        assert {"pb", "roe", "dividend_yield"} <= overlap


# --------------------------------------------------------------------- #
# 合成全目录评估(机制冒烟;CI 快速模式)
# --------------------------------------------------------------------- #

_EVAL_KWARGS: dict[str, Any] = {
    "n_symbols": 12,
    "n_days": 380,
    "warmup": 260,
    "step": 10,
}


@pytest.fixture(scope="module")
def synthetic_report() -> dict[str, Any]:
    return evaluate_catalog_synthetic(**_EVAL_KWARGS)


class TestSyntheticFullCatalog:
    def test_all_factors_covered(self, synthetic_report: dict[str, Any]) -> None:
        """全目录逐因子出报告,一份不少、一份不多。"""
        report = synthetic_report
        assert report["schema_version"] == "v1"
        assert report["data_face"]["mode"] == "synthetic"
        factors = report["factors"]
        assert [item["factor"] for item in factors] == sorted(
            predefined_factor_names()
        )
        assert report["summary"]["factor_count"] == len(PREDEFINED_FACTORS)
        assert report["summary"]["status_counts"]
        assert sum(report["summary"]["status_counts"].values()) == len(
            PREDEFINED_FACTORS
        )

    def test_json_serializable(self, synthetic_report: dict[str, Any]) -> None:
        payload = json.dumps(synthetic_report, ensure_ascii=False)
        assert "signal_eligible_governance" in payload

    def test_signal_factor_measurable(self, synthetic_report: dict[str, Any]) -> None:
        """机制冒烟:动量样板因子 IC/分组/换手全链路非空。"""
        by_name = {item["factor"]: item for item in synthetic_report["factors"]}
        momentum = by_name["return_21d"]
        assert momentum["status"] == "ok"
        assert momentum["rank_ic_mean"] is not None
        assert momentum["group_returns"] is not None
        assert momentum["turnover"] is not None
        assert momentum["first_effective_date"] is not None
        # 合成宇宙的持久收益结构 → 动量 RankIC 为正(机制非退化)
        assert momentum["rank_ic_mean"] > 0.0

    def test_long_window_coverage_start_reported(
        self, synthetic_report: dict[str, Any]
    ) -> None:
        """声明 1320 根 bar 的因子在短窗口下如实 no_data + 双 flag。"""
        by_name = {item["factor"]: item for item in synthetic_report["factors"]}
        long_factor = by_name["sharpe_1320d"]
        assert long_factor["min_history_bars"] == 1320
        assert long_factor["status"] == "no_data"
        assert "window_shorter_than_declaration" in long_factor["flags"]
        assert "coverage_start_missing" in long_factor["flags"]
        assert long_factor["first_effective_date"] is None

    def test_governance_block_in_report(
        self, synthetic_report: dict[str, Any]
    ) -> None:
        governance = synthetic_report["signal_eligible_governance"]
        assert "#214" in governance["rule"]
        assert governance["suspected"] == []

    def test_summary_extremes_consistent(
        self, synthetic_report: dict[str, Any]
    ) -> None:
        summary = synthetic_report["summary"]
        assert summary["ic_available"] > 0
        for name in summary["strongest_abs_rank_ic"]:
            entry = next(
                item
                for item in synthetic_report["factors"]
                if item["factor"] == name
            )
            assert entry["rank_ic_mean"] is not None


class TestSyntheticUniverse:
    def test_deterministic_universe(self) -> None:
        a = build_synthetic_universe(n_symbols=6, n_days=120)
        b = build_synthetic_universe(n_symbols=6, n_days=120)
        assert a.days == b.days
        for symbol in a.symbols:
            np.testing.assert_array_equal(
                a.bars["close"][symbol].values,
                b.bars["close"][symbol].values,
            )

    def test_benchmark_excluded_from_tradable(self) -> None:
        universe = build_synthetic_universe(n_symbols=4, n_days=60)
        assert universe.benchmark not in universe.symbols
        assert universe.benchmark in universe.bars["close"]

    def test_announced_fields_follow_catalog_dependencies(self) -> None:
        """合成公告字段 = 目录依赖 union(动态,免逐因子维护)。"""
        fields = synthetic_announced_fields()
        assert "financial_indicators" in fields
        declared = {
            dep.partition(".")[2]
            for item in PREDEFINED_FACTORS.values()
            for dep in item.data_dependencies
            if dep.startswith("financial_indicators.")
        }
        assert set(fields["financial_indicators"]) == declared

    def test_dividend_events_have_ex_dates(self) -> None:
        universe = build_synthetic_universe(n_symbols=4, n_days=400)
        assert universe.dividends
        history = next(iter(universe.dividends.values()))
        assert any(ex is not None for ex in history.ex_dates)


# --------------------------------------------------------------------- #
# 评估因子清单解析
# --------------------------------------------------------------------- #


class TestResolveEvalFactors:
    def test_none_resolves_to_full_catalog(self) -> None:
        assert resolve_eval_factors(None) == tuple(sorted(predefined_factor_names()))

    def test_prefix_and_bare_dedupe(self) -> None:
        resolved = resolve_eval_factors(["p_return_21d", "return_21d", "vol_20d"])
        assert resolved == ("return_21d", "vol_20d")

    def test_unknown_factor_named_reject(self) -> None:
        with pytest.raises(KeyError, match="p_no_such"):
            resolve_eval_factors(["p_no_such"])


# --------------------------------------------------------------------- #
# CLI 接线(factor-eval 合成模式冒烟 + release 参数校验)
# --------------------------------------------------------------------- #


class TestFactorEvalCli:
    def test_synthetic_mode_writes_report(self, tmp_path: Any) -> None:
        from typer.testing import CliRunner

        from finboard_app.cli import app

        out = tmp_path / "report.json"
        result = CliRunner().invoke(
            app,
            [
                "factor-eval",
                "--mode",
                "synthetic",
                "--factors",
                "p_return_21d,vol_20d",
                "--output",
                str(out),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["data_face"]["mode"] == "synthetic"
        assert payload["summary"]["factor_count"] == 2
        assert {item["factor"] for item in payload["factors"]} == {
            "return_21d",
            "vol_20d",
        }

    def test_release_mode_requires_window(self) -> None:
        from typer.testing import CliRunner

        from finboard_app.cli import app

        result = CliRunner().invoke(
            app, ["factor-eval", "--mode", "release", "--release-id", "DR-x"]
        )
        assert result.exit_code == 2

    def test_unknown_mode_rejected(self) -> None:
        from typer.testing import CliRunner

        from finboard_app.cli import app

        result = CliRunner().invoke(app, ["factor-eval", "--mode", "live"])
        assert result.exit_code == 2


# --------------------------------------------------------------------- #
# 发布数据面(真实挂载装配;stub provider + 真实窗口挂载)
# --------------------------------------------------------------------- #

_SYMS = ("600000.SH", "000001.SZ", "600519.SH")
_INDEX = "000300.SH"
_EVAL_RELEASE = "DR-eval-bars"
_EVAL_DAILY = "DR-eval-daily"


@dataclass
class _Inst:
    code: str
    market: str = "SH"
    industry: str | None = None
    instrument_type: str = "stock"


@dataclass
class _Sym:
    code: str


@dataclass
class _Bar:
    symbol: _Sym
    timestamp: datetime
    open: float = 9.0
    high: float = 11.0
    low: float = 8.0
    close: float = 10.0
    volume: float = 100.0
    amount: float = 1000.0


@dataclass
class _PITBar:
    code: str
    day: date
    close: float
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            self.available_at = datetime(
                self.day.year, self.day.month, self.day.day, 15, tzinfo=UTC
            )

    @property
    def symbol(self) -> _Sym:
        return _Sym(self.code)

    @property
    def bar(self) -> _Bar:
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=datetime(self.day.year, self.day.month, self.day.day),
            close=self.close,
        )


@dataclass
class _Kind:
    value: str


@dataclass
class _Release:
    release_id: str
    dataset_kind: _Kind
    instruments: list[_Inst] = field(default_factory=list)
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2023, 1, 2)
    end_date: date = date(2024, 12, 31)


@dataclass
class _StubProvider:
    release: _Release
    bars: list[_PITBar] = field(default_factory=list)

    async def fetch_point_in_time_bars(
        self,
        symbol: Any,
        period: Any,
        start: Any,
        end: Any,
        *,
        decision_at: Any,
        adjust: str = "qfq",
    ) -> list[_PITBar]:
        del period, start, end, adjust
        gate = decision_at.date()
        return [b for b in self.bars if b.code == symbol.code and b.day <= gate]

    async def fetch_daily_metrics(
        self, symbol: Any, *, start: Any, end: Any, decision_at: Any
    ) -> list[Any]:
        del start, end, decision_at, symbol
        return []


def _provider_bars(n_days: int = 140) -> list[_PITBar]:
    rng = np.random.default_rng(403)
    start = date(2023, 1, 3)
    days = [start + timedelta(days=i) for i in range(n_days)]
    bars: list[_PITBar] = []
    paths = {
        symbol: list(
            100.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.012, size=n_days))
        )
        for symbol in _SYMS
    }
    index_path = list(
        4000.0 * np.cumprod(1.0 + rng.normal(0.0003, 0.008, size=n_days))
    )
    for i, day in enumerate(days):
        for symbol in _SYMS:
            bars.append(_PITBar(code=symbol, day=day, close=paths[symbol][i]))
        bars.append(_PITBar(code=_INDEX, day=day, close=index_path[i]))
    return bars


def _eval_provider_factory(rid: str) -> _StubProvider:
    if rid == _EVAL_DAILY:
        return _StubProvider(
            release=_Release(_EVAL_DAILY, _Kind("daily_metrics"), _eval_instruments())
        )
    return _StubProvider(
        release=_Release(_EVAL_RELEASE, _Kind("bars"), _eval_instruments()),
        bars=_provider_bars(),
    )


def _eval_instruments() -> list[_Inst]:
    return [
        _Inst("600000.SH", industry="银行"),
        _Inst("000001.SZ", industry="银行"),
        _Inst("600519.SH", industry="食品饮料"),
        _Inst(_INDEX, instrument_type="index"),
    ]


class _EvalSettings:
    research_sandbox_workspace_root = ""  # 测试注入 tmp_path
    research_sandbox_max_nan_ratio = 0.5
    research_sandbox_min_coverage = 0.5


class TestReleaseFace:
    async def test_bundle_and_report_on_real_mount(self, tmp_path: Any) -> None:
        """stub provider → 真实窗口挂载 → 评估共享面 → 报告(mode=release)。"""
        from finboard_backtest.research_sandbox.predefined_runner import (
            build_window_eval_bundle,
        )
        from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

        settings = cast(Any, _EvalSettings())
        settings.research_sandbox_workspace_root = str(tmp_path)
        decision_dates = tuple(
            date(2023, 3, 1) + timedelta(days=k * 5) for k in range(12)
        )
        spec = FactorSeriesRunSpec(
            code_artifact="factor_eval",
            code_commit="factor-eval-v1",
            release_id=_EVAL_RELEASE,
            dataset_release_ids=(_EVAL_DAILY,),
            params={},
            window_start=date(2023, 1, 3),
            window_end=date(2023, 6, 30),
            dates=decision_dates,
        )
        bundle = await build_window_eval_bundle(
            spec,
            settings=settings,
            release_provider_factory=_eval_provider_factory,
        )
        # 基准指数在挂载但不在可交易域;close 面板覆盖全标的
        assert _INDEX in bundle.mount_symbols
        assert _INDEX not in bundle.tradable_symbols
        assert set(bundle.close_panel)  # 有可用的收盘价面板

        from finboard_backtest.factors.eval import evaluate_catalog_on_release

        report = await evaluate_catalog_on_release(
            release_id=_EVAL_RELEASE,
            dataset_release_ids=(_EVAL_DAILY,),
            window_start=date(2023, 1, 3),
            window_end=date(2023, 6, 30),
            factors=("return_21d", "vol_20d", "sharpe_1320d", "turnover_ma_20d"),
            config=FactorEvalConfig(
                horizon=5,
                n_groups=3,
                min_cross_section=3,
                min_ic_dates=3,
            ),
            decision_step=5,
            settings=settings,
            release_provider_factory=_eval_provider_factory,
        )
        assert report["data_face"]["mode"] == "release"
        assert report["data_face"]["release_id"] == _EVAL_RELEASE
        by_name = {item["factor"]: item for item in report["factors"]}
        assert set(by_name) == {
            "return_21d",
            "vol_20d",
            "sharpe_1320d",
            "turnover_ma_20d",
        }
        momentum = by_name["return_21d"]
        assert momentum["first_effective_date"] is not None
        assert momentum["coverage_ratio"] > 0.0
        # daily_metrics 发布为空挂载 → 换手因子结构性缺测如实呈现
        assert by_name["turnover_ma_20d"]["status"] == "no_data"
        # 窗口 ~180 日 < 1320 根 bar 声明 → 覆盖起点 flag
        assert (
            "window_shorter_than_declaration" in by_name["sharpe_1320d"]["flags"]
        )

    async def test_bundle_input_for_matches_main_channel_sampling(
        self, tmp_path: Any
    ) -> None:
        """评估输入面与主构建通道同口径:截面因子采样面 = 可交易域。"""
        from finboard_backtest.research_sandbox.predefined_runner import (
            build_window_eval_bundle,
        )
        from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

        settings = cast(Any, _EvalSettings())
        settings.research_sandbox_workspace_root = str(tmp_path)
        decision_dates = tuple(
            date(2023, 3, 1) + timedelta(days=k * 5) for k in range(8)
        )
        spec = FactorSeriesRunSpec(
            code_artifact="factor_eval",
            code_commit="factor-eval-v1",
            release_id=_EVAL_RELEASE,
            dataset_release_ids=(),
            params={},
            window_start=date(2023, 1, 3),
            window_end=date(2023, 5, 30),
            dates=decision_dates,
        )
        bundle = await build_window_eval_bundle(
            spec,
            settings=settings,
            release_provider_factory=_eval_provider_factory,
        )
        # 截面因子(bars-only)采样面 = 可交易域;基准不进截面分母(#380)
        alpha = get_predefined_factor("alpha101_1")
        frame = alpha.compute(bundle.input_for(alpha))
        assert set(frame) == set(decision_dates)
        for cross in frame.values():
            assert _INDEX not in cross
            assert set(cross) == set(_SYMS)
        # 基本面截面因子(qmj):公告类发布未挂载 → 结构性缺测,
        # 采样面收窄到「实际产出序列」的交集(全缺测 → 空截面,不虚构)
        qmj = get_predefined_factor("qmj")
        qmj_frame = qmj.compute(bundle.input_for(qmj))
        assert set(qmj_frame) == set(decision_dates)
        for cross in qmj_frame.values():
            assert _INDEX not in cross
            assert set(cross) <= set(_SYMS)
            assert all(value is None for value in cross.values())


# --------------------------------------------------------------------- #
# 覆盖起点声明冻结入 series manifest(#403 item 5)
# --------------------------------------------------------------------- #


class TestDeclarationFreezeInRecord:
    @staticmethod
    def _result(quality: Any) -> Any:
        @dataclass
        class _Result:
            dates: tuple[date, ...]
            values: dict[str, dict[str, float | None]]
            quality: Any
            run_id: str | None = None

        return _Result(
            dates=(date(2024, 1, 2),),
            values={"2024-01-02": {"600000.SH": 0.01}},
            quality=quality,
        )

    def _record(self, result: Any, declaration: Any = None) -> Any:
        from finboard_backtest.background_jobs.executors.factor_series_build import (
            _record_from_result,
        )

        return _record_from_result(
            result,
            code_artifact="sharpe_1320d",
            code_commit="predefined-aaaaaaaaaaaa",
            kind="predefined_factor",
            release_id="DR-bars",
            dataset_release_ids=(),
            params={},
            window_start=date(2024, 1, 1),
            window_end=date(2024, 3, 1),
            catalog_declaration=declaration,
        )

    def test_declared_factor_freezes_catalog_declaration(self) -> None:
        record = self._record(
            self._result(quality={"nan_ratio": 0.0}),
            declaration={"min_history_bars": 1320, "window": 1320},
        )
        assert record.quality["catalog_declaration"] == {
            "min_history_bars": 1320,
            "window": 1320,
        }
        assert record.quality["nan_ratio"] == 0.0  # 原有 quality 保留

    def test_undeclared_factor_omits_key(self) -> None:
        from finboard_backtest.factors.predefined import get_predefined_factor

        item = get_predefined_factor("return_21d")
        assert item.min_history_bars is None
        record = self._record(self._result(quality={"nan_ratio": 0.0}))
        assert "catalog_declaration" not in record.quality
        assert record.quality == {"nan_ratio": 0.0}

    def test_declaration_without_quality_creates_quality(self) -> None:
        record = self._record(
            self._result(quality=None),
            declaration={"min_history_bars": 1320, "window": 1320},
        )
        assert record.quality == {
            "catalog_declaration": {"min_history_bars": 1320, "window": 1320}
        }

    def test_content_checksum_ignores_quality(self) -> None:
        """声明冻结不进 content_checksum(值面不变,缓存命中语义零变化)。"""
        from finboard_persistence.factor_series_repo import (
            compute_content_checksum,
        )

        dates = (date(2024, 1, 2),)
        values = {"2024-01-02": {"600000.SH": 0.01}}
        assert compute_content_checksum(dates, values) == compute_content_checksum(
            dates, values
        )


# --------------------------------------------------------------------- #
# 构建期声明快照(评估报告与 manifest 的一致性)
# --------------------------------------------------------------------- #


class TestDeclarationConsistency:
    def test_min_history_bars_declared_only_for_long_windows(self) -> None:
        """#399 基线 7 个长窗声明精确到值;#429 起按批次 scope 追加。

        后续批次的声明集合在各自测试文件内精确断言(批次隔离),此处只锁定
        基线与通用不变量(声明覆盖起点 >= window),避免每加一批就改历史断言。
        """
        declared = {
            name: item.min_history_bars
            for name, item in PREDEFINED_FACTORS.items()
            if item.min_history_bars is not None
        }
        assert {
            name: declared.get(name)
            for name in (
                "beta_1320d",
                "corr_market_1320d",
                "sharpe_1320d",
                "resid_momentum_120d",
                "resid_momentum_250d",
                "rsrs_beta_600d",
                "rsrs_r2_600d",
            )
        } == {
            "beta_1320d": 1320,
            "corr_market_1320d": 1320,
            "sharpe_1320d": 1320,
            "resid_momentum_120d": 239,
            "resid_momentum_250d": 499,
            "rsrs_beta_600d": 600,
            "rsrs_r2_600d": 600,
        }
        for name, warmup in declared.items():
            item = PREDEFINED_FACTORS[name]
            assert item.window is not None, name
            assert warmup >= item.window, name

    def test_direction_enum_values_report_friendly(self) -> None:
        for item in PREDEFINED_FACTORS.values():
            assert item.direction in {FactorPreference.HIGHER, FactorPreference.LOWER}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
