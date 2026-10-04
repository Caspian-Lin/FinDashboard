# 小盘复合 v13 可信度复核（2026-10-04，#505）
结论为 **not_supported（当前目标）＋ insufficient_evidence（独立 alpha / 正式 OOS / 完整执行稳健性）**。原历史年化不能证明可晋级；本轮停止，不追加调优，不启动模拟、影子或实盘。

## 目标、来源与预算

原始日志、查询快照与失败记录已完整移至仓库外本地存档，见[证据索引](evidence-index.md)。本报告与精简证据保留在 PR 中；索引中的原始文件仅本机可用。

先读 canonical ROADMAP/FINDINGS 与 [活动记忆摘录](evidence-index.md#active-memories-json)。ROADMAP 2026-09-01 目标为年化 ≥15%、最大回撤 ≤20%；`RM-a14a4b147ccd4afb9501980a` 与冻结 v13 说明的 2026-09-22 当次门为年化 ≥15%、回撤 <10%。两目标来源分别保留，未自主改变。原历史回撤 20.25%，两门均未通过。

本轮只登记 12 个不同研究假设：7 个基线/消融和 5 个费用/资金档。每次自动重试上限 3；延迟 2 Bar 只记 unsupported，不生成运行。另预登记 2 个同参数工程复核用于核验窗口统计修复，未增加候选、参数搜索或研究阈值。[预注册](preregistration.json)、[原入队参数](evidence-index.md#queued-json)、[压力入队](evidence-index.md#stress-queue-json)、[工程复核登记](window-repair-registration.json)保留。

## 冻结输入与实际规则

原运行 `RR-e909662f7b2c9f7ad30e89c4`，策略 `sc-mr-macd-composite-v1` v13；manifest checksum `5ff2882c9a52ca992738862b7d73f75cf8fc481410ff6beede83d55e02dfc3d4`，result checksum `8f174b280e23c4f379d7a02c1c839372bd870e2f87e1aea5254314e8faf5685f`。[冻结原件](evidence-index.md#baseline-json)、[同源规则说明](evidence-index.md#rules-json)为解释依据。

| 项目 | 冻结 / 生效口径 |
|---|---|
| bars 主发布 | `a-share-cs-20260917-v5`；5215 股票＋9 指数＋4 期货；指数/主连仅基准，候选/特征/排名同口径排除 |
| 研究发布 | `a-share-daily-metrics-20260829-v3`；研究观测不替代主 bars |
| 四腿 | ret5 .30、RSI14 .15、bias20 .40、MACD .15；四腿均 negate→截面 rank，原值越低分数越高 |
| 候选池 | A 股 equity；市值 ≤50亿，价格 ≥2，排 ST/退市；市值底部200；上市天数下限0、完整率下限.5；exclude_suspended=false，不宣称主动候选停牌过滤 |
| 信号与组合 | top15%买、bottom50%卖；等权、最多20只、单只≤.06、gross .95、现金≥.05、再平衡带.03、流动性参与.1、目标换手约束.25 |
| 实际组合覆盖 | `max_risk_contribution=1`，保留源覆盖，未用默认.35替换；协方差/硬约束仍 fail-closed；源预检见 preflight.json |
| 风险退出 | DD .08 降 gross 至.30，冷却14天；其他风险退出关闭；no-overlay仅独立研究对照关闭这两项 |
| 撮合 | next_open、v2、参与率.1、手数/价格限制、拒同 Bar 成交；未模拟订单簿/冲击/实际券商排队 |
| 费用版本 | 源未带 explicit_overrides_v1，资产级默认优先，spec min5不能直接证明源逐笔min5；新对照显式覆盖佣金.0003/min5/卖税.0005/滑点5bps（slip在源spec继承），有费用实际账本 |
| 原时间轴 | bars 2015-01-05..2026-09-04；决策 2015-02-06..2026-08-14，553次；原曲线含预决策现金及最后决策后估值，原manifest无research_window |
| 本轮时间轴 | 冻结预热从2015-01-05可见，决策仅2024原日历47次（最后12-27），下一成交日，估值止12-31；绩效仅2024，30万现金重新起步，属于已用窗回顾 |

四腿的实现 commit 与冻结锚匹配；不是从名称猜方向。[因子覆盖](evidence-index.md#factor-coverage-json)有逐日期与 checksum：ret5/RSI/bias 的全发布质量 passed=false，失败来自2015-01-05预热首日100% NaN；MACD passed=true；总体覆盖约74.63%–75.34%。正式决策先核验所需日期覆盖，不把全域质量失败抹成通过；日期有记录也不证明候选每只都有有效观测。四腿是价格因子，声明附带 bluechip48 研究发布不等于本策略市值源；精确依赖见冻结引用。因子序列结束2026-08-31，主bars到09-04，不能据此自动延长决策或生成新OOS。

主发布 quality_status=warnings：2850 个 short_history、226274 停牌会话，发布缺失会话计数0。元数据缺失集中含13个基准标的，源申明 qfq/PIT，不代表每个研究输入均经独立外部审计。[发布差异](evidence-index.md#release-diff-json)保留全部质量警告；[运行警告查询](evidence-index.md#warning-counts-json)为空，不能用空查询证明发布无警告。rolling IC/行业市值中性 alpha 未支持，本轮未重跑已证伪价格动量课题。

声明规则不等于实际过滤已生效：[执行端具名警告](producer-warning-evidence.json)显示 exclude_delisted 因 delist_date 全空退化为不生效；ST 判定在部分日期缺 name_history 覆盖时回退当前名称（2024-09-13 为271/5215）。这带来历史候选/PIT不确定性，缺失会话计数0不能消除该风险。未改源规则或静默修补原产物；不能将本轮标作完整PIT/生存偏差审计通过。

## v4 → v5 不可归因

共同5215股票仅383个 parquet manifest checksum相同，4832个不同；另新增13个基准标的。名称历史5215、生命周期3914、预期会话2947等元数据也变动，3个标的行数变动。两者字段/复权/PIT申明相同不足以证明同源；未另对全文件做独立重哈希。旧v12与旧2×成本运行同时有数据/因子/日历/代码等差异，旧成本还改变策略规格和滑点。[历史对照](evidence-index.md#comparison-history-json)只作混源背景，不作为v13改进或纯费用因果证据。

## 原曲线确定性统计

机器数值与运行校验锚另见[精简证据](evidence-summary.json)。所有数值来自服务端有界 SQL 诊断；年化252交易日，Sharpe rf=0/ddof=1，回撤为正数。区间首日保留前一估值锚，不重复计初始资本；边界和定义在每份JSON中。

| 年份 | 净收益 | 最大回撤 |
|---|---:|---:|
| 2015 | 234.19% | 20.11% |
| 2016 | 29.42% | 14.70% |
| 2017 | -9.62% | 12.93% |
| 2018 | -3.30% | 9.28% |
| 2019 | 7.05% | 9.87% |
| 2020 | -3.40% | 10.63% |
| 2021 | 5.37% | 5.63% |
| 2022 | -2.78% | 11.11% |
| 2023 | 25.44% | 6.47% |
| 2024 | 9.79% | 18.70% |
| 2025 | 25.80% | 14.08% |
| 2026 | 1.03% | 16.91% |

2026年仅至09-04，非全年。年度回撤各自重新计算，不能简单拼成总体回撤。

| 区间（已使用） | 净收益 | 年化252 | 最大回撤 | 来源 |
|---|---:|---:|---:|---|
| 2015-01-05..2026-09-04 | 600.91% | 18.88% | 20.25% | [annual](evidence-index.md#annual-json) |
| 2015-01-05..2023-12-29 | 402.31% | 20.43% | 20.25% | [development](evidence-index.md#development-json) |
| 2024-01-02..2026-09-04 | 39.54% | 13.81% | 18.70% | [subsequent-used](evidence-index.md#subsequent-used-json) |
| 2016-01-04..2026-09-04 | 109.73% | 7.46% | 20.25% | [exclude-2015](evidence-index.md#exclude-2015-json) |
| 2024-01-02..2024-03-29 | -8.88% | -33.24% | 18.70% | [2024-risk](evidence-index.md#2024-risk-json) |

剔除2015后年化7.46%，说明异常年份敏感性；这是一份既有曲线的切片，不重新建立持仓/风险状态，不是反事实重跑，更不是事后选择的新OOS。2024风险段仅Q1，年化是短区间换算值；不能声称之后没有风险。

## 同源2024回顾矩阵

全部12个原运行 completed，保存所有输入/checksum和实际账本。对照元数据的 controlled_difference 只表示申明的输入差异受控，不自动证明运行时版本或因果。[运行源](evidence-index.md#executed-sources-json)、[输入对照](evidence-index.md#executed-comparisons-json)和每个 result JSON完整保留。

| 对照 | run | 净收益 | 最大回撤 | 成交笔数 | 实际佣金 / 税 / 滑点（元） | 总成交额÷起始资金 | shortfall / 请求额 |
|---|---|---:|---:|---:|---|---:|---|
| baseline | `RR-062582fd19f3341868a256b8` | -16.03% | 28.59% | 982 | 4914.64/817.76/1668.10 | 11.121 | 0.00/3330534.70 (0.00%) |
| ret5-only | `RR-d37f292b1fa17c30ac390765` | -18.28% | 28.92% | 1164 | 5825.28/855.14/1751.92 | 11.679 | 0.00/3498475.19 (0.00%) |
| rsi-only | `RR-99c878bdbdcd052918f69327` | -20.98% | 30.65% | 1217 | 6092.25/946.43/1943.42 | 12.956 | 0.00/3881225.08 (0.00%) |
| bias-only | `RR-b8c210685ded4c80988eeb26` | -15.92% | 30.10% | 979 | 4900.11/791.78/1617.91 | 10.786 | 0.00/3231532.33 (0.00%) |
| macd-only | `RR-35bc908879afc136e0861b22` | 5.33% | 18.16% | 1062 | 5339.54/1226.49/2470.83 | 16.472 | 36581.84/4970920.00 (0.74%) |
| macd-positive | `RR-3351b8ef0caa1e94f3104b4d` | -17.99% | 28.95% | 919 | 4598.67/799.40/1629.53 | 10.864 | 0.00/3252712.61 (0.00%) |
| no-overlay | `RR-77da98da90a60aedfb43c5c8` | 21.60% | 31.45% | 956 | 4861.52/1926.68/3838.36 | 25.589 | 109132.99/7760354.36 (1.41%) |
| cost_x2 | `RR-88cd075891b02608b9ae6a9e` | -18.22% | 28.94% | 978 | 9789.29/1614.04/1645.26 | 10.968 | 0.00/3285166.20 (0.00%) |
| slippage_10bps | `RR-207902172adebe8e765c6e3c` | -17.15% | 29.20% | 979 | 4899.64/811.06/3305.24 | 11.017 | 0.00/3299921.59 (0.00%) |
| slippage_20bps | `RR-3fcb939bd03edcb270e7bc82` | -17.32% | 28.90% | 996 | 4984.64/818.87/6660.54 | 11.101 | 0.00/3324825.00 (0.00%) |
| capital_100000 | `RR-03fdb4fad15d18ff97bbd8c5` | -19.32% | 24.24% | 664 | 3320.00/231.07/473.35 | 9.467 | 0.00/945206.42 (0.00%) |
| capital_500000 | `RR-5f04407d3bd134a153eacf4e` | -16.21% | 29.84% | 1136 | 5803.41/1376.57/2815.35 | 11.261 | 0.00/5621825.85 (0.00%) |

总成交额÷起始资金为双边累计交易强度，不等于单次目标换手约束，也不等于权益归一化年换手。shortfall分母为同决策 rebalance 指令绝对 estimated_value（含拒单/部分成交），轴是decision business_date；费用按实际filled_at汇总，两种计数时间轴不同。MACD单腿和关闭overlay对照存在非零shortfall，其余本轮对照为0；不能据此推出无限容量或无真实成交残差。原因计数见各result。

单腿表现都只针对预登记2024窗。MACD负向单腿约+5.33%/18.16%回撤，复合与翻正仍亏损；关闭overlay收益较高但DD约31.45%。这只支持该窗中配置对收益/风险有影响，不支持换成MACD单腿或关风控能达成长期目标。成本2×净收益更差；资金档/滑点档非线性，受离散手数与风险退出状态影响，不假定单调。

原历史2024的+9.79%与本轮reset基线-16.03%不可直接归因：起始资本/持仓/风险状态和费用版本改变。新压力只相对本轮cost1/reset基线，不代表完整2015–2026费用稳健性。

## 尝试、实现修复与复现边界

[任务尝试账本](evidence-index.md#attempt-ledger-json)：12个任务终态成功，累计attempt=13；基线BJ-A288207BCFE54BEF attempt=2，第一次发生stall watchdog收敛后重试成功。该次即时状态摘要未独立留原始stdout，当前DB只证明attempt=2，不伪装成完整故障日志。第一次执行中的两项窗口统计包含窗口外估值/基准，原record不改写；上表使用各份服务端2024选定区间诊断，原错误native总体年化/benchmark不参与比较。

修复后另做同参数工程复核；[冻结参数与结果](evidence-index.md#window-repair-results-json)，[任务次数](evidence-index.md#window-repair-jobs-json)，代码哈希在window-repair-registration.json。
- baseline：原 `RR-062582fd19f3341868a256b8` → `RR-f7f228441567616b1e9178bd`；2024选定窗口字段一致性 `{"net_return": true, "max_drawdown": true, "final_equity": true, "fills": true, "shortfall": true}`。
- ret5-only：原 `RR-d37f292b1fa17c30ac390765` → `RR-239bd09e45b5ef0c0e70e75a`；2024选定窗口字段一致性 `{"net_return": true, "max_drawdown": true, "final_equity": true, "fills": true, "shortfall": true}`。
上面一致性限于净收益/回撤/期末权益/费用成交/shortfall；原记录区间锚为2023-12-29现金点，新物理窗口首锚为2024-01-02现金点，因此收益观测数相差1，年化/Sharpe有小幅边界差异，未声称所有统计字段逐位相同。新基线年化-16.697%/Sharpe-1.10644，旧区间诊断年化-16.634%/Sharpe-1.10415；两者均不达目标。新native指标与新诊断同口径，原native全发布指标不用于该窗结论；见window-repair-native.json。

全部原12个研究运行会计不变量 passed；窗口物理隔离/冻结/benchmark-only与正式组合硬门由现有正式管线及软件测试覆盖，历史元数据退化仍缺证据，不能冒充完整PIT或外部逐笔行情审计。[当前管线代码哈希](reproduction-code.json)是开发分支复现证据，`review505-v1-fee-policy-v1`为版本标签，不是部署不可变GitSHA。中途修复窗口统计导致最早两项运行时代码不同，已保留并限定其证据用途。

复现：仓库根目录 `uv run python -m scripts.review_smallcap_505`（只读快照），`uv run python -m scripts.archive_smallcap_review_505`（只读归档），`uv run python -m scripts.render_smallcap_review_505`（只格式化本地证据）。`execute_smallcap_review_505`/`replay_smallcap_window_505`会入队研究；保留预登记幂等键，不为得到更好结果改键重跑。

## 分项证据门与停止结论

| 命题 | 结论 | 依据 / 未覆盖 |
|---|---|---|
| 当前预注册目标已通过 | not_supported | 源总体DD20.25%，两来源门均失败；本轮2024基线更未过门 |
| v13相对v12的改善来自策略 | insufficient_evidence | 混源、字段/校验/因子/代码改变 |
| 2015异常年份不影响可信度 | not_supported | 剔2015年化仅7.46%，分段为敏感性，不是新OOS |
| 单腿/方向/overlay在2024有绩效影响 | supported（仅该窗配置敏感性） | 有真实同发布对照；不能扩大为独立收益贡献或长期最优配置 |
| 四腿提供独立alpha | insufficient_evidence | 中性化/rolling IC未实现；固定微盘暴露未剥离 |
| 完整执行稳健性通过 | insufficient_evidence | 成本/滑点/资金真实运行有负值；2Bar延迟/订单簿冲击/盈亏平衡/真实残差无证据 |
| 当前正式规格OOS获支持 | insufficient_evidence | bc9fc5af85e84173 是ma_cross载体且not_supported/已揭盲，不能代替正式规格 |
| 对手盘身份已确定 | insufficient_evidence | 日线不能识别散户/机构/被动资金；只能假设 |
| 可以晋级模拟/影子/实盘 | not_supported | 上述门未通过，不启动任何晋级 |

中证1000是同主发布冻结基准，新窗口native基准约+1.76%；该宽小盘基准不等于200只微盘风格匹配。缺冻结PIT历史成分，不能拿当前微盘指数成分回填历史或宣称已消除市值暴露。

完整2015–2026已用于开发/评估，包括所谓后续2024–2026；这些窗全部登记为已用。新正式research_spec入口已复用同管线，但本课题没有未用的发布/因子覆盖。不重新揭盲原实验，不启动替代ma_cross实验。后续必须另登记新假设/预算或合格未用窗口。

本轮提供phase1_doc.md §3.4 3–14的离线决策/成交/账本/恢复等价证据，不声称完成QMT/真实券商/小资金验收。canonical结论经本PR评审，工作课题摘要/agent解释/自动事实分层保留，归档ID见workspace-archive.json。
