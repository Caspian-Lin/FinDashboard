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

`akshare` 是标的池发现的基础依赖,会随 `finboard-data` 默认安装。
`yfinance`、`tushare` 和 `pyarrow` 仍按实际数据源/缓存能力选装:

```bash
uv pip install -e ".[yfinance,tushare,cache]"
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

### 因子实验室快照与信号契约(issue #78)

`finboard_data.factor_lab` 在既有候选集契约之上定义通用研究产物:

- `FactorDefinition`: 版本化区分 alpha、risk 和 market input,固定频率、窗口、
  偏好方向、转换、中性化、缺失策略、发布时间规则、代码版本和校验和。
- `FeatureSnapshot`: 只接收 `available_at <= decision_at` 的观测,并固定
  `release_id`、候选池版本、字段/窗口、构建版本和内容 checksum。
- `FactorSignal`: 仅允许 alpha 定义生成,逐标的记录方向、score、confidence、
  有效期、候选池版本和来源快照;风险因子与市场输入不能生成信号。
- `FactorExperiment`: 冻结样本内/样本外窗口、试验预算、基准、成本假设、数据/
  代码/因子版本,并显式记录失败或中断状态。

目录只暴露已有计算实现且能复现的因子。例如 `residual_momentum` 当前缺少残差
回归实现,不会出现在目录中。对同一 ID 重放不一致的 payload、让未来发布时间的
数据进入快照、数据发布 checksum 不匹配、跨版本混用或伪造样本外通过都会
fail closed。

这组对象都是研究契约。信号不是目标仓位或订单,也没有访问 Broker、账户和持仓的
能力;策略如何把已经验证的信号转换为仓位属于后续独立 issue。

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

`TushareBarProvider` 的批量日线更新默认使用 16 个有界 worker 和专用线程池;
所有远端 endpoint 仍由共享预算按配置 RPM 逐次放行,并在每个请求前把用量写入
`tushare_usage.json`。Parquet 对应的 `.meta.json` 会记录 `covered_ranges`,表示
已经成功查询的闭区间(包括停牌、上市前等合法无 Bar 日期)。进程中断后重新运行
时会用该覆盖信息与 Parquet 首尾日期计算剩余区间,完整覆盖则直接 `cache_hit`,
不会维护或执行一份独立的持久化任务队列。

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

## 不可变研究数据发布(issue #77)

`FrozenDatasetReleaseBuilder` 将可变的 `data_cache` 冻结为版本目录:

```text
data_releases/<release_id>/
├── manifest.json
└── bars/<symbol>_<period>_<adjust>.parquet
```

`manifest.json` 包含来源、资产类别/市场、日期范围、字段、复权方式、发布时间、
代码/元数据版本、已知局限和 SHA-256。每个标的还固定 `available_at`、上市/退市/
名称历史、生命周期事件覆盖和以下研究执行假设:

- `lot_size`、`price_tick`、交易日历和 `settlement_days`;
- 印花税/佣金假设;
- 期货 `multiplier`、`margin_rate` 与多空能力。

发布过程先写同一文件系统的隐藏 staging 目录,所有必需能力和逐标的质量门通过后
再原子改名。已有 `release_id` 永不覆盖;同一 schema 下字段漂移、缺文件、重复/
异常 Bar、低覆盖、源文件发布中变化、checksum 不一致、ETF 分类未知、可转债/
期货元数据或事件覆盖不足都会 fail closed。失败不会删除或替换之前的可用发布。

当前覆盖审计报告实际起止日、预计/缺失交易日、停牌代理 Bar、异常数、上市/
退市和短历史分类。首期交易日历使用工作日近似,中国节假日缺口以 warning 报告;
停牌以“成交量为零且 OHLC 不变”识别。这两点会写入发布的
`known_limitations`,不会静默伪装成完整交易所日历。

### 发布与校验

先应用迁移并确保 `instruments` 已同步、所选标的已在 `data_cache`:

```bash
uv run alembic upgrade head
uv run finboard data release \
  --release-id multiasset-2024-v1 \
  --version 2024-v1 \
  --symbols 600519.SH,510300.SH,513100.SH,518880.SH,511010.SH \
  --start 2024-01-01 \
  --end 2024-12-31

uv run finboard data release-verify multiasset-2024-v1
```

默认质量门要求股票、宽基/指数、跨境、商品和债券 ETF 能力。只有显式修改
`--required-capabilities` 才能发布较窄的数据集。首期内置的代表 ETF 目录包含
`510300.SH`、`159915.SZ`、`513100.SH`、`513500.SH`、`518880.SH`、
`511010.SH` 和 `511260.SH`;未知 ETF 不猜测分类。

PostgreSQL 的 `research_dataset_releases` 保存完整 manifest 和常用查询列:

| 方法 | 路径 | 内容 |
|---|---|---|
| `POST` | `/api/instruments/datasets/releases` | 从服务端本地缓存选择标的/日期并冻结发布 |
| `GET` | `/api/instruments/datasets/releases` | 版本、质量、能力与覆盖列表 |
| `GET` | `/api/instruments/datasets/releases/{release_id}` | 完整逐标的审计和资产规则 |

研究工作台的“数据与标的 → 数据发布”提供同一入口。发布请求只能提交结构化
标识、来源、日期、复权方式、标的和质量门;不能提交文件路径或代码。服务端固定
读取 `FINBOARD_DATA_CACHE_DIR`(默认 `data_cache`)并写入
`FINBOARD_DATA_RELEASE_ROOT`(默认 `data_releases`)。发布不会联网补数据,
不会自动启动因子计算、回测、模拟盘或实盘流程。

`FrozenReleaseProvider` 只读取调用方指定的 `release_id`,并在首次读取每个标的时
复核文件 checksum。标的不在发布中、周期/复权/市场不符或日期越界时直接拒绝,
绝不会回退到可变缓存或联网 Provider。

该功能只读研究数据,不访问 Broker、账户、订单、持仓或实盘 Risk Manager。

### 来源、许可与更新频率

发布器只冻结本地已有数据,不改变上游许可:

| 数据 | `source` 示例 | 建议更新频率 | 许可/使用边界 |
|---|---|---|---|
| A 股/ETF 日线 | `akshare` / `yfinance` | 每个交易日收盘后 | 遵守对应上游接口与原始数据提供方条款;仓库不附带再分发授权 |
| 标的/上市状态/改名 | #35 `instruments` / `instrument_names` | 每日发现任务 | AkShare 聚合数据仅用于获授权的内部研究 |
| ETF/债券/可转债/期货合约 | #58 元数据表 | 合约或产品变更后 | `source`、`dataset_version` 必须随记录保留;人工数据需记录出处 |
| 生命周期事件 | `instrument_lifecycle_events` | 公告后尽快 | Tushare/交易所/人工来源遵守各自许可,并保留 `available_at` |

操作者有责任在发布前确认账户套餐、下载频率、内部使用和再分发权限。未经许可不要
把 `data_releases` 上传到仓库或对外分发;目录已加入 `.gitignore`。来源许可变化
不修改历史发布,应新建版本并在 `known_limitations` 中记录。
