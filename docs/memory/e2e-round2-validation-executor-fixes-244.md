# 第二轮 L3 E2E:验证实验三项阻断修复(#244)

- 主题:第二轮 L3 E2E(#219 首次晋级实战)暴露的 #233 验证实验执行三缺陷——根因、修复口径与测试坑
- 日期:2026-08-31 登记 issue #244,同日实现(#245 为 screen 门方向开放问题,登记待决策)
- 状态:修复已实现,PR 待合并

## 结论 / 事实

第二轮 E2E 走通了首次晋级 4/5 段(submit→RCR 快照→screen RR→promote fail-closed 具名拒绝),
但 #233 walk-forward 执行被新 bug 精确阻断,4 个实验(含单 trial / 单参数空间)全部同一处崩溃:

1. **PBO 矩阵不等长崩溃(阻断级)**:`run_walk_forward` 把「逐 walk-forward 窗口的日收益
   序列」当 PBO/CSCV 的 trials 矩阵。窗口生成器 `_add_trading_days` 只跳周末(近似交易日),
   真实回测权益曲线按真实交易日(节假日)产出 → 各窗口行点数不等 →
   `probability_of_backtest_overfitting` 严格等长断言必崩。**任何配置都复现**(单 trial 也崩,
   因为崩的是窗口矩阵不是 trial 数)。
2. **capital 字符串崩溃**:`default_trial_runner_factory` 对 `capital` 无类型处理,str 直通
   `BacktestConfig`(纯 dataclass,`initial_capital: Decimal` 无强转)→ 引擎算术处
   `str + decimal.Decimal`。
3. **崩溃无兜底**:executor 对 walk-forward 段意外异常不接,实验停留 `in_sample` 中间态
   (可重入队但确定性再崩),job 报泛化错误。

修复口径(#244,分支 feat/validation-experiment-robustness-244):

- PBO 矩阵改取 **IS 阶段各竞争 trial 的日收益**(同 `[train_start, train_end]` 同轴天然
  等长,且符合 CSCV「行=竞争配置」语义);runner 新增内存态 `_in_sample_returns`
  (不持久化,续跑时为空 → PBO 具名跳过写 `methodology_notes`,pbo=0.0);
  best_returns(窗口拼接)仍喂 DSR/PSR/bootstrap 不变。
- capital/params/策略名/基础参数在 runner 构建期具名 `trial_runner_invalid_config`
  预校验(探针 `create_strategy(strategy, "validation-probe", **base_params)` 一石二鸟:
  校验存在性 + pydantic 参数合法性),不消耗试验预算。
- 意外异常兜底为具名 `experiment_execution_failed`:**故意不把实验推进 REJECTED**——
  REJECTED 是实验结论且终态不可重入(揭盲不可重做),崩溃是平台问题,烧掉实验等于
  把 bug 变成永久判决;保留 `in_sample` + 错误附续跑指引,修复后重入队断点续跑。

## Why(为什么)

- 不等长崩溃在单测里从未暴露:测试假 runner 按日历天产曲线,而测试 plan 的
  step_days=10(整 2 周)使所有窗口起点同为周三 → 日历跨度恰好全等。真实世界的
  节假日 + 任意 step 必然打破等长。教训:**等长类断言的测试要主动造不等长输入**,
  靠「恰好对齐」的 fixture 是假覆盖。
- PBO 语义错误比崩溃更隐蔽:即使等长,用顺序窗口当竞争 trials 得到的 PBO 也是
  无意义数(窗口是同一路径的分段,不是竞争假设)。修崩溃时必须同时修语义,
  否则只是把崩溃换成噪声门值。

## How to apply(下次如何应用)

- 给假回测 runner 造曲线:**必须含至少一次回撤**(既有 `_fake_runner_factory` 就因
  mdd=0 → calmar=Infinity 落 JSON 列崩过,注释写明;本轮 ragged runner 再踩一次)。
- 实验类状态机遇执行器异常:一律「具名化 + 状态保留 + 续跑指引」,永远不要在
  异常兜底里替实验下 REJECTED 结论。
- 断言「输入必须等长/同轴」的统计函数,调用方要自带矩形性卫兵并具名上报跳过原因,
  不能让裸 ValueError 炸穿任务队列。
- 相关:#233 执行器、#245(screen 门 `min_abs_rank_ic` 取 abs,负 IC 方向型因子可过
  promote——开放问题:abs 维持/方向硬门/告警降级,待用户决策);E2E 数据面观察
  (发布 instruments list_date 空 = 2026-08-04 的发布早于 #185 兜底,重发布即愈,
  非代码 bug)。
