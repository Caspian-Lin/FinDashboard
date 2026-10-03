# FinDashboard 研究执行误差与数据粒度

核查日期：2026-10-03。本文说明当前近似与证据边界，不授权 P6 复杂回测、订单簿重放或实盘开发。相关：#497、#502、#503、#505；分钟/tick 讨论 #220 不排期。

## 哪些误差需要分别验证

| 层次 | 当前证据限制 | 更细数据能改善什么 | 仍然不能据此确定什么 |
|---|---|---|---|
| 价格与日内路径 | 日 Bar 不含开盘后的路径与价位持续时间 | 分钟 Bar 提供分段路径，快照/逐笔提高时间分辨率 | 模拟订单提交时一定能按记录价格成交 |
| 成交判定与排队 | 开盘/收盘价基加限制是撮合近似 | 盘口深度、成交与委托序列支持更细的成交估计 | 模拟单在历史队列中的准确位置及优先级 |
| 自身冲击 | 历史市场未包含模拟订单 | 历史订单簿可辅助校准冲击假设 | 加入模拟单后的反事实市场路径 |
| 延迟与部分成交 | Bar 级时点不能描述网络/券商/撤单竞争 | 带时间戳的事件与实际执行日志可量测分布 | 单靠历史市场数据复原真实端到端延迟 |
| 样本不确定性 | 多重尝试、样本选择和环境变化影响收益估计 | 更多独立样本、预注册、同策略 OOS 提供统计证据 | 更细粒度本身消除过拟合或保证未来盈利 |

前四层属于执行与可观测性，最后一层属于统计推断。tick 不能保证零误差，也不能据此推出日频回测已可信。

## 当前成交与费用口径

代码来源：`packages/finboard-backtest/src/finboard_backtest/strategy_spec/contracts.py` 的 `ExecutionModel`；正式研究消费端 `research_run/portfolio_pipeline.py`、`frozen_loader.py`、`config_overrides.py`；产品模拟盘 `packages/finboard-simulation/src/finboard_simulation/matching.py`。

默认 next_open，可选 next_close，拒绝同 Bar 成交。研究默认佣金率 0.0003、最低佣金 5 元、卖出税率 0.0005、滑点 5 bps、成交量参与率 0.1，并启用手数与涨跌停约束。默认值是研究假设，不能作为所有资产、所有历史税费时段的真实规则。

正式 long-only ResearchRun 在 `execution_prices` 上记录成交价基，另扣滑点费用。产品模拟盘通过 `price_with_slippage` 调整价格并取整至价位，二者属于同类近似，但逐字段与账务实现并不完全相同。两套模拟的一致性不能量测真实交易执行残差。

各资产的手数、价位、交易限制由冻结标的规则及消费端提供。股票 daily amount 为千元；期货 fut_daily amount 万元转元、vol 手转张；可转债 vol 手转张乘10。不能把不同原始单位直接用于容量比较，也不能把股票卖出税默认套给期货。期货主连与指数在当前正式管线中为 benchmark-only，不可撮合。

#482 后 `fee_config.overrides` 合并到冻结 `strategy_spec.execution_model`，合法键为 commission_rate、minimum_commission、sell_tax_rate、slippage_bps；未覆盖字段继承规格。`portfolio_config` 是组合约束，`risk_config` 是退出规则，两者不能混用。`execution_config` 与 `validation_config` 非空覆盖入队即拒。merge 仅发生在消费端，历史 manifest/checksum 不修改。接线前的旧运行如有费用覆盖，不能仅用今天的派生摘要断言当时已经生效，须核对代码版本与费用账本。

## 压力能力与执行证据分开登记

| 能力 | 现有配置 | 当前默认执行器 | 实际证据 / 缺口 |
|---|---|---|---|
| 基线研究成交与费用 | ExecutionModel 与合法 fee overrides | 正式 PortfolioPipelineAdapter 已消费 | 每个运行的成交、费用、账本及会计校验；需逐运行核对 |
| 成本倍率 | ValidationPlanSpec 默认 1、2；RobustnessPlan 默认 1、2、3 | robustness runner 不自动执行成本重跑；默认 trial runner 丢弃 config_overrides | 人工2倍运行见下；自动压力能力由 #503 补齐 |
| 滑点梯度 | RobustnessPlan 默认 0、5、10、20 bps | 未自动按梯度重跑 | 不把配置数组或实验完成状态当成压力通过，#503 |
| 成交延迟 | RobustnessPlan 默认 1、2 Bar | 默认 robustness 未执行真实成交延迟 | unsupported；移动决策日不能代替成交延迟，#503 |
| 市场阶段 / 邻域 | 已有阶段和邻域规则 | runner 可执行阶段检验与最差WF邻域近似 | 近似结果不等于成本/延迟探针 |
| 正式多期规格 OOS | ResearchRun 与验证实验分别存在 | 默认 validation runner 用注册策略 BacktestEngine，未直接复用冻结多期规格 | 同名或替代策略不能代验正式规格，#502 |
| 精确订单簿队列 / 自身冲击 | 无 | unsupported | P6 边界与 #220，不提前开发 |

