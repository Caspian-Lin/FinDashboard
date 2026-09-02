"""failure_context / truncate_summary 单元测试(issue #263)。"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finboard_backtest.background_jobs.contracts import truncate_summary
from finboard_backtest.research_run.failure_context import (
    FailureContext,
    attach_decision_load_context,
    build_failure_summary,
    read_decision_load_context,
)


def test_attach_read_roundtrip_preserves_exception() -> None:
    failure = RuntimeError("发布清单不存在: /data/REL-x")
    decision_at = datetime(2024, 3, 29, 15, tzinfo=UTC)

    attach_decision_load_context(failure, decision_at=decision_at, release_id="REL-x")

    marker = read_decision_load_context(failure)
    assert marker is not None
    assert marker.decision_at == decision_at
    assert marker.release_id == "REL-x"
    # bare raise 语义:标记不改变类型与消息。
    assert type(failure) is RuntimeError
    assert str(failure) == "发布清单不存在: /data/REL-x"


def test_read_without_marker_returns_none() -> None:
    assert read_decision_load_context(ValueError("普通异常")) is None
    assert read_decision_load_context(RuntimeError("被 attach 过的非标记属性")) is None


def test_build_summary_without_any_stage_or_decision(manifest_factory) -> None:
    """循环前失败(validate_manifest 等):头部只有冻结发布绑定。"""
    exc = ValueError("strategy_spec_checksum 与策略规格内容不一致")
    summary = build_failure_summary(exc, FailureContext(), manifest_factory())

    assert summary.startswith(
        "[dataset_releases=release-v1; factor_snapshots=factor-v1] "
    )
    assert summary.endswith("strategy_spec_checksum 与策略规格内容不一致")


def test_build_summary_load_marker_precedes_runner_context(manifest_factory) -> None:
    """加载期标记的精确决策日优先于 runner 上下文。"""
    exc = RuntimeError("bars 文件校验和不一致")
    attach_decision_load_context(
        exc,
        decision_at=datetime(2024, 6, 28, 0, 0, tzinfo=UTC),
        release_id="release-v1",
    )
    ctx = FailureContext(stage="decision_load")

    summary = build_failure_summary(exc, ctx, manifest_factory())

    assert summary.startswith(
        "[stage=decision_load; decision=2024-06-28; release=release-v1; "
        "dataset_releases=release-v1; factor_snapshots=factor-v1] "
    )
    assert summary.endswith("bars 文件校验和不一致")


def test_build_summary_execution_context_carries_index(manifest_factory) -> None:
    """执行期失败:stage / 决策日期 / 1-based 期次与冻结清单齐备。"""
    exc = ValueError("组合流水线产物校验和不一致")
    ctx = FailureContext(
        stage="signals",
        decision_date=date(2024, 1, 3),
        decision_index=2,
    )

    summary = build_failure_summary(exc, ctx, manifest_factory())

    assert summary == (
        "[stage=signals; decision=2024-01-03; decision_index=2; "
        "dataset_releases=release-v1; factor_snapshots=factor-v1] "
        "组合流水线产物校验和不一致"
    )


def test_build_summary_marks_load_stage_when_context_missing(manifest_factory) -> None:
    """runner 忘记推进 stage 时,标记仍能补全 decision_load 归属。"""
    exc = ValueError("决策期加载失败")
    attach_decision_load_context(
        exc,
        decision_at=datetime(2024, 4, 30, 0, 0, tzinfo=UTC),
        release_id="release-v1",
    )

    summary = build_failure_summary(exc, FailureContext(), manifest_factory())

    assert summary.startswith("[stage=decision_load; decision=2024-04-30; ")


def test_truncate_summary_short_text_untouched() -> None:
    text = "[stage=decision_load] 短错误"
    assert truncate_summary(text) == text
    assert truncate_summary("x" * 1000) == "x" * 1000


def test_truncate_summary_keeps_head_and_tail() -> None:
    head = "[stage=decision_load; decision=2024-06-28; release=REL-1] 开头根因描述"
    tail = "actual=ffffffffChecksum 校验失败,请重新发布"
    middle = "x" * 3000
    text = head + middle + tail

    truncated = truncate_summary(text)

    assert len(truncated) <= 1005  # 省略标记字符数波动的软上限
    assert truncated.startswith(head)
    assert truncated.endswith(tail)
    assert "中间省略" in truncated
    # 完整中段被省略,不在结果中(头尾各自保留了一小段 x 连缀属预期)。
    assert middle not in truncated


@pytest.mark.parametrize("limit", [50, 200])
def test_truncate_summary_small_limit_still_keeps_both_ends(limit: int) -> None:
    text = "HEAD-" + "y" * 1000 + "-TAIL"

    truncated = truncate_summary(text, limit=limit, tail_chars=10)

    assert truncated.startswith("HEAD-")
    assert truncated.endswith("-TAIL")
    assert "中间省略" in truncated
