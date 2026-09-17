# 统一数据链路方案(2026-09-09)

> 输入拍板:tushare **≤2000 积分档全量纳入**(股票/指数/债券/期货;港股不做,ETF 不做
> tushare 源);因子按 [[tushare-factor-roadmap-survey-20260909]] 路线借清单自研;
> **不考虑旧数据兼容**(payload/表结构/批次可直改)。
> 代码盘点以 2026-09-09 工作区为准;背景事故 [[tushare-dirty-row-skip-389]]。

## 一、现状盘点(拉取路径 5 条)

| job kind | 干什么 | 源路由 | 落点 |
|---|---|---|---|
| `data_sync` | 标的登记(发现) | akshare(指数/期货主连为受控登记表,无网络) | PG `instruments` |
| `bulk_download` | bars 行情 | 股票 akshare/tushare/yfinance 回落链;指数 akshare/index_daily;ETF 仅 akshare;转债仅 tushare cb_daily;期货主连仅 akshare | data_cache parquet |
| `fetch_all` | symbols.yaml 池拉 bars(遗留) | 回落链 | 同上 |
| `research_data_sync` | 6 研究数据集 | 恒 tushare | PG `research_*` 表 |
| `quality_repair` | 按标的缓存修复 | 回落链 | data_cache |

**Provider 封装现状**:AkShare(股票/指数/ETF 日线分钟线、期货主连+官网合约、转债一览、集思录强赎);TushareBar(daily+adj_factor+**suspend_d 已封装未入研究集**、cb_daily、index_daily;拒 ETF/期货);TushareResearch(stock_basic/daily_basic/fina_indicator/index_member_all/namechange/cb_basic 共 6 接口);YFinance(兜底,仅股票);Fallback(包装器)。预算:RPM 200/日 10 万,进程内共享锁。

**因子现状**:FACTOR_LAB_CATALOG(v2 唯一事实源)26 因子——signal_eligible 13 个(pb/earnings_yield/dividend_yield/turnover_rate/roe/gpm/debt_to_assets/revenue_yoy/momentum20/vol20/60/120/downside_vol60)+ 风险暴露 7 + 市场输入 6;信号引擎逐期重算仅 momentum/vol;**factor_series 通道仅用户沙箱因子,无平台内置因子入口**;两个投影目录(v1 selection、scorer catalog)。

## 二、数据类型全景(现有 + 新增)

### A. 行情 bars 类(parquet 缓存 → 冻结发布)

