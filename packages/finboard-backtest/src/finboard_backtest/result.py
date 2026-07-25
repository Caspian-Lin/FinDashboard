"""回测结果。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finboard_shared.models import Fill, Order


@dataclass
class BacktestResult:
    """回测绩效报告。"""

    # 原始数据
    equity_curve: list[tuple[date, Decimal]] = field(default_factory=list)
    benchmark_curve: list[tuple[date, Decimal]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)

    # 绩效指标
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    commission_paid: Decimal = Decimal("0")
    stamp_tax_paid: Decimal = Decimal("0")

    # 基准
    benchmark_return: float = 0.0
    excess_return: float = 0.0

    # 元信息
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")

    def summary(self) -> str:
        """生成文本绩效摘要。"""
        lines = [
            "=== 回测报告 ===",
            f"区间:       {self.start_date} ~ {self.end_date}",
            f"初始资金:   ¥{self.initial_capital:,.2f}",
            f"最终权益:   ¥{self.final_equity:,.2f}",
            "",
            f"总收益率:   {self.total_return:+.2%}",
            f"年化收益率: {self.annualized_return:+.2%}",
            f"夏普比率:   {self.sharpe_ratio:.2f}",
            f"最大回撤:   {self.max_drawdown:.2%}",
            f"胜率:       {self.win_rate:.2%}",
            f"交易次数:   {self.trade_count}",
            f"换手率:     {self.turnover:.2f}",
            "",
            f"佣金支出:   ¥{self.commission_paid:,.2f}",
            f"印花税:     ¥{self.stamp_tax_paid:,.2f}",
        ]
        if self.benchmark_curve:
            lines += [
                "",
                f"基准收益:   {self.benchmark_return:+.2%}",
                f"超额收益:   {self.excess_return:+.2%}",
            ]
        return "\n".join(lines)
