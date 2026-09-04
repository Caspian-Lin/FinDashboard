"""晋级相关性门空 baseline 不再静默 pass(issue #311)。

三态 fail-closed:``correlation`` 键缺失 / 空对象且证据声明过 baseline /
空对象且 ``correlation_baselines=[]`` 各自具名 failure,均不 pass;
screen 逐项检查状态(``screen_checks``,含 not_evaluated)随 evidence 与
promote 响应可见。纯单元测试,不连数据库。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from finboard_backtest.research_code import (
    CHECK_FAIL,
    CHECK_NOT_EVALUATED,
    CHECK_PASS,
    PROMOTION_FAILED,
    evaluate_promotion_gates,
)
from finboard_mcp.tools import research_code
from tests.unit.test_research_code_promotion import (
    _promotion_app,
    _promotion_artifact,
    _promotion_code_run,
    _promotion_validation,
    _PromotionArtifactRepo,
    _PromotionCodeRunRepo,
    _PromotionExperimentRepo,
)

# ---------------------------------------------------------------------------
# 三态:missing key / {} + baselines 非空 / {} + baselines 空
# ---------------------------------------------------------------------------


def _factor_screen(metrics: dict[str, object], baselines: object) -> dict[str, object]:
    """factor_screen 外壳形状(与 run report 一致:baselines 在外壳层)。"""
    return {"factors": {"u_alpha": metrics}, "correlation_baselines": baselines}


def test_missing_correlation_key_is_named_failure() -> None:
    """键缺失(现状已是 failure)补进三态回归,不得 pass。"""
    screen = _factor_screen(
        {"n_periods": 3, "rank_ic": 0.05, "average_turnover": 0.25},
        ["momentum", "pb"],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert not result.screen_passed
    assert "screen.correlation_missing" in result.failures
    assert result.screen_checks["correlation"] == CHECK_FAIL


def test_empty_correlation_mapping_with_baselines_is_missing_not_pass() -> None:
    """空 mapping 零迭代曾静默 pass(#311 根因)—— 修复后与缺失同罪。"""
    screen = _factor_screen(
        {
            "n_periods": 3,
            "rank_ic": 0.05,
            "average_turnover": 0.25,
            "correlation": {},
        },
        ["momentum", "pb"],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert not result.screen_passed
    assert "screen.correlation_missing" in result.failures
    assert result.screen_checks["correlation"] == CHECK_FAIL


def test_empty_correlation_mapping_without_baselines_is_not_evaluated() -> None:
    """横截面确无 baseline 因子可比(correlation_baselines=[])→ not_evaluated。"""
    screen = _factor_screen(
        {
            "n_periods": 3,
            "rank_ic": 0.05,
            "average_turnover": 0.25,
            "correlation": {},
        },
        [],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert not result.screen_passed
    failure = next(item for item in result.failures if item.startswith("screen."))
    assert failure.startswith("screen.correlation_not_evaluated")
    # 修复路径可操作:指出横截面需含非用户因子特征。
    assert "非用户因子特征" in failure
    assert result.screen_checks["correlation"] == CHECK_NOT_EVALUATED


def test_missing_correlation_key_with_empty_baselines_also_not_evaluated() -> None:
    """分流按 correlation_baselines 走:键缺失 + 显式无 baseline 同样 not_evaluated。"""
    screen = _factor_screen(
        {"n_periods": 3, "rank_ic": 0.05, "average_turnover": 0.25},
        [],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert any(
        item.startswith("screen.correlation_not_evaluated") for item in result.failures
    )
    assert result.screen_checks["correlation"] == CHECK_NOT_EVALUATED


def test_malformed_baselines_fall_back_to_missing() -> None:
    """baselines 形状异常(非列表)按证据缺失兜底,不虚构 not_evaluated。"""
    screen = {
        "factors": {
            "u_alpha": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {},
            }
        },
        "correlation_baselines": "momentum",
    }
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert "screen.correlation_missing" in result.failures
    assert result.screen_checks["correlation"] == CHECK_FAIL


# ---------------------------------------------------------------------------
# 正常路径零回归 + 逐项状态可见性
# ---------------------------------------------------------------------------


def test_normal_correlation_path_passes_with_all_checks() -> None:
    """相关性真实可算且未超阈值 → 全部 pass,#311 不影响正常路径。"""
    screen = _factor_screen(
        {
            "n_periods": 3,
            "rank_ic": 0.05,
            "average_turnover": 0.25,
            "correlation": {"momentum": 0.35, "pb": -0.2},
        },
        ["momentum", "pb"],
    )
    result = evaluate_promotion_gates(
        screen=screen,
        validation={"status": "validated_oos", "final_test_unsealed": True},
    )

    assert result.passed
    assert result.failures == ()
    assert result.screen_checks == {
        "n_periods": CHECK_PASS,
        "rank_ic": CHECK_PASS,
        "average_turnover": CHECK_PASS,
        "correlation": CHECK_PASS,
    }
    assert result.as_dict()["screen_checks"] == result.screen_checks


def test_checks_record_threshold_breach_and_baselines_untouched() -> None:
    """相关性超阈值仍按原具名 failure 记 fail,checks 与 failures 一致。"""
    screen = _factor_screen(
        {
            "n_periods": 3,
            "rank_ic": 0.05,
            "average_turnover": 0.25,
            "correlation": {"momentum": 0.95},
        },
        ["momentum"],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert any(
        "screen.correlation_above_maximum(momentum" in item for item in result.failures
    )
    assert result.screen_checks["correlation"] == CHECK_FAIL


def test_all_screen_checks_fail_together() -> None:
    screen = _factor_screen(
        {
            "n_periods": 1,
            "rank_ic": 0.01,
            "average_turnover": 0.9,
            "correlation": {"momentum": 0.95},
        },
        ["momentum"],
    )
    result = evaluate_promotion_gates(screen=screen, validation=None)

    assert not result.passed
    assert result.screen_checks == {
        "n_periods": CHECK_FAIL,
        "rank_ic": CHECK_FAIL,
        "average_turnover": CHECK_FAIL,
        "correlation": CHECK_FAIL,
    }


def test_no_screen_evidence_keeps_checks_empty() -> None:
    """证据整体缺席(screen=None)不产逐项状态,failures 语义不变。"""
    result = evaluate_promotion_gates(screen=None, validation=None)

    assert not result.passed
    assert result.failures == ("screen_missing", "validation_missing")
    assert result.screen_checks == {}


# ---------------------------------------------------------------------------
# promote 响应与持久化 evidence 可见性
# ---------------------------------------------------------------------------


def _patch_promote_repos(monkeypatch: Any, *, artifact_repo: Any, code_run: Any) -> None:
    monkeypatch.setattr(
        research_code, "ResearchCodeArtifactRepository", lambda _session: artifact_repo
    )
    monkeypatch.setattr(
        research_code,
        "ResearchExperimentRepository",
        lambda _session: _PromotionExperimentRepo(_promotion_validation()),
    )
    monkeypatch.setattr(
        research_code, "ResearchCodeRunRepository", lambda _session: _PromotionCodeRunRepo(code_run)
    )


async def test_promote_rejects_empty_correlation_and_records_check(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """{}+baselines 非空 → promote 拒绝,screen_checks.correlation=fail 进 evidence。"""
    app, _session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(
        metrics={
            "screen": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {},
                "correlation_baselines": ["momentum"],
            }
        }
    )
    _patch_promote_repos(monkeypatch, artifact_repo=artifact_repo, code_run=code_run)

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "error"
    assert env.error is not None
    assert "screen.correlation_missing" in env.error.message
    assert artifact.status == "draft"
    assert artifact.promotion_status == PROMOTION_FAILED
    evidence = cast(dict[str, Any], artifact_repo.failed_calls[0]["evidence"])
    gates = cast(dict[str, Any], evidence["gates"])
    assert gates["passed"] is False
    assert gates["screen_checks"]["correlation"] == CHECK_FAIL


async def test_promote_reports_not_evaluated_with_fix_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """{}+baselines 空 → 失败信息附可操作修复路径,check 记 not_evaluated。"""
    app, _session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(
        metrics={
            "screen": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {},
                "correlation_baselines": [],
            }
        }
    )
    _patch_promote_repos(monkeypatch, artifact_repo=artifact_repo, code_run=code_run)

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "error"
    assert env.error is not None
    assert "screen.correlation_not_evaluated" in env.error.message
    assert "非用户因子特征" in env.error.message
    evidence = cast(dict[str, Any], artifact_repo.failed_calls[0]["evidence"])
    gates = cast(dict[str, Any], evidence["gates"])
    assert gates["screen_checks"]["correlation"] == CHECK_NOT_EVALUATED


async def test_promote_success_exposes_screen_checks_in_response(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """正常路径 promote 响应的 promotion_evidence.gates 携带逐项 pass。"""
    app, _session = _promotion_app(tmp_path)
    artifact = _promotion_artifact()
    artifact_repo = _PromotionArtifactRepo(artifact)
    code_run = _promotion_code_run(
        metrics={
            "screen": {
                "n_periods": 3,
                "rank_ic": 0.05,
                "average_turnover": 0.25,
                "correlation": {"momentum": 0.35},
            }
        }
    )
    _patch_promote_repos(monkeypatch, artifact_repo=artifact_repo, code_run=code_run)

    env = await research_code.promote(
        app,
        artifact_id="RC-alpha",
        validation_experiment_id="EXP-alpha",
        screen_run_id="RCR-screen",
    )

    assert env.status == "ok", env.error
    data = cast(dict[str, Any], env.data)
    assert data["status"] == "active"
    evidence = cast(dict[str, Any], data["promotion_evidence"])
    gates = cast(dict[str, Any], evidence["gates"])
    assert gates["passed"] is True
    assert gates["screen_checks"] == {
        "n_periods": CHECK_PASS,
        "rank_ic": CHECK_PASS,
        "average_turnover": CHECK_PASS,
        "correlation": CHECK_PASS,
    }
