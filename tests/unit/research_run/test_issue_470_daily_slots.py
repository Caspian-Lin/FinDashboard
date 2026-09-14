"""daily 预计算逐期槽位存储与释放(issue #470 后半场,#438 存储形态 v2)。

2026-09-14 内存归因:per-symbol (n_periods, n_cols) 矩阵让 daily 预计算
常驻整条 run(全市场 5215 x 556 期 ≈ 0.45GB/发布,驻留是 run 级非期数级)。
存储改为逐期槽位(期 → 行=标的)后,单期槽位是可独立释放的 numpy 大数组,
分块消费完成后逐期真释放;被释放期复读回落逐期读取路径,值语义不变。

本文件锁定:

* 槽位构建散转正确:预建后逐期特征与未预建 loader(逐期路径)逐值等值
  (#438 既有测试继续覆盖,此处锁释放后回落同值);
* ``release_period`` / ``release_daily_precompute_periods`` 真释放:槽位置
  ``None``,重复 / 未知决策期为 no-op;
* 被释放期复读:消费口回落逐期路径,特征与释放前逐值等值(多付 IO 不改值);
* 未释放期消费不受影响。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any

import pytest

from finboard_backtest.research_run.contracts import ResearchRunManifest
from finboard_backtest.research_run.frozen_loader import (
    DailyMetricsPrecompute,
    FrozenInputLoader,
)

from .test_issue_438_daily_precompute import (
    _CODES,
    _RELEASE_ID,
    _SESSIONS,
    _daily,
    _precompute_manifest,
    _publish_bars_and_daily,
)

pytestmark = pytest.mark.asyncio

_DECISIONS = (
    datetime(2024, 1, 3, 16, 0, tzinfo=UTC),
    datetime(2024, 1, 4, 16, 0, tzinfo=UTC),
)


def _daily_records() -> dict[str, list[Any]]:
    return {
        code: [
            _daily(
                code,
                day,
                available_at=datetime.combine(day, time(15, 30), tzinfo=UTC),
                pb=f"{10 + index}.{index}",
            )
            for index, day in enumerate(_SESSIONS)
        ]
        for code in _CODES
    }


async def _prepared_loader(
    tmp_path: Path,
) -> tuple[FrozenInputLoader, DailyMetricsPrecompute]:
    providers = await _publish_bars_and_daily(tmp_path, _daily_records())

    def factory(release_id: str):
        return providers[release_id]

    async def snapshot_provider(snapshot_id: str):
        del snapshot_id
        return None

    loader = FrozenInputLoader(
        release_provider_factory=factory,
        snapshot_provider=snapshot_provider,
    )
    await loader.ensure_daily_metrics_histories(_precompute_manifest(), _DECISIONS)
    return loader, loader._daily_precompute[_RELEASE_ID]


async def test_release_period_frees_slot_and_refetch_falls_back(
    tmp_path: Path,
) -> None:
    """释放:槽位置 None;被释放期复读回落逐期路径,特征逐值不变。"""
    from finboard_backtest.research_run.frozen_loader import (
        _build_candidates_and_lots,
    )

    loader, precompute = await _prepared_loader(tmp_path)
    manifest: ResearchRunManifest = _precompute_manifest()
    candidates, _ = _build_candidates_and_lots(
        list(loader.release_provider_factory(_RELEASE_ID).release.instruments)
    )

    before, missing_before = await loader._load_research_features(
        manifest, candidates, _DECISIONS[0]
    )
    assert before
    assert missing_before == {}
    # 预建态:两期槽位均在。
    assert precompute.slots[0] is not None
    assert precompute.slots[1] is not None

    released = loader.release_daily_precompute_periods([_DECISIONS[0]])
    assert released == 1
    assert precompute.slots[0] is None  # 真释放(非仅标记)
    assert precompute.slots[1] is not None  # 未消费期不受影响

    # 被释放期复读:槽位缺失回落逐期读取路径,值语义不变。
    after, missing_after = await loader._load_research_features(
        manifest, candidates, _DECISIONS[0]
    )
    assert after == before
    assert missing_after == missing_before


async def test_release_period_noop_for_repeat_and_unknown(tmp_path: Path) -> None:
    """重复释放 / 未知决策期:no-op(契约与价格特征预计算一致)。"""
    loader, precompute = await _prepared_loader(tmp_path)
    unknown = datetime(2030, 1, 1, 16, 0, tzinfo=UTC)
    assert precompute.release_period(unknown) == 0
    assert loader.release_daily_precompute_periods([unknown]) == 0

    assert precompute.release_period(_DECISIONS[1]) > 0
    assert precompute.slots[1] is None
    # 重复释放:槽位已是 None,no-op。
    assert precompute.release_period(_DECISIONS[1]) == 0
    assert loader.release_daily_precompute_periods([_DECISIONS[1]]) == 0


async def test_unreleased_period_consumption_unaffected(tmp_path: Path) -> None:
    """未释放期照常走槽位:释放前期与释放后其它期特征逐值等值(逐期参考)。"""
    from finboard_backtest.research_run.frozen_loader import (
        _build_candidates_and_lots,
    )

    loader, precompute = await _prepared_loader(tmp_path)
    manifest: ResearchRunManifest = _precompute_manifest()
    candidates, _ = _build_candidates_and_lots(
        list(loader.release_provider_factory(_RELEASE_ID).release.instruments)
    )

    first, _ = await loader._load_research_features(
        manifest, candidates, _DECISIONS[0]
    )
    second, _ = await loader._load_research_features(
        manifest, candidates, _DECISIONS[1]
    )
    assert first
    assert second
    loader.release_daily_precompute_periods([_DECISIONS[0]])
    # 第二期槽位仍在:再次消费走槽位路径,值不变。
    second_again, _ = await loader._load_research_features(
        manifest, candidates, _DECISIONS[1]
    )
    assert second_again == second
    assert precompute.slots[1] is not None
