"""标的生命周期检测的纯逻辑组件(issue #35)。

退市检测的反向 diff 在 :meth:`InstrumentRepository.sync_with_diff` 中实现
(需要 DB session);本模块只提供**不依赖 DB 的可测试逻辑**:

* :class:`SuspendDetector` —— 停牌检测:基于行情增量拉取的"连续无新数据"
  计数,输出应 suspend / resume 的标的。纯内存状态,可导出 / 恢复,便于
  在定时任务中跨进程重启持久化。
* :func:`apply_suspend_decision` —— 把检测决策写回 ``instruments.status``,
  调用方负责提供 session(延迟 import,避免 finboard-data 硬依赖 persistence)。

设计要点(对照 issue 验收标准):
* 停牌判定必须连续 N 个交易日无数据,默认 5;单次抖动不触发。
* 恢复交易(重新有数据)→ 回退 active。
* 只有 active 标的的"无数据"才累计计数;suspended 标的继续无数据属正常,
  不再重复计数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from datetime import date

    from finboard_persistence import InstrumentRepository

logger = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SuspendDecision:
    """``SuspendDetector.process`` 的输出决策。"""

    to_suspend: list[str]
    to_resume: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.to_suspend and not self.to_resume


class SuspendDetector:
    """停牌检测器(纯逻辑)。

    每次 batch 拉取行情后调用 :meth:`process`,传入:
    * ``fetch_results`` —— ``{symbol_code: has_new_data}``;``has_new_data`` 为
      ``True`` 表示本次拉取到了新 bar(缓存 ``last_date`` 前进),``False`` 表示无新数据。
    * ``active_codes`` —— 当前 ``status=active`` 的标的集合(只有它们会累计计数)。
    * ``suspended_codes`` —— 当前 ``status=suspended`` 的标的集合(有新数据时回退)。

    阈值 ``threshold`` 表示连续多少个交易日无数据才判定停牌(默认 5)。
    """

    def __init__(self, threshold: int = 5) -> None:
        if threshold < 1:
            raise ValueError("threshold 必须 >= 1")
        self._threshold = threshold
        self._streak: dict[str, int] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    def streak(self, code: str) -> int:
        """返回某标的当前的连续无数据天数(0 表示有数据或未观测)。"""
        return self._streak.get(code, 0)

    def process(
        self,
        *,
        fetch_results: dict[str, bool],
        active_codes: set[str] | frozenset[str],
        suspended_codes: set[str] | frozenset[str] | None = None,
    ) -> SuspendDecision:
        """根据本次拉取结果更新计数并输出 suspend / resume 决策。

        :returns: :class:`SuspendDecision`(code 列表已排序,便于断言)
        """
        suspended_codes = suspended_codes or set()
        to_suspend: list[str] = []
        to_resume: list[str] = []

        for code, has_new in fetch_results.items():
            if has_new:
                self._streak[code] = 0
                if code in suspended_codes:
                    to_resume.append(code)
            else:
                # 只有 active 标的的无数据才累计(已 suspended 的继续停牌不重复计)
                if code in active_codes:
                    self._streak[code] = self._streak.get(code, 0) + 1
                    if self._streak[code] >= self._threshold:
                        to_suspend.append(code)

        decision = SuspendDecision(
            to_suspend=sorted(to_suspend),
            to_resume=sorted(to_resume),
        )
        logger.info(
            "suspend_detector.decision",
            threshold=self._threshold,
            suspend=len(decision.to_suspend),
            resume=len(decision.to_resume),
        )
        return decision

    def state(self) -> dict[str, int]:
        """导出全部连续无数据计数,用于持久化 / 跨进程恢复。"""
        return dict(self._streak)

    def restore(self, state: dict[str, int]) -> None:
        """从导出的 state 恢复计数。"""
        self._streak = {k: int(v) for k, v in state.items()}


async def apply_suspend_decision(
    repo: InstrumentRepository,
    decision: SuspendDecision,
) -> None:
    """把 :class:`SuspendDecision` 写回 ``instruments.status``。

    * ``to_suspend`` → ``status=suspended``;
    * ``to_resume``  → ``status=active``(并归零 missing_runs)。

    调用方负责 ``commit``。延迟 import 枚举,避免循环依赖。
    """
    from finboard_shared.types import ListingStatus

    for code in decision.to_suspend:
        await repo.update_listing_status(code, ListingStatus.SUSPENDED)
    for code in decision.to_resume:
        await repo.update_listing_status(
            code, ListingStatus.ACTIVE, reset_missing_runs=True
        )


@dataclass
class LifecycleSyncReport:
    """sync 后供 CLI / API 展示的汇总(与 :class:`InstrumentSyncResult` 对齐)。"""

    new: int = 0
    updated: int = 0
    renamed: list[tuple[str, str, str]] = field(default_factory=list)
    pending_delist: list[str] = field(default_factory=list)
    delisted: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)

    @classmethod
    def from_result(cls, res: object) -> LifecycleSyncReport:
        return cls(
            new=getattr(res, "new", 0),
            updated=getattr(res, "updated", 0),
            renamed=list(getattr(res, "renamed", [])),
            pending_delist=list(getattr(res, "pending_delist", [])),
            delisted=list(getattr(res, "delisted", [])),
            reactivated=list(getattr(res, "reactivated", [])),
        )


def compute_has_new(
    before_last: date | None,
    after_last: date | None,
) -> bool:
    """比较拉取前后的缓存 ``last_date``,判断本次是否取得新 bar。

    * ``after_last`` 严格晚于 ``before_last`` → 有新数据;
    * 二者均为 ``None``(从无缓存且本次也没拉到)→ 无新数据;
    * ``before_last`` 为 ``None`` 但 ``after_last`` 有值 → 有新数据。
    """
    if after_last is None:
        return False
    if before_last is None:
        return True
    return after_last > before_last
