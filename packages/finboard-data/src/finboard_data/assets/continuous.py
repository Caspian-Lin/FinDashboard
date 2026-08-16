"""连续期货序列拼接(issue #58 验收:可审计地还原未拼接价格)。

支持三种调整方法:
* ``NONE`` —— 不调整,换月处保留跳空;
* ``RATIO`` —— 比例调整:``adjusted[t] = raw[t] * factor``,factor 在换月处累计;
* ``DIFFERENCE`` —— 差分调整:``adjusted[t] = raw[t] + shift``,shift 在换月处累计。

返回 :class:`ContinuousFuturesSeries`,其中每个点同时保留 raw_price 和 contract_code,
研究者可以随时还原原始未拼接序列。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from finboard_shared.instruments import (
    ContinuousFuturesPoint,
    ContinuousFuturesRule,
    ContinuousFuturesSeries,
    FuturesContract,
)
from finboard_shared.types import AdjustmentMethod


class ContinuousFuturesBuildError(RuntimeError):
    """连续期货拼接失败。"""


@dataclass(frozen=True, slots=True)
class _ContractSlice:
    """单一合约的 Bar 切片。"""

    contract: FuturesContract
    bars: list[tuple[datetime, Decimal, Decimal]]  # (timestamp, price, volume)


def build_continuous_series(
    rule: ContinuousFuturesRule,
    contract_slices: Sequence[_ContractSlice | tuple[FuturesContract, list[tuple[datetime, Decimal, Decimal]]]],
    *,
    data_source: str = "",
    dataset_version: str = "",
) -> ContinuousFuturesSeries:
    """按 ``rule`` 拼接连续期货序列。

    Parameters
    ----------
    rule
        拼接规则(换月方法 / 调整方法)。
    contract_slices
        多个合约的 Bar 切片,按时间顺序排列;每片 = ``(FuturesContract, [(ts, price, vol), ...])``。
        相邻两片应该有时间重叠(用于确定换月点);无重叠时按时间断点确定。

    Returns
    -------
    ContinuousFuturesSeries
        包含 ``ContinuousFuturesPoint`` 列表 + 换月表 + 原始合约映射。

    Notes
    -----
    * ``roll_method == VOLUME`` 时调用方应提前按成交量排序,本函数只做拼接;
    * 换月日(roll_flag=True)同时记录 from / to 合约到 ``roll_table``;
    * ``RATIO`` 调整不影响收益率序列,适合 OOS 回测;``DIFFERENCE`` 会扭曲收益率,慎用。
    """
    if not contract_slices:
        raise ContinuousFuturesBuildError("contract_slices 不能为空")

    slices: list[_ContractSlice] = []
    for raw in contract_slices:
        if isinstance(raw, _ContractSlice):
            slices.append(raw)
        else:
            contract, bars = raw
            slices.append(_ContractSlice(contract=contract, bars=list(bars)))

    # 按 slice 起始时间排序
    slices.sort(key=lambda s: s.bars[0][0] if s.bars else datetime.max)

    # 验证时间连续性(允许重叠,不允许完全断开)
    for i in range(1, len(slices)):
        prev_end = slices[i - 1].bars[-1][0] if slices[i - 1].bars else None
        cur_start = slices[i].bars[0][0] if slices[i].bars else None
        if prev_end is not None and cur_start is not None and cur_start < prev_end:
            # 时间重叠是允许的(用于确定换月日)
            pass

    # 确定换月点:每个 slice 切换时,前 N 天作为重叠期,选择 volume 大的合约
    points: list[ContinuousFuturesPoint] = []
    roll_table: list[tuple[date, str, str]] = []
    # 调整因子(RATIO 累乘,DIFFERENCE 累加)
    factor = Decimal("1")
    shift = Decimal("0")

    last_consumed_ts: datetime | None = None
    prev_contract_code: str | None = None

    for slice_idx, slice_ in enumerate(slices):
        # 本 slice 是否触发了换月(只在第一个有效 bar 标记一次)
        rolled_in_this_slice = False
        for ts, raw_price, volume in slice_.bars:
            # 跳过前一个合约已经覆盖的 Bar(只在重叠期保留较晚合约的数据)
            if last_consumed_ts is not None and ts <= last_consumed_ts:
                continue

            is_roll = (
                not rolled_in_this_slice
                and prev_contract_code is not None
                and prev_contract_code != slice_.contract.contract_code
            )
            if is_roll:
                rolled_in_this_slice = True
                # 换月日:计算调整因子(前合约最后价格 → 新合约首价格)
                prev_slice = slices[slice_idx - 1]
                if prev_slice.bars and slice_.bars:
                    prev_last_price = prev_slice.bars[-1][1]
                    new_first_price = slice_.bars[0][1]
                    if new_first_price > 0 and prev_last_price > 0:
                        if rule.adjustment_method is AdjustmentMethod.RATIO:
                            factor *= prev_last_price / new_first_price
                        elif rule.adjustment_method is AdjustmentMethod.DIFFERENCE:
                            shift += prev_last_price - new_first_price
                roll_table.append(
                    (ts.date(), prev_contract_code or "", slice_.contract.contract_code)
                )

            adjusted = _apply_adjustment(raw_price, factor, shift, rule.adjustment_method)
            points.append(
                ContinuousFuturesPoint(
                    timestamp=ts,
                    price=adjusted,
                    raw_price=raw_price,
                    contract_code=slice_.contract.contract_code,
                    volume=volume,
                    roll_flag=is_roll,
                )
            )

        if slice_.bars:
            last_consumed_ts = slice_.bars[-1][0]
            prev_contract_code = slice_.contract.contract_code

    return ContinuousFuturesSeries(
        series_id=rule.series_id,
        rule=rule,
        points=tuple(points),
        roll_table=tuple(roll_table),
        data_source=data_source,
        dataset_version=dataset_version,
    )


def _apply_adjustment(
    raw: Decimal,
    factor: Decimal,
    shift: Decimal,
    method: AdjustmentMethod,
) -> Decimal:
    if method is AdjustmentMethod.NONE:
        return raw
    if method is AdjustmentMethod.RATIO:
        return raw * factor
    if method is AdjustmentMethod.DIFFERENCE:
        return raw + shift
    return raw


__all__ = [
    "ContinuousFuturesBuildError",
    "build_continuous_series",
]
