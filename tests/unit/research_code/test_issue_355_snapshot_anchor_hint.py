"""快照锚定失配的前置可见化单测(issue #355)。

覆盖验收三态与文案断言:

* 全匹配 → 收集器 / validate 提示均为空(零噪音);
* 部分失配 → 逐快照 / 逐因子全量列出(名称 / 锚定发布 / 失配方向),
  不再像旧门控一次只暴露第一个失配;
* 全失配 → 文案附两条修复路径(加入 dataset_release_ids / 对新发布
  重算沙箱因子 RCR → 质量门 → 新快照);
* 入队拒绝文案具名 ``snapshot_anchor_mismatch``(REST 422 与 MCP
  invalid_argument 共用同一渲染函数,文案断言在本层完成);
* validate 通道 warning 具名 ``user_factor_anchor_mismatch``。

收集器与 warning 查询经 monkeypatch 替换 ``ResearchCodeRunRepository``,
不依赖 PostgreSQL;真实入队 / validate 端到端见
``tests/integration/test_issue_355_snapshot_anchor_hint.py``。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from finboard_backtest.research_code import (
    SNAPSHOT_ANCHOR_MISMATCH_CODE,
    USER_FACTOR_ANCHOR_WARNING_CODE,
    SnapshotAnchorMismatch,
    snapshot_anchor_mismatch_error,
    snapshot_anchor_mismatches,
    user_factor_anchor_warnings,
)
from finboard_data.factor_lab import (
    FeatureObservation,
    build_feature_snapshot,
)

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #
def _observation(feature_name: str, symbol: str = "A.SH") -> FeatureObservation:
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=1.0,
        observed_at=_TS,
        available_at=_TS,
        source="research_code_run",
        source_version="a" * 40,
    )


def _sandbox_snapshot(
    snapshot_id: str,
    *,
    run_id: str,
    factor_name: str = "u_mom20",
    release_id: str | None = None,
) -> Any:
    """run 锚定快照(#217 形状);``release_id`` 非 None 时给发布锚点。"""
    snapshot = build_feature_snapshot(
        dataset_release_id=release_id,
        dataset_release_checksum="m" * 64,
        decision_at=_TS,
        code_version="a" * 40,
        observations=(_observation(factor_name),),
        source_run_id=run_id,
    )
    return replace(snapshot, snapshot_id=snapshot_id)


def _published_snapshot(snapshot_id: str, release_id: str) -> Any:
    snapshot = build_feature_snapshot(
        dataset_release_id=release_id,
        dataset_release_checksum="c" * 64,
        decision_at=_TS,
        code_version="v1",
        observations=(_observation("momentum"),),
    )
    return replace(snapshot, snapshot_id=snapshot_id)


@dataclass
class _FakeRun:
    run_id: str
    dataset_release_ids: list[str]
    output_snapshot_id: str | None
    status: str = "succeeded"
    kind: str = "factor"
    name: str = "mom20"


@dataclass
class _FakeRunRepo:
    """按 (kind, name, status) 过滤的假 RCR 仓储。"""

    runs: list[_FakeRun]
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def get(self, run_id: str) -> _FakeRun | None:
        for run in self.runs:
            if run.run_id == run_id:
                return run
        return None

    async def list_runs(self, **kwargs: Any) -> list[_FakeRun]:
        self.calls.append(kwargs)
        matched = [
            run
            for run in self.runs
            if run.kind == kwargs.get("kind")
            and run.name == kwargs.get("name")
            and run.status == kwargs.get("status")
        ]
        # 真实仓储按 created_at 倒序;夹具按插入序倒排模拟(越后越新)。
        return list(reversed(matched))[: kwargs.get("limit", 100)]


@pytest.fixture
def patch_run_repo(monkeypatch: pytest.MonkeyPatch):
    """替换 ``finboard_persistence.ResearchCodeRunRepository``(函数内导入)。"""

    def _install(repo: _FakeRunRepo) -> _FakeRunRepo:
        monkeypatch.setattr(
            "finboard_persistence.ResearchCodeRunRepository",
            lambda _session: repo,
        )
        return repo

    return _install


# --------------------------------------------------------------------------- #
# 入队拒绝文案(REST 422 / MCP invalid_argument 共用渲染)
# --------------------------------------------------------------------------- #
class TestSnapshotAnchorMismatchError:
    def test_message_names_code_factor_and_both_remediations(self) -> None:
        mismatch = SnapshotAnchorMismatch(
            snapshot_id="FS-1",
            factor_names=("u_mom20",),
            anchored_release_ids=("rel-a", "rel-b"),
            missing_release_ids=("rel-b",),
            anchor_kind="sandbox_run",
            run_id="RCR-1",
        )
        message = snapshot_anchor_mismatch_error([mismatch])
        # 具名标记 + 逐快照三要素
        assert SNAPSHOT_ANCHOR_MISMATCH_CODE in message
        assert "FS-1" in message
        assert "u_mom20" in message
        assert "['rel-a', 'rel-b']" in message
        assert "['rel-b']" in message
        assert "锚定 ⊄ 本次冻结清单" in message
        # 两条修复路径:加入 dataset_release_ids / 重算 RCR(→ 质量门 → 新快照)
        assert "dataset_release_ids" in message
        assert "finboard_research_code_run" in message
        assert "质量门" in message

    def test_message_covers_published_and_sandbox_branches(self) -> None:
        mismatches = [
            SnapshotAnchorMismatch(
                snapshot_id="FS-pub",
                factor_names=("momentum",),
                anchored_release_ids=("rel-old",),
                missing_release_ids=("rel-old",),
                anchor_kind="published_release",
            ),
            SnapshotAnchorMismatch(
                snapshot_id="FS-sbx",
                factor_names=("u_rev30",),
                anchored_release_ids=("rel-old",),
                missing_release_ids=("rel-old",),
                anchor_kind="sandbox_run",
                run_id="RCR-9",
            ),
        ]
        message = snapshot_anchor_mismatch_error(mismatches)
        assert "2 个引用快照" in message
        assert "FS-pub" in message
        assert "发布快照绑定" in message
        assert "FS-sbx" in message
        assert "RCR-9" in message


# --------------------------------------------------------------------------- #
# 收集器:逐快照全量收集(#217 同判定口径,#355 改全量列出)
# --------------------------------------------------------------------------- #
class TestSnapshotAnchorMismatches:
    async def test_all_match_returns_empty(self, patch_run_repo) -> None:
        patch_run_repo(
            _FakeRunRepo(
                runs=[_FakeRun("RCR-1", ["rel-a"], output_snapshot_id="FS-1")]
            )
        )
        snapshots = [
            _sandbox_snapshot("FS-1", run_id="RCR-1"),
            _published_snapshot("FS-2", "rel-a"),
        ]
        assert (
            await snapshot_anchor_mismatches(
                object(), snapshots=snapshots, requested_release_ids={"rel-a"}
            )
            == []
        )

    async def test_partial_mismatch_collects_all_offenders(self, patch_run_repo) -> None:
        patch_run_repo(
            _FakeRunRepo(
                runs=[
                    # 全匹配
                    _FakeRun("RCR-ok", ["rel-a"], output_snapshot_id="FS-ok"),
                    # 部分失配:锚定 [a, b] ⊄ 请求 [a]
                    _FakeRun("RCR-2", ["rel-a", "rel-b"], output_snapshot_id="FS-2"),
                ]
            )
        )
        snapshots = [
            _sandbox_snapshot("FS-ok", run_id="RCR-ok", factor_name="u_good"),
            _sandbox_snapshot("FS-2", run_id="RCR-2", factor_name="u_mom20"),
            _published_snapshot("FS-3", "rel-c"),
        ]
        mismatches = await snapshot_anchor_mismatches(
            object(), snapshots=snapshots, requested_release_ids={"rel-a"}
        )
        # 旧门控一次只抛第一个失配;现在两个失配快照一次性列出。
        assert [item.snapshot_id for item in mismatches] == ["FS-2", "FS-3"]
        first = mismatches[0]
        assert first.anchor_kind == "sandbox_run"
        assert first.run_id == "RCR-2"
        assert first.factor_names == ("u_mom20",)
        assert first.anchored_release_ids == ("rel-a", "rel-b")
        assert first.missing_release_ids == ("rel-b",)
        second = mismatches[1]
        assert second.anchor_kind == "published_release"
        assert second.missing_release_ids == ("rel-c",)
        # 失配文案逐快照渲染
        message = snapshot_anchor_mismatch_error(mismatches)
        assert "FS-2" in message
        assert "FS-3" in message


# --------------------------------------------------------------------------- #
# validate 通道 warning:逐因子 / 逐锚定集合,零噪音
# --------------------------------------------------------------------------- #
class TestUserFactorAnchorWarnings:
    async def test_zero_noise_when_all_anchors_covered(self, patch_run_repo) -> None:
        repo = patch_run_repo(
            _FakeRunRepo(
                runs=[
                    _FakeRun("RCR-1", ["rel-a"], output_snapshot_id="FS-1"),
                    _FakeRun("RCR-2", ["rel-a", "rel-b"], output_snapshot_id="FS-2"),
                ]
            )
        )
        warnings = await user_factor_anchor_warnings(
            object(),
            required_factor_sources={"u_mom20", "momentum"},
            dataset_release_ids={"rel-a", "rel-b"},
        )
        assert warnings == ()
        # 只查询 u_ 前缀因子(builtin 因子不查 run)
        assert repo.calls == [
            {"kind": "factor", "name": "mom20", "status": "succeeded", "limit": 500}
        ]

    async def test_partial_mismatch_warns_only_offending_factor(
        self, patch_run_repo
    ) -> None:
        patch_run_repo(
            _FakeRunRepo(
                runs=[
                    _FakeRun(
                        "RCR-ok",
                        ["rel-new"],
                        output_snapshot_id="FS-ok",
                        name="fresh",
                    ),
                    _FakeRun(
                        "RCR-old",
                        ["rel-old"],
                        output_snapshot_id="FS-old",
                        name="stale",
                    ),
                ]
            )
        )
        warnings = await user_factor_anchor_warnings(
            object(),
            required_factor_sources={"u_fresh", "u_stale"},
            dataset_release_ids={"rel-new"},
        )
        assert len(warnings) == 1
        warning = warnings[0]
        assert warning.code == USER_FACTOR_ANCHOR_WARNING_CODE
        assert warning.factor_name == "u_stale"
        assert warning.run_id == "RCR-old"
        assert warning.snapshot_id == "FS-old"
        assert warning.missing_release_ids == ("rel-old",)
        # message 附两条修复路径
        assert "dataset_release_ids" in warning.message
        assert "finboard_research_code_run" in warning.message
        assert warning.as_dict()["code"] == USER_FACTOR_ANCHOR_WARNING_CODE

    async def test_full_mismatch_dedupes_by_anchor_set(self, patch_run_repo) -> None:
        patch_run_repo(
            _FakeRunRepo(
                runs=[
                    # 同一锚定集合两次重算(不同 decision_at)→ 只提示一次
                    _FakeRun("RCR-old1", ["rel-old"], output_snapshot_id="FS-old1"),
                    _FakeRun("RCR-old2", ["rel-old"], output_snapshot_id="FS-old2"),
                    # 另一锚定集合
                    _FakeRun("RCR-x", ["rel-x"], output_snapshot_id="FS-x"),
                    # 无快照产出的失败重算不提示
                    _FakeRun("RCR-dead", ["rel-y"], output_snapshot_id=None),
                ]
            )
        )
        warnings = await user_factor_anchor_warnings(
            object(),
            required_factor_sources={"u_mom20"},
            dataset_release_ids={"rel-new"},
        )
        # 逐锚定集合一条;倒序遍历下每个集合取最新成功 run 作证据
        assert sorted(warning.run_id for warning in warnings) == ["RCR-old2", "RCR-x"]
        assert all(
            "snapshot_anchor_mismatch" in warning.message for warning in warnings
        )

    async def test_no_user_factors_or_no_releases_is_silent(
        self, patch_run_repo
    ) -> None:
        repo = patch_run_repo(_FakeRunRepo(runs=[]))
        assert (
            await user_factor_anchor_warnings(
                object(),
                required_factor_sources={"momentum", "close"},
                dataset_release_ids={"rel-a"},
            )
            == ()
        )
        assert (
            await user_factor_anchor_warnings(
                object(),
                required_factor_sources={"u_mom20"},
                dataset_release_ids=set(),
            )
            == ()
        )
        assert repo.calls == []
