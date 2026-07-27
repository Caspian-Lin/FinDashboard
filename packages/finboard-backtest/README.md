# finboard-backtest

事件驱动历史回放、纸面撮合、绩效分析、按日因子选股与策略层 Bar 规则选股。

## 因子快照时序

`BacktestConfig.selection` 默认 `enabled=false`,因此旧回测继续把静态
`symbols` 全部交给策略。启用后,引擎按交易日批处理:

1. T 日所有标的行情先统一更新,消除输入标的顺序造成的横截面偏差。
2. T 日上海时间 17:00 仅使用 `available_at <= decision_at` 的已发布研究数据,
   计算并冻结因子值、全局/行业排名和候选池。
3. 快照从下一实际回放交易日生效,并通过 `UniverseSelectionEvent` 交给策略。
4. 数据缺失、陈旧、来源混入或行业覆盖不完整时整次调仓标记为 `skipped`;
   不使用部分横截面,并保留上一有效候选池。

首批 `factor_version=v1` 支持总市值、PB、换手率、动量、ROE、毛利率和营收同比。
输出始终是静态 `symbols` 的子集。候选池只限制策略可见行情,不产生订单,也不访问
券商、持仓或风控组件。

`BacktestResult` 会归档 `selection_snapshots`、`dataset_versions` 和
`factor_version`,用于复现与审计。需要回滚时关闭 `selection.enabled` 即可恢复
旧行为。

## 策略层 Bar 规则选股 (`bar_universe`)

`BarUniverseSelector` 是不依赖因子系统的研究级选股器,只消费策略已经收到的
当前/历史 Bar,不需要任何 I/O 或研究数据。**默认 `mode="all"` 不改变现有策略
行为**;`mode="liquidity_momentum"` 启用后,会在指定回溯窗口内按以下规则筛选:

* **预热**:`lookback` 根 Bar 填满前标的恒不入选,避免读取未来数据;
* **平均成交额**:窗口内平均成交额 ≥ `min_avg_amount`(留空表示不校验);
  成交额缺失或为 0 时回退为 `close × volume`;
* **区间动量**:`close[-1] / close[0] - 1 ≥ min_momentum`(留空表示不校验);
* **退出清仓**:策略可在 `universe_exit_clear=True` 时通过 `ctx.submit_order`
  对已有内部持仓发出卖出意图,卖出仍走完整 Risk → OrderManager → Broker 链路。

约束(issue #43):

* 只能缩小回测请求的静态候选池,不能添加新标的或触发数据下载;
* 多标的状态相互隔离,异常 Bar(非正价格、负成交量/成交额)安全不入选;
* 不实现行业、市值、基本面和跨截面排名 —— 这些能力由 `selection` 模块的
  因子快照提供,需要独立的数据契约。

参数通过策略 `MaCrossParams` 暴露,前端表单由 `GET /api/backtest/strategies`
返回的 schema 自动生成,预设保存走 `strategy_presets`。扩展新规则时:

1. 在 `BarUniverseMode` 添加新模式;
2. 在 `BarUniverseSelector.update` 增加分支判定;
3. 在策略 schema(`MaCrossParams` 或新策略)以 Pydantic Field 暴露参数;
4. 不需要修改前端 —— schema-driven 表单会自动渲染新字段。

