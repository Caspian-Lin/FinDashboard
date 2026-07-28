"""期货时间序列动量(TSMOM)策略研究框架。

issue #64: 仅用于离线研究 / 回测。不连接实盘,不发送订单。

核心模块:
- ``config`` — 版本化配置(信号 / 风险预算 / 费用 / 换月)
- ``contracts`` — 合约规格(乘数 / 保证金 / tick / 手续费)
- ``signals`` — TSMOM 信号(多 lookback / 波动率缩放)
- ``roll`` — 换月检测 + 展期收益 + 连续序列
- ``backtest`` — 多空 / 保证金 / 每日盯市模拟器
- ``analysis`` — 收益归因 / 资金可行性 / 压力测试
"""

from __future__ import annotations

from .analysis import (
    CapitalTierFeasibility,
    CostAttribution,
    CrisisPerformance,
    ReturnAttribution,
    StressTestResult,
    TsmomAnalysis,
    analyze_tsmom,
)
from .backtest import (
    DailyRecord,
    FuturesTrade,
    TradeAction,
    TsmomResult,
    run_backtest,
)
from .config import (
    FUTURES_TSMOM_VERSION,
    FuturesTsmomConfig,
    TsmomParameterGrid,
    TsmomSignalFamily,
)
from .contracts import (
    DEFAULT_CONTRACT_SPECS,
    IC_SPEC,
    IF_SPEC,
    IH_SPEC,
    T_SPEC,
    TF_SPEC,
    TS_SPEC,
    ContractSpec,
    FuturesMarket,
    resolve_contract_spec,
)
from .roll import (
    ActiveContractSeries,
    ContinuousSeries,
    RollEvent,
    build_active_series,
    build_continuous_series,
)
from .signals import (
    TsmomSignal,
    compute_composite_score,
    generate_signals,
    generate_tsmom_signal,
    past_return,
    realized_volatility,
)

__all__ = [
    "DEFAULT_CONTRACT_SPECS",
    # config
    "FUTURES_TSMOM_VERSION",
    "IC_SPEC",
    "IF_SPEC",
    "IH_SPEC",
    "TF_SPEC",
    "TS_SPEC",
    "T_SPEC",
    # roll
    "ActiveContractSeries",
    # analysis
    "CapitalTierFeasibility",
    "ContinuousSeries",
    # contracts
    "ContractSpec",
    "CostAttribution",
    "CrisisPerformance",
    # backtest
    "DailyRecord",
    "FuturesMarket",
    "FuturesTrade",
    "FuturesTsmomConfig",
    "ReturnAttribution",
    "RollEvent",
    "StressTestResult",
    "TradeAction",
    "TsmomAnalysis",
    "TsmomParameterGrid",
    "TsmomResult",
    # signals
    "TsmomSignal",
    "TsmomSignalFamily",
    "analyze_tsmom",
    "build_active_series",
    "build_continuous_series",
    "compute_composite_score",
    "generate_signals",
    "generate_tsmom_signal",
    "past_return",
    "realized_volatility",
    "resolve_contract_spec",
    "run_backtest",
]
