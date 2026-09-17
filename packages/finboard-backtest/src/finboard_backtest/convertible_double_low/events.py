"""可转债事件风险过滤(issue #63)。

红线:**事件按公告 ``available_at`` 生效,禁止用未来公告回填历史决策**。
``filter_event_risk`` 在 ``as_of`` 当日排除有活跃事件风险的转债:
* 强赎公告后 → 禁止新开仓(已有持仓在赎回期卖出)
* 回售期 → 禁止新开仓
* 下修公告 → 标记但不直接禁止(下修通常是利好)
* 到期前 ``min_days_to_maturity`` 天 → 禁止新开仓
* 退市公告 → 禁止开仓
* 停牌 → 禁止开仓
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime

from finboard_backtest.convertible_double_low.universe import ConvertibleSnapshot
from finboard_shared.instruments import LifecycleEvent
from finboard_shared.types import LifecycleEventType


@dataclass(frozen=True, slots=True)
class EventRiskResult:
    """单只转债的事件风险检查结果。"""

    code: str
    is_blocked: bool
    block_reason: str
    active_events: tuple[LifecycleEvent, ...]


def _is_event_active(
    event: LifecycleEvent,
    *,
    as_of_dt: datetime,
    lookforward_days: int,
) -> bool:
    """判断事件在 ``as_of_dt`` 当日是否处于活跃窗口。

    活跃 = ``available_at`` <= ``as_of_dt``(公告已可知)且
    生效日期在 ``as_of_dt`` 前 ``lookforward_days`` 天之后。
    """
    if event.available_at > as_of_dt:
        return False
    as_of_date = as_of_dt.date()
    window_end = event.effective_date
    from datetime import timedelta

    window_start = event.effective_date - timedelta(days=lookforward_days)
    return window_start <= as_of_date <= window_end


def check_event_risk(
    snapshot: ConvertibleSnapshot,
    events: list[LifecycleEvent],
    *,
    as_of: date,
    min_days_to_maturity: int = 30,
) -> EventRiskResult:
    """检查单只转债的事件风险。

    参数:
        snapshot: 转债快照
        events: 该转债的全部历史事件(含未来,但仅 PIT 可知的生效)
        as_of: 判断日期
        min_days_to_maturity: 到期前多少天禁止开仓

    返回:
        EventRiskResult,``is_blocked=True`` 时禁止新开仓。
    """
    as_of_dt = datetime.combine(as_of, datetime.min.time(), tzinfo=UTC)
    code = snapshot.code

    active: list[LifecycleEvent] = []
    for ev in events:
        if ev.symbol != code:
            continue
        if ev.available_at > as_of_dt:
            continue

        if ev.event_type in (
            LifecycleEventType.FORCED_REDEMPTION,
            LifecycleEventType.DELISTING,
            LifecycleEventType.SUSPENSION,
        ) or (ev.event_type in (
            LifecycleEventType.SELL_BACK,
            LifecycleEventType.CONVERSION_PRICE_ADJUST,
            LifecycleEventType.DOWNWARD_REVISION,
        ) and _is_event_active(ev, as_of_dt=as_of_dt, lookforward_days=30)):
            active.append(ev)

    block_reasons: list[str] = []
    for ev in active:
        if ev.event_type is LifecycleEventType.FORCED_REDEMPTION:
            block_reasons.append("forced_redemption")
        elif ev.event_type is LifecycleEventType.DELISTING:
            block_reasons.append("delisting")
        elif ev.event_type is LifecycleEventType.SUSPENSION:
            block_reasons.append("suspended")
        elif ev.event_type is LifecycleEventType.SELL_BACK:
            block_reasons.append("sell_back_period")

    if (
        snapshot.days_to_maturity is not None
        and snapshot.days_to_maturity < min_days_to_maturity
    ):
        block_reasons.append("near_maturity")

    is_blocked = len(block_reasons) > 0
    return EventRiskResult(
        code=code,
        is_blocked=is_blocked,
        block_reason=";".join(block_reasons) if block_reasons else "ok",
        active_events=tuple(active),
    )


def filter_event_risk(
    snapshots: list[ConvertibleSnapshot],
    all_events: dict[str, list[LifecycleEvent]],
    *,
    as_of: date,
    min_days_to_maturity: int = 30,
) -> tuple[list[ConvertibleSnapshot], list[EventRiskResult]]:
    """批量过滤事件风险,返回 (安全列表, 全部检查结果)。"""
    safe: list[ConvertibleSnapshot] = []
    results: list[EventRiskResult] = []
    for snap in snapshots:
        events = all_events.get(snap.code, [])
        result = check_event_risk(
            snap, events, as_of=as_of, min_days_to_maturity=min_days_to_maturity
        )
        results.append(result)
        if not result.is_blocked:
            safe.append(snap)
    return safe, results


__all__ = [
    "EventRiskResult",
    "check_event_risk",
    "filter_event_risk",
]
