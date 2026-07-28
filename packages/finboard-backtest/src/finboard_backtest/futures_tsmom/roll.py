"""期货换月 / 展期 / 连续序列构造。

issue #64 的核心要求:
1. **未拼接原合约**:信号和成交使用原始合约,换月处产生跳空(正确行为)。
2. **展期收益可审计**:记录每次换月的展期价差(roll yield)。
3. **连续序列仅供展示**:通过比例 / 差分调整消除换月跳空,但调整收益
   **不能**被当成可交易利润。

换月判定方法(VOLUME):
- 在 ``roll_lookback`` 窗口内,当下月合约成交量超过当月时触发换月。
- 或在最后交易日前 ``roll_days_before_expiry`` 个交易日强制换月。

展期价差(roll yield):
    roll_yield = (front_price - next_price) / next_price

正展期收益(backwardation)通常意味着多头展期增益。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from finboard_shared.types import AdjustmentMethod, RollMethod


@dataclass(frozen=True, slots=True)
class RollEvent:
    """单次换月事件。"""

    symbol: str
    """品种代码,如 ``"IF"``。"""
    bar_index: int
    """换月发生的 Bar 索引。"""
    from_contract: str
    """换出合约 ID。"""
    to_contract: str
    """换入合约 ID。"""
    front_price: float
    """换出合约换月时价格。"""
    next_price: float
    """换入合约换月时价格。"""
    roll_yield: float
    """展期收益率 = (front - next) / next。"""
    reason: str
    """换月原因(volume_crossover / scheduled / forced)。"""

    @property
    def is_contango(self) -> bool:
        """升水结构(front < next → roll_yield < 0)。"""
        return self.roll_yield < 0

    @property
    def is_backwardation(self) -> bool:
        """贴水结构(front > next → roll_yield > 0)。"""
        return self.roll_yield > 0


@dataclass(frozen=True, slots=True)
class ActiveContractSeries:
    """主力合约序列 —— 每个时间点对应的活跃合约。"""

    symbol: str
    """品种代码。"""
    dates: tuple[str, ...]
    """日期序列(ISO 格式)。"""
    closes: tuple[float, ...]
    """主力合约收盘价序列(未调整)。"""
    opens: tuple[float, ...]
    """主力合约开盘价序列。"""
    volumes: tuple[float, ...]
    """主力合约成交量序列。"""
    contract_ids: tuple[str, ...]
    """每个时间点的合约 ID。"""
    roll_events: tuple[RollEvent, ...]
    """换月事件。"""

    @property
    def roll_yield_total(self) -> float:
        """累计展期收益(每次换月的 roll_yield 之和)。"""
        return sum(ev.roll_yield for ev in self.roll_events)

    @property
    def roll_count(self) -> int:
        return len(self.roll_events)


@dataclass(frozen=True, slots=True)
class ContinuousSeries:
    """连续期货序列 —— 仅供展示,不可当成交价格。

    通过 ``AdjustmentMethod`` 消除换月跳空,使收益率序列连续。
    但调整后的价格不能用于成交 —— 成交必须用原始合约价格。
    """

    symbol: str
    method: AdjustmentMethod
    dates: tuple[str, ...]
    adjusted_closes: tuple[float, ...]
    roll_events: tuple[RollEvent, ...]

    @property
    def adjustment_method(self) -> str:
        return self.method.value


@dataclass
class _RollDetector:
    """成交量交叉换月检测器。"""

    roll_lookback: int = 5

    def should_roll(
        self,
        front_volumes: Sequence[float],
        next_volumes: Sequence[float],
        bar_index: int,
    ) -> bool:
        """当前 ``bar_index`` 时下月成交量是否连续超过当月。"""
        if bar_index < self.roll_lookback:
            return False
        if bar_index >= len(front_volumes) or bar_index >= len(next_volumes):
            return False
        for i in range(bar_index - self.roll_lookback + 1, bar_index + 1):
            if i < 0 or i >= len(next_volumes) or i >= len(front_volumes):
                return False
            if next_volumes[i] <= front_volumes[i]:
                return False
        return True


def build_active_series(
    *,
    symbol: str,
    front_closes: Sequence[float],
    front_opens: Sequence[float],
    front_volumes: Sequence[float],
    next_closes: Sequence[float] | None = None,
    next_volumes: Sequence[float] | None = None,
    front_contract_id: str = "FRONT",
    next_contract_id: str = "NEXT",
    dates: Sequence[str] | None = None,
    roll_method: RollMethod = RollMethod.VOLUME,
    roll_lookback: int = 5,
    roll_days_before_expiry: int = 5,
    forced_roll_indices: Sequence[int] | None = None,
) -> ActiveContractSeries:
    """从主力/次主力合约数据构造活跃合约序列。

    输入是两条原始合约价格序列(front = 当月, next = 下月)。
    输出是一条带有换月事件的活跃合约序列。

    当 ``roll_method=VOLUME`` 时,在下月成交量连续 ``roll_lookback`` 天
    超过当月时触发换月。``forced_roll_indices`` 可指定强制换月日历
    (用于 SCHEDULED 方法)。

    换月发生在换月日**收盘后**:信号在换月日的收盘价上生成,
    成交在次日开盘用新合约价格执行。因此活跃序列在换月日的 close
    仍为旧合约价格,次日 open 为新合约价格。
    """
    n = len(front_closes)
    if n == 0:
        raise ValueError("front_closes 不能为空")
    if len(front_opens) != n:
        raise ValueError("front_opones 长度必须等于 front_closes")
    if len(front_volumes) != n:
        raise ValueError("front_volumes 长度必须等于 front_closes")

    _dates = tuple(dates[i] if dates and i < len(dates) else str(i) for i in range(n))
    contract_ids: list[str] = [front_contract_id] * n
    roll_events: list[RollEvent] = []

    if next_closes is not None and next_volumes is not None:
        detector = _RollDetector(roll_lookback=roll_lookback)
        forced = set(forced_roll_indices) if forced_roll_indices else set()

        current_contract = front_contract_id
        for i in range(n):
            if roll_method is RollMethod.VOLUME:
                should = detector.should_roll(front_volumes, next_volumes, i)
                reason = "volume_crossover"
            elif roll_method is RollMethod.SCHEDULED:
                should = i in forced
                reason = "scheduled"
            else:
                should = False
                reason = "manual"

            if should and i < n - 1:
                front_px = front_closes[i]
                next_px = next_closes[i] if i < len(next_closes) else front_px
                ry = (front_px - next_px) / next_px if next_px > 0 else 0.0

                roll_events.append(RollEvent(
                    symbol=symbol,
                    bar_index=i,
                    from_contract=current_contract,
                    to_contract=next_contract_id,
                    front_price=front_px,
                    next_price=next_px,
                    roll_yield=ry,
                    reason=reason,
                ))
                current_contract = next_contract_id

            contract_ids[i] = current_contract

    return ActiveContractSeries(
        symbol=symbol,
        dates=_dates,
        closes=tuple(front_closes),
        opens=tuple(front_opens),
        volumes=tuple(front_volumes),
        contract_ids=tuple(contract_ids),
        roll_events=tuple(roll_events),
    )


def build_continuous_series(
    active: ActiveContractSeries,
    *,
    method: AdjustmentMethod = AdjustmentMethod.RATIO,
) -> ContinuousSeries:
    """从活跃合约序列构造连续序列(仅供展示)。

    * ``RATIO``:换月处按 next/front 比例回溯缩放,使收益率连续。
    * ``DIFFERENCE``:换月处按 (next - front) 差分平移历史价格。
    * ``NONE``:不做调整(有换月跳空)。
    """
    n = len(active.closes)
    adjusted = list(active.closes)

    if method is AdjustmentMethod.RATIO:
        ratio = 1.0
        roll_idx = 0
        rolls = active.roll_events
        for i in range(n):
            while roll_idx < len(rolls) and rolls[roll_idx].bar_index < i:
                ev = rolls[roll_idx]
                if ev.front_price > 0:
                    ratio *= ev.next_price / ev.front_price
                roll_idx += 1
            adjusted[i] = active.closes[i] * ratio

    elif method is AdjustmentMethod.DIFFERENCE:
        shift = 0.0
        roll_idx = 0
        rolls = active.roll_events
        for i in range(n):
            while roll_idx < len(rolls) and rolls[roll_idx].bar_index < i:
                ev = rolls[roll_idx]
                shift += ev.next_price - ev.front_price
                roll_idx += 1
            adjusted[i] = active.closes[i] + shift

    return ContinuousSeries(
        symbol=active.symbol,
        method=method,
        dates=active.dates,
        adjusted_closes=tuple(adjusted),
        roll_events=active.roll_events,
    )


__all__ = [
    "ActiveContractSeries",
    "ContinuousSeries",
    "RollEvent",
    "build_active_series",
    "build_continuous_series",
]
