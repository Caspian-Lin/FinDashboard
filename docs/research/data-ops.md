# 数据基座运营手册(研究数据同步与主数据治理)

跨会话运营步骤的 canonical 事实源:研究 / 数据 agent 与主 coding agent 共享。
与 `research_memories` 记忆冲突时以本文档为准(#268 约定)。

## v1 选股(research_db)必需数据集的发布状态(#255)

**背景**:2026-09-01 runs 273-275(ROE / PB / 换手率 top-N)全部 0 交易
「成功」。根因:**摄取 ≠ 发布**——`dataset_sync` 会在质量门通过后自动
`mark_published`(批次 `status=published`),但 profiles 批次的**同步+发布**
步骤从未进入运行手册;`selection.inputs_mode=research_db`(默认)必需
`instrument_profiles` 批次已发布,未发布时旧链路逐期 SKIPPED、引擎 0 交易收场。

**已建的防线(#255)**:

- 入队期:REST `POST /api/backtest/run` 与 MCP `finboard_backtest_run`(同步 +
  异步入队)对 `enabled + inputs_mode=research_db` 的 selection 逐个检查
  `required_datasets` 的已发布批次,缺失秒级 422 / `invalid_argument`
  (`dataset_unpublished:{dataset}` 具名);
- 执行端:`run_backtest_and_persist`(worker,覆盖 grid 提交等旁路)重放同一
  检查,失败 `selection_dataset_unpublished` 具名错误,任务不重试;
- 引擎:选股启用的 run 一律随结果携带 `selection_diagnostics`
  (published/skipped 快照数、skip 原因计数、候选池是否曾生效),整期无候选
  时打 `backtest.selection_pool_never_active` warning 并在 `summary()` 标注
  「本 run 大概率 0 交易」。

**运营步骤(选股回测前的核验清单)**:

1. 核验批次发布状态(哪些 dataset、哪个 version、是否 published):

   ```sql
   SELECT dataset, source, dataset_version, status, published_at, accepted_rows
   FROM research_sync_batches
   WHERE source = 'tushare'
   ORDER BY dataset, id DESC;
   ```

   选股回测要求的 datasets 按 selection 配置推导(默认 research_db 至少含
   `instrument_profiles`;配置了市值/PB/换手过滤或排名再加 `daily_metrics`,
   ROE/毛利率/营收增速再加 `financial_indicators`)。

2. 缺失或 `status != 'published'` 时:`finboard_job_enqueue(kind=dataset_sync,
   payload={datasets: [...], symbols: [...], start_date: ..., end_date: ...})`
   摄取;质量门通过即自动发布(见任务 phase 摘要);质量门失败看批次
   `quality_report` 修复后重跑。

3. 若 selection 显式声明 `dataset_versions`,发布版本必须与之精确匹配
   (`published_version` 口径);通常不声明即可(取最近发布)。

4. 发布后入队仍有疑虑时:先跑 `finboard_strategy_validate`(research_run 侧
   universe 预检)或小规模同步 `finboard_backtest_run` 验证选股出单,再放大区间。

**边界**:粗粒度门只回答「是否存在已发布批次」;批次已发布但不覆盖具体
交易日(如 daily_metrics 只发到 2023)仍由逐期 SKIPPED 的 `skip_reason`
承载,配合 `selection_diagnostics.skip_reasons` 可见。

## instruments 主数据元数据(list_date / industry / delist_date / 名称历史,#251)

**背景**:`instruments` 主数据由 akshare 发现链路写入,只含
code/name/market/instrument_type/exchange/listing_board。list_date、industry、
delist_date 的结构化上游都在 tushare:

| 字段 | 上游 | 落点 | 回填路径 |
| --- | --- | --- | --- |
| list_date / industry | `stock_basic`(list_status=L) | `research_instrument_profiles` | data_sync 后置回填 |
| delist_date | `stock_basic`(list_status=D 退市档案) | 同上 | 同上 |
| 名称历史(PIT) | `namechange` | `instrument_names` 主数据表 | dataset_sync `name_changes` dataset 直接重建 |

**标准同步顺序**(每次刷新全市场主数据时):

1. `dataset_sync`(默认全部 datasets,或显式 `["profiles", "name_changes"]`)
   —— profiles 段同时拉取在市(L)与退市(D)档案合并进同一批次;
   name_changes 段全市场分页拉取名称变更并按半开区间重建 `instrument_names`
   (未提及 symbol 保留既有记录,重跑幂等)。
2. `dataset_release_publish`(release_kind=`instrument_profiles` 或联合发布)
   —— 发布 profiles 批次。发布构造器兜底(instruments 字段为 null 时从档案
   补齐)**仍要求已发布批次**;回填不要求(见下)。
3. `data_sync`(universe 全市场同步)—— 同步 `instruments` 并后置回填:
   从最近一次**实际摄取过档案的批次**(不论发布状态,#251 修复)回填
   list_date / industry / delist_date(只填 null,不覆盖)。
   `status` 不随回填变更 —— 退市状态由缺席二次确认(sync_with_diff)推进。

**验证 SQL**(回填效果):

```sql
-- 元数据非空率
SELECT count(*) FILTER (WHERE list_date IS NULL)  AS missing_list_date,
       count(*) FILTER (WHERE industry IS NULL)  AS missing_industry,
       count(*) FILTER (WHERE delist_date IS NULL) AS missing_delist_date,
       count(*) AS total
FROM instruments;

-- 名称历史覆盖(#213 ST-PIT 依赖)
SELECT count(DISTINCT instrument_code) AS covered FROM instrument_names;
```

缺失统计同时出现在 `data_sync` 任务的 phase 摘要与 bars 发布的
`quality_report.instrument_metadata`(missing_list_date / missing_industry /
missing_delist_date / with_name_history / name_history_coverage)。

**边界**:`sector` 无上游来源,保持 null;退市 `status` 变更只走缺席二次确认,
档案回填不触碰。

## 跨发布标的集一致性(#252)

**背景**:2026-09-01 实测全市场 `financial_indicators` 发布缺单只标的
002889.SZ(发布时点的 symbols 内联清单漏配),引用该发布的 multi_period
research_run 在执行期才发现并整条失败。三发布(bars / daily_metrics /
financial_indicators)的标的并集一致性必须自检。

**发布期校验**(推荐,秒级拦截):`dataset_release_publish` 发布研究发布时带
`consistency_baseline_release_id=<同区间 bars 主发布>` +
`consistency_fail_on_mismatch=true`——差集非空即拒绝发布(code=
`symbol_set_mismatch`,差集具名进错误 context);不带 fail 开关则只 warning。

**标的集来源复制(#261,消灭漏配根因)**:发布/重发布时标的集用
`symbols_from_release=<同区间 bars 主发布 release_id>` 直接复制其冻结标的集
(与内联 `symbols` / `full_market=true` 三选一,同时声明即 `invalid_argument`
拒绝),不再手工维护全市场清单——002889.SZ 这类内联清单漏配从源头消失;
来源发布不存在/不可用(quality 非 passed/warnings)入队即 422/`invalid_argument`
具名拒绝。REST `POST /api/datasets/releases` 同步支持
(`ResearchDatasetReleaseCreate.symbols_from_release` / `.full_market`)。

**full_market 板块/交易所过滤(#385)**:`full_market=true` 可叠加
`exchange`(SSE|SZSE|BSE|CFFEX)与 `listing_boards`(sse_main|szse_main|
star|chinext|bse|cdr)缩小展开范围(仅 full_market 模式生效,与其他标的来源
混用入队即拒)。ETF/指数/转债的 listing_board 恒为 `unknown`,mixed 发布
过滤后想保留它们须显式含 `unknown`。例:全市场股票发布剔除北交所 →
`listing_boards=[sse_main,szse_main,star,chinext,cdr]`(不含 bse)。

**发布后自检**(任一入口,同一实现):

- MCP:`finboard_dataset_release_diff(release_id=<研究发布>,
  other_release_id=<bars 主发布>)` → `consistent` 布尔 + 差集具名清单;
- REST:`GET /api/instruments/datasets/releases/{id}/symbol-diff?other_release_id=...`。

**002889.SZ 补齐运营步骤**(历史缺口的修复路径):

1. `dataset_sync`(datasets 含 `financial_indicators`,symbols 含
   002889.SZ,报告期区间覆盖该股全部历史)补齐摄取;
2. 用全市场 symbols 清单重发布 `financial_indicators`(带
   `consistency_baseline_release_id` + `fail_on_mismatch=true`);
3. `finboard_dataset_release_diff` 复核 vs bars 主发布 → `consistent: true`;
4. 引用旧发布的 research_run 用新 release_id 重新入队(发布不可变,不回填)。

**执行期语义**(兜底):研究发布缺标的在 research_run 执行期**不再炸整条
run**——缺失标的的研究因子值为 null,发具名 `research_release_missing_symbols`
warning(release_id + 缺失清单),与 factor_lab #212 容忍语义一致;bars 主
发布缺标的仍 fail-closed。

## 指数基准数据链路(#256,#184 运营化)

**背景**:2026-09-01 所有 research run 的 `benchmark_return`/`excess_return`
全 null——#184 读取端完整,但全链路没有 `instrument_type=index` 的登记写入者,
指数日线进不了缓存,也就进不了任何冻结发布。

**标准运营步骤**(以 000300.SH 基准为例):

1. `data_sync`(REST `POST /api/data/sync` / MCP `finboard_data_sync_universe`)
   —— **#394 起登记源为 tushare `index_basic` 全量**:`discover_indices`
   按 `is_index_code`(000xxx.SH / 399xxx.SZ / 899xxx.BJ)收窄登记域,
   A 股三所指数自动登记 `instrument_type=index` 行(编外市场 CSI/CIC/MSCI
   无行情上游,不登记);只登记在市(L)指数,退市交生命周期 diff。
   `BENCHMARK_INDEX_REGISTRY` 收窄为**基准资格白名单**(`is_benchmark_index`
   只认白名单;沪深300 / 中证500 / 中证1000 / 上证50 / 科创50 / 创业板指 /
   深证成指 / 北证50 等 9 只)—— 白名单外的指数照常登记 / 可缓存 / 可发布,
   但不是基准资格资产;扩展新基准指数直接在 `finboard_data/discovery.py`
   白名单加一行(代码必须满足 `is_index_code`,导入期断言)。
   **`data_sync` 现在依赖 `FINBOARD_TUSHARE_TOKEN`**(未配置具名失败不重试);
   index_basic `base_date`(基日)随登记携带,由执行器后置
   `backfill_listing_dates` 回填 `instruments.list_date`(只补 null,
   #185 语义),mixed 发布的 `missing_list_date` 不再被指数恒 null 抬高。
2. `bulk_download`(REST `POST /api/data/bulk-download` / MCP
   `finboard_data_bulk_download_start`)带 `instrument_type=index` ——
   **#394 起指数 bars 默认 tushare**(`index_daily` 主源,原始点位;
   未显式声明 source 且筛选域全指数时默认源覆盖为 tushare,显式
   `source=akshare` 恒优先,akshare `index_zh_a_hist` 降为副源)。
   #341 起 tushare 源放行指数(`index_daily` 专属接口,2000 积分档
   实测可调;无复权概念,缓存键沿用请求 adjust no-op);ETF/期货仍
   `tushare_scope_mismatch` 拒绝(不静默换源)。
3. `dataset_release_publish`(release_kind=`multi_asset_mixed`)—— **指数代码
   必须与股票放进同一份发布**(manifest 只允许一个 bars 主发布,基准行情与
   候选池同源);`adjustment` 用默认 `qfq`(与 bulk_download 缓存键一致;
   指数本身无复权概念,键只是缓存/发布分区)。发布后指数 instrument ready
   (asset_class=equity、零费用执行占位)。
4. research_run 入队时 `benchmark_config={"symbol": "000300.SH"}` ——
   `_load_benchmark_curve` 从同一 bars 发布 PIT 读取指数行情,
   `benchmark_return`/`excess_return` 非真实行情不落值(缺失仍 null + 具名
   warning,禁止静默 0.0,#184 不变量)。

**边界**:指数**不进候选池**(只做基准数据、不可撮合)——静态预检 /
入队空池门控 / 运行时候选构建三处共用 `is_benchmark_only_instrument`
排除指数;UNIVERSE artifact 中只有股票。验证 SQL:

```sql
-- 指数登记行
SELECT code, name, exchange, list_date FROM instruments WHERE instrument_type = 'index';
```

端到端回归:`tests/integration/test_index_benchmark_chain.py`(登记 → 混发
发布 → 真实 FrozenReleaseProvider worker run → `benchmark_return` 非 null +
UNIVERSE artifact 无指数)。

## ETF 行情链路与缓存多源策略(#257)

**背景**:2026-09-01 ETF 轮动 / 均值回归策略全链路空转。复现确认根因(本仓
无任何 secid 拼接代码,EM secid 前缀是 akshare 库内部行为):akshare 1.18.78
的股票日线接口内部按 ``6`` 开头判定沪市(``market_code = 1 if
symbol.startswith("6") else 0``),SH-ETF(51/56/58 段)一律被拼成深市
secid——实测 ``stock_zh_a_hist("510300")`` 请求 URL 携带 ``secid=0.510300``
(正确应为 ``1.510300``);正确路由 ``fund_etf_hist_em`` 的 ``get_market_id``
才能正确处理 5 开头沪市基金。第二个断点:引擎按注入 provider 读共享
parquet 缓存,``data_provider=tushare`` 时 tushare provider 把异源(akshare)
缓存视为全量缺口并丢弃 bars,指数/ETF 等不在 tushare scope 的标的 bars 恒空
(scope 是设计决定而非积分硬约束:实测 index_daily/fund_daily 2000 积分档可调,#341)。

**标准运营步骤**(以 510300.SH 为例):

1. `bulk_download` 带 `instrument_type=etf`、`source=akshare` —— ETF 日线经
   `fund_etf_hist_em` 进 parquet 缓存(缓存键与股票同为 `qfq`,列名一致)。
   **tushare 源对 ETF 保持拒绝**(`tushare_scope_mismatch`,#256 起既有行为
   ——scope 设计决定而非积分硬约束:2026-09-06 实测 `fund_daily` 2000
   积分档可调,官方文档标 5000 与实测不符,#341)。
2. 回测消费:`data_provider=tushare` 时,若异源缓存完整覆盖请求区间,
   tushare provider 直接 read-through 返回缓存(具名 log
   `tushare.foreign_cache_hit`,零 tushare 预算消耗);缺口区间 tushare
   拉不到时具名回退(`tushare.foreign_cache_fallback`)返回异源已缓存 bars,
   不再静默丢弃。缺口区间 tushare 拉得到(股票)时保持既有重建语义
   (重建纯 tushare 缓存),避免两种复权口径混在同一条权益曲线。
3. (可选)配置 `FINBOARD_DATA_FALLBACK_PROVIDER=akshare`:回测三入口
   (REST 入队 / MCP 同步 / MCP 异步入队)主源对某标的返回空或抛错时,
   在取数入口显式回退备用源(具名 log `fallback.using_fallback`)。默认
   关闭;回退只解决「主源整体不覆盖该标的」,部分区间缺失由第 2 步的
   缓存层策略处理,不在引擎层拼接异源曲线。

端到端回归:`tests/integration/test_etf_bar_chain.py`(mock akshare 同步 →
parquet 缓存 → tushare 源引擎回测拿到 bars 并出成交)。

## 可转债数据链路(#265)

**背景**:可转债双低策略(#63)此前只有策略引擎没有数据上游——转债既无登记
写入者(`discover_a_shares` / ETF / 指数接口都不覆盖转债),也没有条款元数据
(转股价/到期日/评级)和转股溢价率观测。#265 打通「转债登记 → cb_daily 日线
→ cb_basic 条款 → 溢价率冻结发布 → 双低回测」全链路。

**标准运营步骤**(转债 + 正股同链路):

1. `data_sync`(REST `POST /api/data/sync` / MCP `finboard_data_sync_universe`)
   —— `discover_convertibles` 从东财可转债一览 `bond_zh_cov` 登记
   `instrument_type=convertible` 行(11xxxx.SH / 12xxxx.SZ,北交所暂无场内
   转债不纳入)。**已知边界**:东财一览只覆盖当前存续转债,退市转债不在
   列表(存续偏差由第 2 步 cb_basic 摘牌档案缓解);转债无 list_date 上游,
   保持 null 等第 3 步回填。
2. `bulk_download` 带 `instrument_type=convertible`、`source=tushare` ——
   转债日线走 2000 积分档专属接口 `cb_daily` 进 parquet 缓存(无复权概念,
   缓存键沿用默认 `qfq` 但语义为 no-op,发布 adjustment 与下载键一致;
   1 手 = 10 张,vol 换算 ×10)。**tushare 源放行转债**(与 ETF/指数的
   `tushare_scope_mismatch` 边界相反);akshare 源对转债日线 fail-visible
   拒绝(股票接口会把 1 开头误路由,#257 同源缺陷)。正股日线照常同步
   (溢价率计算的另一输入,必须与转债同区间同缓存)。
3. `dataset_sync` 带 `datasets=["convertible_profiles"]`(默认全数据集
   已包含)—— tushare `cb_basic`(在市 L + 摘牌 D 合并)快照 upsert 主数据
   `convertible_metadata`(转股价 `swap_price` / 起息日 / 到期日 / 票面利率;
   `conversion_price NOT NULL`,无转股价的行跳过并计数),顺带回填
   `instruments.list_date/delist_date`(只补 null);评级(akshare
   `bond_zh_cov` 债券评级列)与集思录强赎事件(`bond_cb_redeem_jsl` →
   `instrument_lifecycle_events`,event_type=forced_redemption)走 akshare
   兜底,**失败降级为 warning 不阻断 tushare 主链路**(评级缺失经
   `missing_rating` 计数可见)。
4. `dataset_release_publish`(release_kind=`convertible_metrics`)—— 只接受
   A 股转债标的(非转债 `convertible_scope_violation`);发布执行时从本地
   缓存 bars × 冻结转股价元数据计算转股价值(`100/转股价×正股收盘`)与
   转股溢价率(`转债收盘/转股价值-1`),逐日冻结为带日期观测
   (`available_at` = T 日 15:30 上海,与日线一致);整期无正股同日收盘时
   fail-visible 拒绝(`no_underlying_close_for_premium`),部分缺口计入
   `premium_missing_days` issue 可见。转债 bars 建议与正股/基准同处一份
   `multi_asset_mixed` 发布(mixed 展开已含 convertible),供双低回测同源
   消费;manifest instruments 携带 `convertible` 条款快照(含 observed_at)。
   质量报告 `convertible_instruments` 块:转债标的数 / with_metadata /
   missing_maturity_date / missing_rating / with_lifecycle_events 计数。

**PIT 语义(诚实边界)**:`cb_basic` 是**当前时点**条款快照,不含转股价历史
变动(下修史/除权除息调整史);评级与强赎是快照/当前公告(集思录无历史公告
时间,历史公告回补需 5000 积分的 `cb_call`,后续 issue)。下游派生观测
(转股溢价率)只能宣称「冻结快照转股价 × 同日正股收盘」的带日期冻结语义,
**不得宣称全历史 PIT**。未来生效的强赎事件按生效日可见(领域不变量
`available_at >= effective_date` 开盘,保守方向),真实观察时间保留在
`details.observed_at`。

**策略消费**:`convertible_double_low` 用真实 `FrozenReleaseProvider` 读
mixed bars 发布(收盘/开盘/成交额 + manifest 条款)+ convertible_metrics
发布(`fetch_convertible_metrics`,PIT 门控)构建逐日快照后回测;已同步的
强赎事件经 `filter_event_risk` 按 `available_at` 门控参与事件风险过滤
(#63 既有语义)。已知缺口(本 issue 不修):通用事件驱动回测引擎
(`BacktestEngine`)的默认 resolver 把一切代码按 A 股股票撮合(engine.py 不传
resolver),转债走通用引擎需 resolver 注入;`convertible_double_low` 独立
模拟器路径不受影响。

验证 SQL:

```sql
-- 转债登记与条款元数据
SELECT code, name, list_date FROM instruments WHERE instrument_type = 'convertible';
SELECT code, conversion_price, maturity_date, rating FROM convertible_metadata;
-- 强赎事件
SELECT symbol, effective_date, available_at FROM instrument_lifecycle_events
WHERE event_type = 'forced_redemption' ORDER BY effective_date;
```

端到端回归:`tests/integration/test_convertible_chain.py`(mock bond_zh_cov /
cb_daily / cb_basic → 登记 → 缓存 → convertible_profiles 同步 → 双发布 →
双低回测出非空成交)。

## 期货 EOD 数据链路(#267)

**背景**:路线 C(市场中性对冲:股票多头 + 股指空头)的数据面前置。此前
期货既无登记写入者也无行情接入(akshare 全市场列表接口不覆盖期货,tushare
`fut_daily` 属另档积分)。#267 打通「主连登记 → 新浪主连日线 → 冻结发布 →
研究数据可读」全链路。**范围只做数据面**:对冲组合回测工程(换月展期 /
贴水成本 / 保证金占用)另行立项;期货不可撮合,通用回测引擎不做期货撮合
(`asset_rules.py` docstring 明示)。

**主连 vs 具体合约(核心语义,不混淆)**:

- **主连**(品种+`0`,如 `IF0.CFFEX`):换月拼接的连续序列,**仅用于研究
  信号 / 基准数据,不可当作可成交合约**。v1 只登记 / 只缓存主连。
- **具体合约**(如 `IF2406.CFFEX`):不进逐标的缓存(无结构化合约链上游,
  合约链另行立项);EOD 按日全市场表可经 `fetch_futures_official_daily`
  读取(交易所官网 `get_futures_daily`,v1 仅供研究脚本直读)。非主连代码
  在缓存层 fail-visible 拒绝,防止两种语义的数据混进同一条权益曲线。

**标准运营步骤**:

1. `data_sync`(REST `POST /api/data/sync` / MCP `finboard_data_sync_universe`)
   —— `discover_futures_main` 从受控登记表 `FUTURES_MAIN_SERIES_REGISTRY`
   登记 IF/IH/IC/IM 主连(`market=future` / `instrument_type=futures`,
   CFFEX;乘数 / 保证金率与 `FuturesRule` 同口径)。扩展新品种直接在登记表
   加一行;未登记品种 fail-closed 拒绝。主连无 list_date 上游,保持 null
   可见缺失(主连是连续序列,不是单一上市合约)。
2. `bulk_download` 带 `instrument_type=future`、`source=akshare` —— 主连
   日线走新浪 `futures_main_sina` 进 parquet 缓存(无复权概念,缓存键沿用
   默认 `qfq` 但语义为 no-op,发布 adjustment 与下载键一致;新浪无成交额
   列 amount=0)。**tushare 源对期货拒绝**(fut_daily 属另档积分,具名
   提示另建 akshare 任务,不静默换源)。
3. `dataset_release_publish` —— 期货 bars 建议与股票 / 债券基准同处一份
   `multi_asset_mixed` 发布(mixed 展开含 futures 五类之一),或独立 BARS
   发布(source=akshare)。发布候选从登记表读取乘数 / 保证金率 / 最小变动
   价位 / `allows_short`;质量报告 `futures_instruments` 块:期货标的数 /
   continuous / missing_list_date / with_lifecycle_events 计数。期货事件
   硬门降级(#58 换月 / 到期事件在主连日线上无结构化上游,同 #265 转债
   决策),已同步事件仍随 manifest 冻结。
4. 消费 —— 研究运行 / 回测把期货主连当**基准数据**用(`benchmark_config`
   / 研究发布引用):`is_benchmark_only_instrument` 扩为 index + futures,
   静态预检与运行时候选一致排除(不进候选池、不撮合)。缓存 `make_symbol`
   已支持期货后缀 → `Market.FUTURE`(此前未知后缀兜底 A_SHARE 会让冻结
   发布 market 校验误拒)。

验证 SQL:

```sql
-- 期货主连登记
SELECT code, name, exchange FROM instruments WHERE instrument_type = 'futures';
```

端到端回归:`tests/integration/test_futures_chain.py`(受控登记 → mock
futures_main_sina → 缓存 → BARS 发布 → 真实 FrozenReleaseProvider 读回;
主连 / 合约语义守卫)。
