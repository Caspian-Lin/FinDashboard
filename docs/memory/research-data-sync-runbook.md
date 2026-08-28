# research_data_sync 同步运营 runbook(issue #212)

**主题**:tushare 研究数据(daily_metrics / financial_indicators / profiles / industry_memberships)的首次全量与日常增量同步——命令、吞吐、配额纪律、PIT 锚点、发布衔接。

**结论 / 事实**(2026-08-28 本机实测):

- 入队:`POST /api/jobs`,body `{"kind": "research_data_sync", "payload": {"datasets": [...], "start_date": "...", "end_date": "...", "symbols": [...]}}`;datasets 白名单 `profiles / daily_metrics / financial_indicators / industry_memberships`;**symbols 省略时逐标的接口自动以 profiles 全市场为池**。进度看 `GET /api/jobs/{id}`(phase 形如 `research_data_sync:daily:2024-04-17`)。
- 切片粒度:daily_metrics **逐交易日截面且恒为全市场**(与 symbols 参数无关,单日 ~5500 行);fina_indicator / industry_memberships 逐标的。每片 = 1 次 tushare 调用。
- 实测吞吐 ~5 秒/片(API 延迟 + 批量写库为主,200 RPM 限速不是瓶颈)→ 首次全量(2015 起 daily ~2800 片 + 逐标的 ~5400×2)wall-clock 以**小时**计(约 6-10h),宜过夜跑;日常增量每日只差 1 片 + 当日新公告,秒级。
- 断点续跑:切片 dataset_version 确定性(`daily:{date}` / `financial:{symbol}:{start}:{end}` / `industry:{symbol}` / `profiles:{today}`),已发布切片**写库层**跳过(upsert 幂等,数据无害)。**已知限制(2026-08-28)**:跳过判断在 provider 拉取之后——重跑仍会重新调用 tushare(费时不费数据);过夜全量任务失败重试会重复拉取已完成部分,改善方向是把「已发布版本查询」提前到 fetch 之前(暂未实施,重跑验证用窄范围做)。
- 配额纪律(重要):tushare_budget 是**进程内**限速锁,MCP server 与 worker 各自计数;大同步只跑 worker 单进程,勿同时段经 MCP `finboard_data_*` 并发拉 tushare;usage 文件 `data_cache/tushare_usage.json` 跨进程读改写有竞窗(计数偏保守方向,安全)。
- 截断红线:daily_basic 单次上限 6000 行,全市场 ~5547 已接近——股票总数逼近 6000 时 provider 会拒整批而非静默截断,需监控;fina_indicator 单次上限 100 行,报告期范围控制在约 9 年内(过长含修订可能触截断)。
- PIT 锚点(勿改,有回归测试锁边界):fina_indicator `available_at = ann_date(公告日)+1 天 00:00 上海时区`;daily_metrics `available_at = 交易日 17:00`。回归测试:`tests/unit/data/test_research_release.py::test_financial_indicators_pit_gate_hides_unannounced_reports`。
- 同步完成后的衔接:发布(`POST /api/instruments/datasets/releases`,release_kind=`daily_metrics` / `financial_indicators`)→ 与 bars 主发布联合做因子快照(激活 pb / earnings_yield / dividend_yield / turnover_rate / roe / gross_profit_margin / debt_to_assets / revenue_yoy 8 个 alpha 因子)→ `strategy_validate` 的 universe_precheck 复查字段观测。

**Why**:同步是运营动作而非开发动作(#212 核实:代码链路完整,缺的只是执行);吞吐、配额纪律、切片语义这些数字不写下来,每次都要重新踩坑重测。

**How to apply**:首次全量过夜跑(单 job、不指定 symbols);日常增量每日盘后一片;怀疑数据缺失先查 job phase 与 research_* 表行数,重跑幂等无害;任何涉及 available_at 锚点的改动先跑上述回归测试。
