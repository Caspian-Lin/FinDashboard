"""平台预置因子目录(issue #398,批次 0;批次 #399-#402 的注册地基)。

目录条目 = 「公式即代码」:name / 公式描述 / 数据依赖 / 方向 /
signal_eligible / 参数化窗口,``compute`` 是平台可信代码 —— 构建走
**进程内** factor_series 通道(免用户因子的容器税,审计 / 内容寻址 /
覆盖检查全套同构,见 ``research_sandbox.predefined_runner``)。

**引用命名** ``p_<name>``(``PREDEFINED_FACTOR_PREFIX``,与用户因子
``u_`` 对称,见 ``finboard_data.factor_lab``);目录内部只存裸名。

**批次 0 样板族** ``return_{21,63,126,252}d``:同一参数化实现按 tushare
命名展开注册(动量族 return_N;批次 1 起的 ~75 个量价因子照此模式批量
注册,注册指南见 PR「新预置因子注册指南」)。

**批次 3(#401)财务因子族** ``fin_*``(40 个 Growth / Quality):
数据依赖 = ``financial_indicators.<field>``,输入是**公告序列**(每行
一次公告修订,按 available_at 升序),采样取「决策日可见的最近一次
公告」→ 公告频率步进函数;单季 QoQ 直接用上游 q_ 前缀单季字段(诚实
取数,不做跨报告期自推导),加速度族为同比增速的公告序一阶差分。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from finboard_backtest.factors.predefined.context import (
    FactorSeriesFrame,
    PredefinedFactorInput,
)
from finboard_backtest.factors.predefined.operators import ts_delay, ts_delta
from finboard_data.factor_lab import FactorPreference

#: 因子裸名规则(目录内部不带 p_/u_ 前缀)
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: 目录级 schema 版本(参与 commit 锚;调整采样/依赖语义等跨因子规则时递增)
PREDEFINED_FACTORS_SCHEMA_VERSION = "v1"

#: compute 的函数签名:(PredefinedFactorInput) -> FactorSeriesFrame
PredefinedFactorCompute = Callable[[PredefinedFactorInput], FactorSeriesFrame]


@dataclass(frozen=True)
class PredefinedFactorDefinition:
    """一个平台预置因子的目录条目。

    * ``name`` —— 裸名(如 ``return_21d``),引用名 = ``p_return_21d``;
    * ``data_dependencies`` —— 数据依赖声明,条目形如
      ``"bars.<field>"`` / ``"daily_metrics.<field>"`` /
      ``"financial_indicators.<field>"`` / ``"index_bars.close"``(指数
      基准行情,引擎按发布 instruments 识别);构建引擎据此决定加载哪些
      挂载数据集,#401/#402 基本面因子由此声明研究发布依赖;
    * ``direction`` —— 研究语义方向(``FactorPreference``;HIGHER =
      值越大越看多),只做研究偏好,信号方向仍由策略规格决定;
    * ``implementation_version`` —— **实现版本**:公式 / 数值语义任何
      变化必须递增;它是内容寻址 commit 锚的原料(版本不变 → 同参数
      重建命中缓存,版本变化 → 新序列);
    * ``cross_section`` —— 最终值是截面算子产物:构建采样面收窄到
      可交易域(#380:截面分母不得混入 benchmark-only);时序因子
      False(全挂载标的,消费端统一剔除基准)。
    """

    name: str
    title: str
    family: str
    direction: FactorPreference
    signal_eligible: bool
    data_dependencies: tuple[str, ...]
    window: int | None
    implementation_version: str
    compute: PredefinedFactorCompute
    cross_section: bool = False

    def __post_init__(self) -> None:
        if not _NAME_RE.match(self.name):
            raise ValueError(
                f"预置因子名 {self.name!r} 不合规则(小写 [a-z][a-z0-9_]*,"
                "目录内部不带 p_/u_ 前缀)"
            )
        if not self.title or not self.family or not self.implementation_version:
            raise ValueError(f"{self.name}: title/family/implementation_version 必填")
        if self.window is not None and self.window < 1:
            raise ValueError(f"{self.name}: window 须 >= 1 或 None")
        if not self.data_dependencies:
            raise ValueError(f"{self.name}: 必须声明数据依赖")
        for item in self.data_dependencies:
            if "." not in item:
                raise ValueError(
                    f"{self.name}: 数据依赖 {item!r} 须为 '<dataset>.<field>' 形态"
                )


def _momentum_return(window: int) -> PredefinedFactorCompute:
    """参数化的 ``return_Nd`` 实现(同族窗口变体一份代码,#398)。

    ``return_Nd = close[t] / close[t-N] - 1``——N 根 bar 的区间收益
    (qfq 收盘;停牌缺行使窗口自然后移到最近可得 bar,「N 根 bar」
    而非「N 个日历日」);历史不足 N 根 → 缺测(None)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        closes = inp.bars("close")
        per_symbol: dict[str, Any] = {}
        for symbol, series in closes.items():
            base = ts_delay(series.values, window)
            per_symbol[symbol] = ts_delta(series.values, window) / base
        return inp.sample(closes, per_symbol)

    return compute


def _financial_level(field: str) -> PredefinedFactorCompute:
    """财务公告水平因子实现(#401):公告字段原值直接作为因子值。

    值 = 该标的财务公告序列(每行一次公告修订)的字段值,采样取
    「决策日可见的最近一次公告」—— 财务指标天然是公告频率的步进函数
    (公告之间持有上一期值);比率字段已由摄取层归一为小数
    (percent 类 ÷100,倍数/比率类原值)。因子必须因果:位置 ``i`` 的
    输出就是公告 ``i`` 自身,天然只依赖 ``<= i`` 的行。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        financial = inp.financial_indicators(field)
        return inp.sample(
            financial, {s: series.values for s, series in financial.items()}
        )

    return compute


def _financial_accel(field: str) -> PredefinedFactorCompute:
    """财务公告加速度因子实现(#401):同比增速的公告序一阶差分。

    ``value[i] = growth[i] - growth[i-1]``——最新公告的同比增速相对
    **上一条公告**(即上一报告期)的变化,衡量增长动量(加速/减速)。
    诚实边界:分母是「上一条公告」而非严格「去年同报告期」——上游
    修订公告(同报告期二次公告)会作为独立行插入序列,该位置差分值
    为修订前后增速之差(通常接近 0);首条公告 → NaN(缺测)。
    """

    def compute(inp: PredefinedFactorInput) -> FactorSeriesFrame:
        financial = inp.financial_indicators(field)
        per_symbol = {
            symbol: ts_delta(series.values, 1)
            for symbol, series in financial.items()
        }
        return inp.sample(financial, per_symbol)

    return compute


def _financial_definition(
    name: str,
    *,
    title: str,
    family: str,
    field: str,
    direction: FactorPreference = FactorPreference.HIGHER,
    compute: PredefinedFactorCompute | None = None,
) -> PredefinedFactorDefinition:
    """财务因子条目工厂(#401):公告频率步进序列,无参数化窗口。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=True,
        data_dependencies=(f"financial_indicators.{field}",),
        window=None,
        implementation_version="1",
        compute=compute or _financial_level(field),
    )


def _definition(
    name: str,
    *,
    title: str,
    family: str,
    window: int,
    direction: FactorPreference = FactorPreference.HIGHER,
    cross_section: bool = False,
    signal_eligible: bool = True,
    data_dependencies: tuple[str, ...] = ("bars.close",),
    implementation_version: str = "1",
) -> PredefinedFactorDefinition:
    """``return_Nd`` 族的条目工厂(唯一公式实现,窗口变体展开注册)。"""
    return PredefinedFactorDefinition(
        name=name,
        title=title,
        family=family,
        direction=direction,
        signal_eligible=signal_eligible,
        data_dependencies=data_dependencies,
        window=window,
        implementation_version=implementation_version,
        compute=_momentum_return(window),
        cross_section=cross_section,
    )


#: 目录(裸名 → 条目)。批次 0 样板:return_Nd 动量族(4 窗口变体);
#: 批次 1-4(~150+ 因子)按同一模式在此追加注册。
#:
#: 批次 3(#401):fina_indicator 白名单扩展解锁的 40 个 Growth / Quality
#: 财务因子 —— 数据依赖 = ``financial_indicators.<field>``,公告频率步进
#: 序列(采样取决策日可见的最近一次公告),字段由 #401 扩展白名单提供
#: (announcement_date PIT,available_at = 公告次日零点上海时区)。
PREDEFINED_FACTORS: dict[str, PredefinedFactorDefinition] = {
    item.name: item
    for item in (
        _definition(
            "return_21d",
            title="return_21d = close / close[-21] - 1(21 根 bar 区间收益,动量)",
            family="momentum",
            window=21,
        ),
        _definition(
            "return_63d",
            title="return_63d = close / close[-63] - 1(季度动量)",
            family="momentum",
            window=63,
        ),
        _definition(
            "return_126d",
            title="return_126d = close / close[-126] - 1(半年动量)",
            family="momentum",
            window=126,
        ),
        _definition(
            "return_252d",
            title="return_252d = close / close[-252] - 1(年度动量)",
            family="momentum",
            window=252,
        ),
        # ---- 批次 3(#401):Growth 族(13)--------------------------------
        _financial_definition(
            "fin_revenue_yoy",
            title="fin_revenue_yoy = 营业总收入同比增长率(累计口径,公告步进)",
            family="growth",
            field="revenue_yoy",
        ),
        _financial_definition(
            "fin_operating_revenue_yoy",
            title="fin_operating_revenue_yoy = 营业收入同比增长率(or_yoy)",
            family="growth",
            field="operating_revenue_yoy",
        ),
        _financial_definition(
            "fin_basic_eps_yoy",
            title="fin_basic_eps_yoy = 基本每股收益同比增长率",
            family="growth",
            field="basic_eps_yoy",
        ),
        _financial_definition(
            "fin_deducted_np_yoy",
            title="fin_deducted_np_yoy = 扣非归母净利润同比增长率(dt_netprofit_yoy)",
            family="growth",
            field="deducted_netprofit_yoy",
        ),
        _financial_definition(
            "fin_operating_profit_yoy",
            title="fin_operating_profit_yoy = 营业利润同比增长率(op_yoy)",
            family="growth",
            field="operating_profit_yoy",
        ),
        _financial_definition(
            "fin_netprofit_yoy",
            title="fin_netprofit_yoy = 归母净利润同比增长率(累计口径)",
            family="growth",
            field="net_profit_yoy",
        ),
        _financial_definition(
            "fin_ocf_yoy",
            title="fin_ocf_yoy = 经营活动现金流净额同比增长率(ocf_yoy)",
            family="growth",
            field="operating_cash_flow_yoy",
        ),
        _financial_definition(
            "fin_revenue_yoy_q",
            title="fin_revenue_yoy_q = 营业总收入同比增长率(单季度)",
            family="growth",
            field="revenue_yoy_q",
        ),
        _financial_definition(
            "fin_revenue_qoq",
            title="fin_revenue_qoq = 营业总收入环比增长率(单季度)",
            family="growth",
            field="revenue_qoq",
        ),
        _financial_definition(
            "fin_netprofit_yoy_q",
            title="fin_netprofit_yoy_q = 归母净利润同比增长率(单季度)",
            family="growth",
            field="netprofit_yoy_q",
        ),
        _financial_definition(
            "fin_netprofit_qoq",
            title="fin_netprofit_qoq = 归母净利润环比增长率(单季度)",
            family="growth",
            field="netprofit_qoq",
        ),
        _financial_definition(
            "fin_np_yoy_accel",
            title="fin_np_yoy_accel = 归母净利润同比增速的公告序一阶差分(增长加速度)",
            family="growth",
            field="net_profit_yoy",
            compute=_financial_accel("net_profit_yoy"),
        ),
        _financial_definition(
            "fin_revenue_yoy_accel",
            title="fin_revenue_yoy_accel = 营业总收入同比增速的公告序一阶差分(增长加速度)",
            family="growth",
            field="revenue_yoy",
            compute=_financial_accel("revenue_yoy"),
        ),
        # ---- 批次 3(#401):Quality 盈利族(12)---------------------------
        _financial_definition(
            "fin_roe",
            title="fin_roe = 净资产收益率(摊薄)",
            family="quality",
            field="return_on_equity",
        ),
        _financial_definition(
            "fin_roe_waa",
            title="fin_roe_waa = 加权平均净资产收益率",
            family="quality",
            field="weighted_return_on_equity",
        ),
        _financial_definition(
            "fin_roe_deducted",
            title="fin_roe_deducted = 净资产收益率(扣除非经常损益)",
            family="quality",
            field="roe_deducted",
        ),
        _financial_definition(
            "fin_roe_q",
            title="fin_roe_q = 净资产收益率(单季度)",
            family="quality",
            field="roe_q",
        ),
        _financial_definition(
            "fin_roa",
            title="fin_roa = 总资产报酬率(roa)",
            family="quality",
            field="return_on_assets",
        ),
        _financial_definition(
            "fin_roa_np",
            title="fin_roa_np = 总资产净利率(npta)",
            family="quality",
            field="return_on_assets_np",
        ),
        _financial_definition(
            "fin_roa_q",
            title="fin_roa_q = 总资产净利率(单季度,q_npta)",
            family="quality",
            field="return_on_assets_q",
        ),
        _financial_definition(
            "fin_roic",
            title="fin_roic = 投入资本回报率(roic)",
            family="quality",
            field="roic",
        ),
        _financial_definition(
            "fin_gross_margin",
            title="fin_gross_margin = 销售毛利率",
            family="quality",
            field="gross_profit_margin",
        ),
        _financial_definition(
            "fin_net_margin",
            title="fin_net_margin = 销售净利率",
            family="quality",
            field="net_profit_margin",
        ),
        _financial_definition(
            "fin_gross_margin_q",
            title="fin_gross_margin_q = 销售毛利率(单季度)",
            family="quality",
            field="grossprofit_margin_q",
        ),
        _financial_definition(
            "fin_net_margin_q",
            title="fin_net_margin_q = 销售净利率(单季度)",
            family="quality",
            field="netprofit_margin_q",
        ),
        # ---- 批次 3(#401):Quality 营运效率族(5)------------------------
        _financial_definition(
            "fin_inventory_turnover",
            title="fin_inventory_turnover = 存货周转率(次/报告期)",
            family="quality",
            field="inventory_turnover",
        ),
        _financial_definition(
            "fin_receivables_turnover",
            title="fin_receivables_turnover = 应收账款周转率(次/报告期)",
            family="quality",
            field="receivables_turnover",
        ),
        _financial_definition(
            "fin_current_assets_turnover",
            title="fin_current_assets_turnover = 流动资产周转率(次/报告期)",
            family="quality",
            field="current_assets_turnover",
        ),
        _financial_definition(
            "fin_fixed_assets_turnover",
            title="fin_fixed_assets_turnover = 固定资产周转率(次/报告期)",
            family="quality",
            field="fixed_assets_turnover",
        ),
        _financial_definition(
            "fin_total_assets_turnover",
            title="fin_total_assets_turnover = 总资产周转率(次/报告期)",
            family="quality",
            field="total_assets_turnover",
        ),
        # ---- 批次 3(#401):Quality 流动性 / 偿债族(6)-------------------
        _financial_definition(
            "fin_current_ratio",
            title="fin_current_ratio = 流动比率(流动资产/流动负债)",
            family="quality",
            field="current_ratio",
        ),
        _financial_definition(
            "fin_quick_ratio",
            title="fin_quick_ratio = 速动比率",
            family="quality",
            field="quick_ratio",
        ),
        _financial_definition(
            "fin_debt_to_assets",
            title="fin_debt_to_assets = 资产负债率(越高越看空)",
            family="quality",
            field="debt_to_assets",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_debt_to_equity",
            title="fin_debt_to_equity = 产权比率(负债/股东权益,越高越看空)",
            family="quality",
            field="debt_to_equity",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_interest_coverage",
            title="fin_interest_coverage = 已获利息倍数(EBIT/利息费用,ICR)",
            family="quality",
            field="interest_coverage",
        ),
        _financial_definition(
            "fin_equity_multiplier",
            title="fin_equity_multiplier = 权益乘数(总资产/股东权益,越高越看空)",
            family="quality",
            field="equity_multiplier",
            direction=FactorPreference.LOWER,
        ),
        # ---- 批次 3(#401):Quality 费用 / 现金流质量族(4)---------------
        _financial_definition(
            "fin_expense_ratio",
            title="fin_expense_ratio = 销售期间费用率(期间费用/营业总收入,越高越看空)",
            family="quality",
            field="expense_to_revenue",
            direction=FactorPreference.LOWER,
        ),
        _financial_definition(
            "fin_ocf_to_revenue",
            title="fin_ocf_to_revenue = 经营现金流净额/营业收入(盈利现金含量)",
            family="quality",
            field="ocf_to_revenue",
        ),
        _financial_definition(
            "fin_ocf_to_debt",
            title="fin_ocf_to_debt = 经营现金流净额/负债合计(偿债现金保障)",
            family="quality",
            field="ocf_to_debt",
        ),
        _financial_definition(
            "fin_ocfps",
            title="fin_ocfps = 每股经营活动现金流净额",
            family="quality",
            field="operating_cash_flow_per_share",
        ),
    )
}


def get_predefined_factor(name: str) -> PredefinedFactorDefinition:
    """按裸名取目录条目;未知名 KeyError 附可用清单(入队/编译期秒拒用)。"""
    try:
        return PREDEFINED_FACTORS[name]
    except KeyError:
        raise KeyError(
            f"未注册的平台预置因子: {name!r};可用: {sorted(PREDEFINED_FACTORS)}"
        ) from None


def is_registered_predefined_factor(name: str) -> bool:
    """裸名(或 p_ 引用名)是否已注册。"""
    bare = name.removeprefix("p_")
    return bare in PREDEFINED_FACTORS


def predefined_factor_names() -> tuple[str, ...]:
    """全部已注册裸名(稳定排序)。"""
    return tuple(sorted(PREDEFINED_FACTORS))


def predefined_factor_commit(name: str) -> str:
    """内容寻址的 commit 锚:``predefined-<sha256[:12]>``。

    锚原料 = schema 版本 + 目录条目的**语义字段**(name / 公式依赖 /
    窗口 / 截面标记 / 实现版本);title 等文档性字段不参与(改文案不使
    序列失效)。任何语义变化(实现版本递增 / 依赖调整)→ 新锚 → 新
    series_key → 托管重建;不变 → 同参数重建命中缓存。
    """
    item = get_predefined_factor(name)
    payload = {
        "schema_version": PREDEFINED_FACTORS_SCHEMA_VERSION,
        "name": item.name,
        "data_dependencies": sorted(item.data_dependencies),
        "window": item.window,
        "cross_section": item.cross_section,
        "implementation_version": item.implementation_version,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"predefined-{digest[:12]}"


def _validate_catalog() -> None:
    """导入期目录防漂移(与 BENCHMARK_INDEX_REGISTRY 同风格)。"""
    for name, item in PREDEFINED_FACTORS.items():
        if item.name != name:
            raise ValueError(f"目录键与条目名不一致: {name} vs {item.name}")
        expected = predefined_factor_commit(name)
        if not expected.startswith("predefined-"):
            raise ValueError(f"commit 锚形态非法: {name}")


_validate_catalog()

__all__ = [
    "PREDEFINED_FACTORS",
    "PREDEFINED_FACTORS_SCHEMA_VERSION",
    "PredefinedFactorCompute",
    "PredefinedFactorDefinition",
    "get_predefined_factor",
    "is_registered_predefined_factor",
    "predefined_factor_commit",
    "predefined_factor_names",
]
