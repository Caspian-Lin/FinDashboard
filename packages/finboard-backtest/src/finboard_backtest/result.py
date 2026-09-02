"""回测结果。

issue #56 起归档撮合模型 / 资产规则 / 费用假设 / 基准选择,使历史 run 可复现。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from finboard_backtest.metrics import DEFAULT_RISK_FREE_ANNUAL
from finboard_data.factors import FactorSnapshot
from finboard_shared.models import Fill, Order


@dataclass
class BacktestResult:
    """回测绩效报告。"""

    # 原始数据
    equity_curve: list[tuple[date, Decimal]] = field(default_factory=list)
    benchmark_curve: list[tuple[date, Decimal]] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    selection_snapshots: list[FactorSnapshot] = field(default_factory=list)

    # 绩效指标
    total_return: float = 0.0
    annualized_return: float = 0.0
    sharpe_ratio: float = 0.0
    # issue #262:rf=0/ddof=1 对照口径 + 主口径 rf 取值标注(随指标序列化,
    # 防止引擎默认 rf=3% 把低收益策略 Sharpe 拖近 0 后被误读为无风险调整价值)。
    sharpe_rf0: float = 0.0
    risk_free_annual: float = DEFAULT_RISK_FREE_ANNUAL
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    turnover: float = 0.0
    commission_paid: Decimal = Decimal("0")
    stamp_tax_paid: Decimal = Decimal("0")

    # 基准(issue #184:基准缺失时为 None,禁止静默 0.0)
    benchmark_return: float | None = None
    excess_return: float | None = None
    # issue #254:基准曲线的实际来源(explicit_symbol:<code> /
    # equal_weight_selection_pool / equal_weight_static_pool / first_symbol),
    # 回测配置与实际使用的基准口径可区分。
    benchmark_source: str | None = None

    # 元信息
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: Decimal = Decimal("0")
    final_equity: Decimal = Decimal("0")
    dataset_versions: dict[str, list[str]] = field(default_factory=dict)
    factor_version: str | None = None

    # 研究级成交语义归档(issue #56)
    matching_model: dict[str, object] = field(default_factory=dict)
    asset_rules: dict[str, object] | None = None
    fee_assumptions: dict[str, object] = field(default_factory=dict)
    benchmark_config: dict[str, object] = field(default_factory=dict)

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
            f"夏普比率:   {self.sharpe_ratio:.2f} (rf={self.risk_free_annual:.1%}/年,"
            f"日频 rf/252,ddof=0)",
            f"夏普(rf=0): {self.sharpe_rf0:.2f} (与研究报告 sharpe_ratio 同口径)",
            f"最大回撤:   {self.max_drawdown:.2%}",
            f"胜率:       {self.win_rate:.2%}",
            f"交易次数:   {self.trade_count}",
            f"换手率:     {self.turnover:.2f}",
            "",
            f"佣金支出:   ¥{self.commission_paid:,.2f}",
            f"印花税:     ¥{self.stamp_tax_paid:,.2f}",
        ]
        if self.benchmark_curve and self.benchmark_return is not None:
            lines += [
                "",
                f"基准收益:   {self.benchmark_return:+.2%}",
                f"超额收益:   {self.excess_return:+.2%}",
            ]
            if self.benchmark_source:
                lines += [f"基准来源:   {self.benchmark_source}"]
        elif self.benchmark_curve:
            lines += ["", "基准收益:   缺失(未计算)"]
        if self.matching_model:
            lines += [
                "",
                f"撮合模型:   {self.matching_model.get('matching_model_version', '?')}"
                f" / fill={self.matching_model.get('fill_timing', '?')}",
                f"规则版本:   {self.matching_model.get('asset_rules_version', '?')}",
            ]
        return "\n".join(lines)
