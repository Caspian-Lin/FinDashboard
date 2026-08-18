# Issue #203:multi_period 因子快照依赖的文档与报错对齐

## 主题

research_run 多期回放(#183)的文档措辞、执行期报错、入队期校验三者对齐:
「multi_period 不要求预建因子快照」是**有边界的**承诺,agent 误读后撞上
混淆的报错文案。2026-08-18 修复,PR 待合并。

## 结论 / 事实

- **multi_period 必须显式声明 `parameters.rebalance_frequency`(monthly|
  quarterly)**;未声明即 single_shot,该路径决策时点**只能**来自冻结因子
  快照(fail-closed)。`execution_mode_for` 的旧注释「非法值一律按
  single_shot 处理」与执行期 `_rebalance_frequency` 的 fail-closed(非法值
  抛错)**并不一致** —— 实际行为:非法值在入队时被 `ResearchRunQueueIn`
  schema 拒绝(#183 已有),执行期兜底抛错;只有「未声明」才落 single_shot。
- 「不要求预建快照」仅限**决策日推导与价格因子**(momentum/volatility
  每期由 `build_price_feature_snapshot` 按发布重算);基本面因子(pb/ROE
  等)仍 PIT 取自 `manifest.factor_snapshots` 观测或研究数据发布
  (`fetch_daily_metrics`/`fetch_financial_indicators`,#187),缺来源执行期
  fail-closed。
- 原报错「manifest 未冻结因子快照且未设置 rebalance_frequency」混盖两种
  根因:①声明了多期但发布日历推导不出决策时点(每期期末后须有下一成交日,
  发布只覆盖一个月期末即末日时发生);②未声明频率的 single_shot 缺快照。
  现按根因分别报错,均附 `execution_mode` 与修复路径。
- 入队校验:REST 与 MCP(`_build_queued_manifest`)原已有「依赖因子输入 +
  无快照 + 非 multi_period → 拒绝」(#183 加的 is_multi_period 旁路),本次
  提炼为共享纯函数 `signal_engine.single_shot_snapshot_gate_error`(报错
  文案单一来源,不双份漂移),并补上「multi_factor 无因子源也无快照」的
  拦截 —— single_shot 决策日来自快照,零快照必然执行期失败,没有合法放行
  场景。非信号引擎 kind 不拦(worker 报 not_implemented 是真正根因)。

## Why

- 文档过度承诺 + 报错不区分根因,agent(与用户)会把「声明了频率但发布区间
  太短」误诊为「缺快照」,或反过来以为 multi_period 全套免快照而漏冻结
  基本面输入。
- #186 已确立「入队秒级失败 + 报错附根因」风格;#203 把同一风格应用到
  快照缺失这一必然失败的形态。

## How to apply

- 改动 REST/MCP 入队校验时,永远通过 `single_shot_snapshot_gate_error`
  单点修改,不要在两边各写一份判断/文案(历史上就是双份拷贝)。
- 新增执行模式或决策日来源时,同步检查三处契约件:`tools.md`、
  `research_run_lifecycle.md`、MCP `_INSTRUCTIONS`(issue #123 规范),
  并确认 `execution_mode_for` 注释与 `_rebalance_frequency` 行为一致。
- 测试报错文案区分时用 `pytest.raises(match=...)` 同时断言「含 A 根因词」
  与「不含 B 根因词」(如 multi_period 报错不含 factor_snapshots)。
