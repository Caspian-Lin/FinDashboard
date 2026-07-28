# finboard-data

历史行情数据源(akshare / yfinance)、时点化研究数据契约、本地 parquet 缓存
和多资产元数据契约(issue #58)。

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

### 质量契约

`ResearchDataQualityValidator` 是无数据库、无网络访问的发布前质量门。同步方需
提供期望来源,日指标还需提供期望交易日;全市场/股票池任务应同时提供
`expected_symbols` 来检查覆盖率。

- `passed`: 数据可进入规范化存储并发布。
- `partial`: 只存在覆盖范围缺失/越界,保留批次用于补拉但不发布。
- `failed`: 空集、重复业务键、来源混入、无时区时间、陈旧数据、日期矛盾或数值
  范围异常,整批拒绝。

质量门不会自动修补、填充或删除坏行。原始响应归档、幂等重试和发布状态由
`finboard-persistence` 的 `ResearchDataSyncService` 管理。

## 因子目录与快照契约

`finboard_data.factors` 定义版本化 `FACTOR_CATALOG`。每项因子声明名称、版本、
更新频率、规范单位、依赖字段和 `strict` 时点安全级别。`FactorSelectionConfig`
定义过滤、全局/行业排名、行业上限和数据版本钉住规则;默认关闭。

读取边界 `FactorResearchReader` 必须一次批量返回同一来源和一组已发布
`dataset_versions`,且每条输入满足 `available_at <= decision_at`。输出
`FactorSnapshot` 记录 T 日决策时点、T+1 生效日、静态池、入选标的、因子值、
排名、配置和内容校验和。该契约只服务研究/回测候选集,不定义或执行交易动作。

新增因子时须同时登记目录元数据、依赖数据集和缺失值策略,提升
`factor_version`,并补充未来数据、修订公告、输入顺序和质量失败测试。

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

## 多资产元数据契约(issue #58)

`finboard_data.assets` 子包提供跨资产的元数据契约,支持股票、ETF、债券、
可转债、期货和指数,以及时点化的生命周期事件和连续期货拼接。

### 资产支持矩阵

| InstrumentType | AssetClass      | 示例代码       | 关键字段                         |
|----------------|-----------------|---------------|----------------------------------|
| `STOCK`        | EQUITY          | 600519.SH     | lot_size=100, T+1, 印花税万 5    |
| `ETF`          | EQUITY          | 510300.SH     | category: equity/bond/cross_border/... |
| `BOND`         | FIXED_INCOME    | 019547.SH     | coupon/maturity/duration/ytm     |
| `CONVERTIBLE`  | CONVERTIBLE     | 113001.SH     | conversion_price/forced_redeem/put_back |
| `FUTURES`      | DERIVATIVE      | IF2406.CFFEX  | multiplier/margin/price_limit    |
| `INDEX`        | EQUITY          | 000300.SH     | (不可交易,仅研究/基准)            |

### Fail-closed 解析

`InstrumentRegistry.resolve(code)` 不再默认回退到 A 股 —— 未知代码会 raise
`InstrumentResolutionError`,强制调用方显式注册:

```python
from finboard_data import InstrumentRegistry
from finboard_shared import Instrument, Market, InstrumentType, AssetClass

reg = InstrumentRegistry([
    Instrument(
        code="600519.SH", name="贵州茅台",
        market=Market.A_SHARE, instrument_type=InstrumentType.STOCK,
        asset_class=AssetClass.EQUITY,
    ),
])
inst = reg.resolve("600519.SH")  # OK
reg.resolve("UNKNOWN.XXX")       # raises InstrumentResolutionError
```

带后缀的常见代码可自动推断市场 + 类型(如 `510300.SH` → ETF,
`113001.SH` → CONVERTIBLE,`IF2406.CFFEX` → FUTURES),无需显式注册。

### 时点化生命周期事件

`LifecycleEvent` 强制 `available_at >= effective_date` 当日开盘,
防止未来信息泄漏:

```python
from datetime import date, datetime, timezone
from finboard_data.assets.provider import InstrumentMetadataProvider
```

### 连续期货序列

`build_continuous_series()` 按 `ContinuousFuturesRule` 把多个月份合约拼成
连续序列,支持 NONE / RATIO / DIFFERENCE 三种调整方法,每个点同时保留
`raw_price` 和 `contract_code`,可随时还原未拼接价格。

### 数据集覆盖率审计

`audit_dataset_coverage()` 根据每标的的实际起止日 / bar 数生成
`CoverageReport`,区分 full / short_history / gaps / delisted / missing,
质量不合格的数据集 `quality_status=FAILED`,回测引擎拒绝加载。

### 已知局限

* 本子包只定义契约,**不**实现具体的 akshare/tushare 抓取逻辑;
* 10万-50万元组合优先通过 **债券 ETF** 获取固收暴露,直接现券单列能力边界;
* 期货合约链的换月判定(`RollMethod=VOLUME`)由调用方提前按成交量排序,
  本子包只做拼接;
* 多资产能力属于研究 / 回测阶段,**不**授权多市场实盘或 CTP 实盘接入
  (见 AGENTS.md 阶段顺序)。