核查点：`validation/contracts.py`、`validation/runner.py::_run_robustness_probes`、`background_jobs/executors/validation_experiment.py::default_trial_runner_factory`（内部 `del config_overrides`）。实验 `status=validated_oos` 表示流程状态，先读 `oos_outcome` 判断 supported / not_supported / inconclusive。

2026-10-02 API 审查记录：v12 基线 RR-e1976d09cb26576d4cd9fb9f，年化24.39%、最大回撤31.95%；同 v4 bars（a-share-cs-20260908-v4）与 daily_metrics 发布的 RR-a2f0ad6f035d7eada1538227，规格佣金率/最低佣金/卖出税/滑点同时加倍，年化17.79%、回撤32.17%。这是一个人工组合压力对照，不是仅佣金变化的单变量实验，更不是默认执行器自动完成的证明。v13 RR-e909662f7b2c9f7ad30e89c4 使用 v5 bars，不能直接移用此对照为其压力证据。本文没有启动新运行或认定策略可信。

## 分钟、tick 与“逐笔”分别是什么

- Bar：一段时间的 OHLC 与成交量汇总；不保留区间内事件顺序。
- tick 快照：定时或事件触发的价格/盘口截面；采样之间可能发生多笔成交与撤单。
- 逐笔成交：成交事件；通常不单独提供完整委托队列。
- 逐笔委托：订单新增、撤单等事件；是否足以重建簿取决于序号、字段、覆盖与丢包处理。
- 订单簿：某时点盘口或由事件重建的状态；历史簿仍未包含模拟订单。

选择数据源必须核查字段、精度、完整性、修订、时间戳与交易日定义，不能凭文件名“tick”判断可回放逐笔。

中金所[公开日行情](https://www.cffex.com.cn/en_new/DailyData.html)提供开高低收、成交量/金额、持仓量与结算等日汇总；不是完整历史逐笔接口。[中金所行情授权说明](https://www.cffex.com.cn/u/cms/www/202201/20211342wucd.pdf)区分授权行情，Level-2 五档采用行情快照。2026-10-03 搜索索引可核查文档内容，PDF 直连工具超时，不能据此声称下载已成功或授权价格已核实。作为另一类市场的例子，[上证所历史行情产品](https://www.sseinfo.com/services/assortment/historical/)分别列出快照、逐笔成交、日K与分钟K，说明这些是不同数据产品。官网可爬到日统计不等于免费获得全历史逐笔；实时采样也不能补回过去全部事件。

以下仅为未压缩数值列的假设量级，不是购买报价或实际磁盘容量：5000只股票、每年250个交易日、每日240个一分钟 Bar，每行80字节，约24 GB/年；假设每3秒一幅快照、每日4小时、每幅200字节，约1.2 TB/年。真实体积受列类型、标的覆盖、活跃度、压缩、索引和去重影响。逐笔量取决于事件数量，不能由采样频率代算。期货各品种有不同交易时段及夜盘，不能套A股240分钟；应按具体合约与时段重新估算。

## 逐策略判断需要哪些证据

先核对冻结数据、费用生效值、周转率与成交缺口，再预注册有限成本/滑点/延迟和资金档位对照。日报参与率上限不能证明开盘价位提供了相应流动性。累计 `fill_shortfall` 是未成交金额口径，不是直接的策略亏损；判断容量需分母、成交限制和资金规模证据。

决策频率低不自动意味着换手低。当前小盘周频反转须量测换手、微盘暴露、费用、2015样本敏感性及同源消融，不直接贴“低换手”标签。回撤规则仅在决策时点评估，并在后续允许的交易时点执行；8%触发不保证8%最大回撤。

[短期反转与流动性研究](https://www.nber.org/papers/w30917)可支持提出机制假设，但其研究样本不验证本项目A股策略，也不识别某笔交易对手身份。替代解释包括微盘风险、样本偏差、过拟合及执行近似。核心问题是净收益是否在合理执行假设及资金规模内存活，结论可为未获支持或证据不足。

后续 #502/#503/#505 收集同策略 OOS 与压力证据，按研究→回测→OOS→模拟→影子→小资金晋级。纸面模拟不是实盘残差量测。本文对应 phase1_doc.md §3.4 的3–10及14项在研究域的决策/成交/费用/账本解释，不替代券商验收，不改变晋级门或实盘红线。
