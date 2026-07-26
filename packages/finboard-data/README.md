# finboard-data

历史行情数据源(akshare / yfinance)、时点化研究数据契约和本地 parquet 缓存。

## 使用

```python
from datetime import date

from finboard_data import AkShareProvider
from finboard_shared.models import Symbol
from finboard_shared.types import BarPeriod, Market

provider = AkShareProvider()
bars = await provider.fetch_bars(
    Symbol(code="510300.SH", market=Market.A_SHARE),
    BarPeriod.D1,
    start=date(2023, 1, 1),
    end=date(2024, 12, 31),
    adjust="qfq",
)
```

## CLI

```bash
finboard data fetch 510300.SH --period D1 --start 2023-01-01 --end 2024-12-31 --adjust qfq
```

## 依赖

`akshare`、`yfinance`、`tushare` 和 `pyarrow` 均为可选依赖(lazy import),
Linux CI 无需安装:

```bash
uv pip install -e ".[akshare,yfinance,tushare,cache]"
```

## 研究数据契约

`ResearchDataProvider` 提供四类只读数据:

- `InstrumentProfile`: 股票档案、上市状态及上市/退市日期。
- `DailySecurityMetrics`: 收盘价、换手率、估值、股本和市值。
- `FinancialIndicator`: 按公告版本保留的财务指标。
- `IndustryMembership`: 申万 2021 三级行业成员及有效区间。

公共契约只返回不可变 dataclass,不会泄漏 pandas/DataFrame 或 Tushare SDK 类型。
所有数值使用 `Decimal`;缺失值保持 `None`。Tushare 原始单位按下表转换:

| 数据 | Tushare 原始单位 | 契约单位 |
| --- | --- | --- |
| `total_mv` / `circ_mv` | 万元 | 人民币元(`× 10,000`) |
| `total_share` / `float_share` / `free_share` | 万股 | 股(`× 10,000`) |
| 换手率、股息率、ROE、利润率、增长率 | 百分数 | 小数(`/ 100`) |
| PE / PB / PS / 量比 / 每股指标 | 倍数或元/股 | 原值 |

### 时点语义

每条数据同时保存业务日期、`observed_at` 和 `available_at`:

- 每日指标在交易日上海时间 17:00 后可用。
- 财务指标只有在公告日结束后才可用,保守设为公告日次日 00:00。
- `stock_basic` 与 `index_member_all` 没有历史发布时间,因此档案和行业成员的
  `available_at` 使用本次 `observed_at`。不能把今天看到的快照回填成过去已知数据。
- 后续快照层负责把 `available_at` 对齐到真实交易日;Provider 不猜测节假日。

这些约束用于防止回测未来函数。Provider 只读取外部研究数据,不会访问券商、
产生订单、修改持仓或自动注入实时策略。

### Tushare Provider

安装可选依赖并通过环境变量注入 token:

```bash
export FINBOARD_TUSHARE_TOKEN="..."
uv pip install -e "packages/finboard-data[tushare]"
```

```python
from datetime import date

from finboard_data import TushareResearchDataProvider

research = TushareResearchDataProvider()
metrics = await research.fetch_daily_metrics(date(2026, 7, 24))
financials = await research.fetch_financial_indicators(
    "000001.SZ",
    start_period=date(2025, 1, 1),
    end_period=date(2026, 6, 30),
)
```

Provider 只在显式构造且未注入 client 时加载 Tushare SDK。token 不会进入
`repr`、日志或 Provider 异常。上游字段缺失、非法日期/代码、无穷数值或坏行会
拒绝整批结果;不会静默返回部分记录。扩展新数据源时应实现
`ResearchDataProvider`,并在适配器边界完成代码、时间、单位和空值规范化。

为避免 Tushare 行数上限造成静默截断,财务指标必须提供起止报告期,行业成员
必须按股票代码读取。任一接口返回行数达到官方上限时 Provider 会拒绝结果,
调用方应缩小日期或标的范围后重试。
