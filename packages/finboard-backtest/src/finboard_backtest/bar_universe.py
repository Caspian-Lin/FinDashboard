"""Bar 规则动态选股组件 —— 不依赖因子系统的研究级选股器。

与 :mod:`finboard_backtest.selection` 的关系:

* :class:`PointInTimeFactorSelector` 在 T 日收盘后从研究数据库读取横截面快照,
  产生 T+1 生效的冻结候选池,需要 DB / 因子目录与版本管理;
* :class:`BarUniverseSelector` 只消费策略已经收到的当前/历史 Bar,不需要任何
  I/O、不需要研究数据,适合作为研究期快速验证流动性 / 动量规则的轻量工具。

约束(详见 issue #43):

* 仅能缩小外部授权的静态候选池,不能添加新标的或触发数据下载;
* 预热完成前不入选,只读取按时间到达的当前/历史 Bar,不访问未来数据;
* 是无 I/O 的确定性纯函数组件,异常 Bar(非正价格、负成交量/成交额)按
  "安全不入选" 处理 —— 输入校验由调用方保证,这里再次防御性拒绝。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from finboard_shared.models import Bar


class BarUniverseMode(StrEnum):
    """支持的动态选股模式。"""

    ALL = "all"
    """全部候选标的,默认值,保持现有策略行为。"""

    LIQUIDITY_MOMENTUM = "liquidity_momentum"
    """在回溯窗口内同时校验平均成交额与区间动量。"""


@dataclass(frozen=True, slots=True)
class BarUniverseConfig:
    """Bar 规则选股参数。

    默认值 ``mode=ALL`` 等同于不启用动态选股 —— 所有静态候选标的都被视为
    入选,既有策略/回测语义保持不变。
    """

    mode: BarUniverseMode = BarUniverseMode.ALL
    lookback: int = 20
    min_avg_amount: Decimal | None = None
    min_momentum: Decimal | None = None
    exit_clear: bool = False

    def __post_init__(self) -> None:
        if self.lookback < 1:
            raise ValueError("universe lookback 必须 >= 1")
        if self.min_avg_amount is not None and self.min_avg_amount < 0:
            raise ValueError("universe min_avg_amount 不能为负")
        if self.min_momentum is not None and self.min_momentum < -1:
            raise ValueError("universe min_momentum 不能小于 -1(对应价格归零)")

    def enabled(self) -> bool:
        """是否启用动态过滤;``ALL`` 模式恒为 False。"""
        return self.mode is not BarUniverseMode.ALL


@dataclass(slots=True)
class _SymbolState:
    """单个标的的滚动窗口。"""

    closes: deque[Decimal]
    amounts: deque[Decimal]


class BarUniverseSelector:
    """按标的维护有限长度历史窗口的确定性 Bar 规则选股器。

    线程/协程不安全,预期由策略回调单线程驱动(与 :class:`MaCrossStrategy`
    一致)。多标的状态相互隔离 —— ``update`` 只会读取该 symbol 自己的窗口。
    """

    def __init__(self, config: BarUniverseConfig) -> None:
        self._config = config
        # 每个标的独立 deque,固定最大长度,过期数据自动丢弃
        lookback = config.lookback
        self._closes: dict[str, deque[Decimal]] = {}
        self._amounts: dict[str, deque[Decimal]] = {}
        self._lookback = lookback
        self._selected: dict[str, bool] = {}
        # 记录最近一次 update() 期间发生的 "入选 → 退出" 转换
        self._just_exited: set[str] = set()

    @property
    def config(self) -> BarUniverseConfig:
        return self._config

    def reset(self) -> None:
        """清空所有内部状态(主要用于测试)。"""
        self._closes.clear()
        self._amounts.clear()
        self._selected.clear()
        self._just_exited.clear()

    def is_selected(self, symbol_code: str) -> bool:
        """返回该标的在最近一次 :meth:`update` 后是否入选。

        ``ALL`` 模式恒为 True;``LIQUIDITY_MOMENTUM`` 模式在预热完成前为
        False,完成且满足阈值后为 True。
        """
        if not self._config.enabled():
            return True
        return self._selected.get(symbol_code, False)

    def update(self, bar: Bar) -> bool:
        """消费一根 Bar 并更新该标的的入选状态。

        返回值即 :meth:`is_selected` 的结果。规则:

        * 预热未满 ``lookback`` 根 Bar 时不入选;
        * 价格非正、成交量为负或成交额为负时安全不入选;
        * ``LIQUIDITY_MOMENTUM`` 同时校验平均成交额与区间动量;
          成交额缺失或为 0 时使用 ``close * volume`` 作为明确回退。

        期间发生 "入选 → 退出" 转换时会写入 :meth:`transitioned_out` 标记,
        下次对本标的的 ``update`` 会清除该标记。
        """
        code = bar.symbol.code
        # 每次新 bar 进入时清除上一根的过渡标记
        self._just_exited.discard(code)
        prev_selected = self._selected.get(code, False)

        if not self._config.enabled():
            # ALL 模式:仍记录窗口,便于切换模式时连续;但任何标的恒入选
            self._ensure_state(code)
            self._push_bar(code, bar)
            self._selected[code] = True
            return True

        self._ensure_state(code)
        # 异常 Bar:安全不入选,但仍记录(避免一根脏数据反复触发状态翻转)
        if bar.close <= 0 or bar.volume < 0 or bar.amount < 0:
            self._mark(code, selected=False, prev=prev_selected)
            return False

        self._push_bar(code, bar)

        closes = self._closes[code]
        amounts = self._amounts[code]
        if len(closes) < self._lookback:
            # 预热未满,禁止入选
            self._mark(code, selected=False, prev=prev_selected)
            return False

        if not _passes_avg_amount(amounts, self._config.min_avg_amount):
            self._mark(code, selected=False, prev=prev_selected)
            return False

        if not _passes_momentum(closes, self._config.min_momentum):
            self._mark(code, selected=False, prev=prev_selected)
            return False

        self._mark(code, selected=True, prev=prev_selected)
        return True

    def transitioned_out(self, symbol_code: str) -> bool:
        """该标的在最近一次 :meth:`update` 期间发生 "入选 → 退出" 转换。

        用于策略感知 "退出" 事件以触发清仓卖出。从未入选过的标的不会触发。
        每次调用 :meth:`update` 会重置对应标的的标记。
        """
        return symbol_code in self._just_exited

    # ------------------------------------------------------------------ 内部
    def _mark(self, code: str, *, selected: bool, prev: bool) -> None:
        if prev and not selected:
            self._just_exited.add(code)
        self._selected[code] = selected

    def _ensure_state(self, code: str) -> None:
        if code not in self._closes:
            self._closes[code] = deque(maxlen=self._lookback)
            self._amounts[code] = deque(maxlen=self._lookback)

    def _push_bar(self, code: str, bar: Bar) -> None:
        amount = bar.amount
        if amount <= 0:
            # 成交额缺失/为 0 时回退为 close * volume(明确契约)
            amount = bar.close * bar.volume
            if amount < 0:
                amount = Decimal("0")
        self._closes[code].append(bar.close)
        self._amounts[code].append(amount)


def _passes_avg_amount(
    amounts: deque[Decimal],
    minimum: Decimal | None,
) -> bool:
    if minimum is None:
        return True
    if not amounts:
        return False
    avg = sum(amounts) / Decimal(len(amounts))
    return avg >= minimum


def _passes_momentum(
    closes: deque[Decimal],
    minimum: Decimal | None,
) -> bool:
    if minimum is None:
        return True
    if len(closes) < 2:
        return False
    start = closes[0]
    end = closes[-1]
    if start <= 0:
        return False
    return end / start - Decimal(1) >= minimum