| 数据 | 主源 | 副源 | 备注 |
|---|---|---|---|
| 股票日线 qfq | tushare daily+adj_factor+suspend_d | akshare/yfinance | 建议主源切 tushare(源口径稳定,见旧调研建议) |
| 指数日线 | tushare index_daily | akshare | #341 实测 2000 档可调 |
| ETF 日线 | akshare fund_etf_hist_em | yfinance | **tushare 源不做**(拍板;#341 实测 fund_daily 2000 可调但复权口径未对齐) |
| 转债日线 | tushare cb_daily | — | 2000 档 |
| 期货主连日线 | akshare futures_main_sina | 待定 | **fut_daily 积分档待实测**(#267 记录另档积分;若 ≤2000 则入副源) |
| 期货具体合约 | 交易所官网 get_futures_daily | — | 不入逐标的缓存,现状保持 |

### B. 研究数据类(PG research_* → 冻结发布)

| 数据集 | 接口 | 积分 | 状态 |
|---|---|---|---|
| instrument_profiles | stock_basic | 免费 | 已有(L+D) |
| name_changes | namechange | 免费 | 已有 |
| daily_metrics | daily_basic | 2000 | 已有(18 字段白名单) |
| financial_indicators | fina_indicator | 2000 | 已有(**15 字段,批次 3 扩 ~20:roa/周转率族/流动速动比率/ICR/单季 QoQ 等**) |
| industry_memberships | index_member_all | 2000 | 已有 |
| convertible_profiles | cb_basic | 2000 | 已有 |
| **suspensions 停复牌** | suspend_d(doc_id=214) | 2000 | **新增**——可交易性日历;流动性因子原料+回测撮合假设;provider 已有 fetch_suspension_events |
| **trade_cal 交易日历** | trade_cal | 免费 | **新增落库**(现在 akshare 现拉不落库,#212 遗留缺口;#334 并集日历的持久化) |
| **income / balance / cashflow 三表** | 同名接口 | 2000 | **新增**(批次 4;Value 11 + Quality 补全 ~30 因子的原料) |
| **dividends 分红明细** | dividend | 2000 | **新增**(批次 4;精确股息率,过渡期用 dividend_yield_ttm 滚动近似) |
| index_dailybasic 指每日指标 | index_dailybasic | 2000 | 可选(市场层面估值/换手 → market_breadth/volatility_regime 从手工改真实数据) |

不做:港股(高积分)、ETF 的 tushare 源、分钟线(stk_mins 5000 档)。

## 三、拉取任务重构:数据集驱动的统一同步框架

**痛点**:bulk_download(行情)与 research_data_sync(研究数据)是两套平行框架——scope 谓词、源路由、预算、行级质量口径、批次记账各写一遍;fetch_all 与 bulk_download 功能重叠;新增一个数据集要改 payload contract/executor 分支/白名单多处。

**方案**(不考虑兼容,可直接重构):

1. **SyncSpec 注册表**:每个数据集注册一份规格——枚举形态(全量分页/按日全市场/按标的×区间)、fetcher 绑定、落点(PG 表 or parquet 缓存)、PIT 锚点(available_at 规则)、幂等键(dataset_version 形状)、质量门、增量窗口策略。框架统一消费 SyncSpec:进度上报、#383 timing、行级跳过口径(全市场枚举=行级跳过+具名告警,全脏行拒)、预算共享、research_sync_batches 记账,全部一处实现。
2. **job 收编**:research_data_sync 扩展为数据集驱动(名字可改 `dataset_sync`);bulk_download 保留(bars 是逐标的 parquet 写缓存,语义确实不同)但与 SyncSpec 共享 scope 谓词/源路由表/标的池解析;**fetch_all 废弃删除**(symbols.yaml 池改由 bulk_download 的 symbols 参数承担)。
3. **编排入口**:一个数据运维编排(REST/MCP)按依赖序拉起:登记 → bars 同步 → 研究同步 → 发布 → 快照/因子构建;「新增一个数据集」收敛为「注册一个 SyncSpec + 发布白名单两处」。
4. **universe 过滤参数统一**:exchange/listing_boards/instrument_type/symbols 四元组(#385 的过滤语义)提升为框架级公共参数,所有数据集共用同一解析。

## 四、因子层统一(承接批次 0-5)

因子形态收敛为三分,消费端统一:

1. **平台预置因子(新)**:内置目录(公式即代码,`finboard_backtest.factors.predefined`)→ **平台预置 factor_series 构建通道**(批次 0 基座,复用 #359-#361 管线:内容寻址/前缀不变性审计/覆盖检查白拿;截面因子在容器内套 `is_benchmark_only_instrument` 排除,#380 教训)→ `research_factor_series` 落库 → 信号引擎经 SeriesLookup 消费(与用户因子同通道)。
2. **用户沙箱因子(u_)**:现状不变。
3. **引擎即时价格特征**:保留 momentum/vol 少数几个,作为免预构建基础特征。

目录:FACTOR_LAB_CATALOG 扩登记段(predefined/user/engine-instant),v1 selection 与 scorer catalog 继续投影;202 因子 ≈ 60-70 原子算子实现,同族窗口变体一份参数化代码按 tushare 命名展开注册。

**批次**(每批 ≈ 一个 issue,承接调研):0 基座(算子库+预置因子通道)→ 1 量价 ~75(零缺口)→ 2 Alpha101 31 → 3 财务白名单扩展+Growth/Quality ~40 → 4 三表+dividend 同步+Value/Quality 补全 ~30 → 5 因子质量评估闭环(IC/分组/评分接入)。排除:log_price/ma_20d/price_dist/days_down_up。

## 五、统一链路总图

```
登记   data_sync ──▶ instruments(股票/ETF/指数/转债/期货主连)
                        │
行情链  bulk_download(SyncSpec 共享 scope+源路由)
          stock: tushare daily+adj+suspend(主) ← akshare/yfinance(备)
          index: index_daily │ convertible: cb_daily │ etf: akshare │ futures: 新浪
             ▼
        data_cache parquet ──发布──▶ data_releases/<bars>
                                             │
研究链  dataset_sync(统一 SyncSpec 框架)      │
          已有 6 集 + suspend_d/trade_cal/三表/dividend(新增)
             ▼
        PG research_* ──发布──▶ data_releases/<daily_metrics|financial|...>
                                             │
因子层  批次0 预置因子通道(内置目录→factor_series)│ 批次1-4 因子量产
                                             ▼
        research_factor_series(内容寻址)+ 冻结快照(基本面)
                                             ▼
消费层  research_run/backtest:dataset_release_ids 联合引用 + SeriesLookup 统一取因子
```

## 六、拍板记录(2026-09-09,已确认)

1. **框架载体**:research_data_sync 改名 `dataset_sync` + 删 fetch_all ✅
2. **股票 bars 主源切 tushare**(daily+adj_factor+suspend_d,akshare/yfinance 降副源)✅
3. **index_daily / index_basic / fut_basic / fut_daily / fut_trade_cal 均 2000 积分档可调(实测)**,纳入方案;index_dailybasic 未确认暂缓 backlog ✅
4. **因子批次 0-5 顺序与排除清单**(log_price/ma_20d/price_dist/days_down_up)✅

issue 拆分已创建:总览 #391;框架 #392(dataset_sync);数据源 #393(股票主源)/#394(指数)/#395(期货)/#396(trade_cal+suspend_d)/#397(三表+dividend);因子 #398(批次0)/#399(批次1)/#400(批次2)/#401(批次3)/#402(批次4)/#403(批次5)。
