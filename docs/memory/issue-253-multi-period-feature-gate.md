# Issue #253:multi_period 因子可用性元数据——入队期具名校验(2026-09-02,PR #274)

## 主题

multi_period run 声明财务因子(pb/roe 等)或非标准价格特征时,「identity 节点
缺少数据源」此前拖到执行期才爆(run 已排队、worker 已开跑)。#253 把因子
可用性做成发布侧元数据并入队秒级判定。实现:PR #274 → m/research-backtest。

## 结论 / 事实

- canonical 映射 `RESEARCH_RELEASE_FEATURE_NAMES` 在 `finboard_data.releases`
  (enum-keyed);发布构造器按 kind 冻结 `derived_features` 进 release
  manifest;`derived_feature_names` 空值回退 kind 映射 → 旧发布不误拒。
- 入队门控 `multi_period_feature_gate_error` 在 signal_engine.py,REST
  `queue_research_run` 与 MCP `_build_queued_manifest` 同插、置于用户因子
  门控之后(multi_period 引用 u_ 因子先按 #217 具名拒绝)。
- 检查对象是**全部 identity 节点 source**(`node.source is not None`),
  不是仅 FACTOR/RISK_FACTOR——执行期 `_evaluate_node` 对任意 identity 取
  source,仅 `close` 有价格特殊分支;volume/macro 类 MARKET_INPUT 同样必炸。

## Why(为什么)

- `ResearchDatasetRelease` 加字段必须过 `_release_checksum`:checksum 覆盖
  `as_dict()` 全量,`load_dataset_release` 无 DB 锚定时重算。新键对旧
  manifest 必须「空值不序列化」而非「序列化为空」,否则历史发布整批拒读
  (同先例:#217 可选键不输出、#187 对 bars pop `dataset_kind`)。
- canonical 映射必须放数据层:发布构造器在 finboard-data,依赖方向禁止
  反向 import finboard-backtest;且「checksum 计算前冻结」要求字段在
  builder 内填好,不能由上层 service `dataclasses.replace` 事后补。
- REST 与 MCP 入队是两份平行实现,任何新门控必须两处同插且顺序一致,
  文案收敛在共用函数(#203 先例)。
- multi_factor 模板第一个节点就是 pb identity:此前「只挂 bars 的
  multi_period 正对照」集成测试入队 201 但执行期必炸——正是本 issue 修的
  静默陷阱;该测试现改为附加 daily_metrics 后放行。

## How to apply(下次如何应用)

- 给 `ResearchDatasetRelease`/manifest 加任何新字段:先确认 as_dict 对
  空值不序列化,并补「旧 manifest 重算 checksum 不变」断言。
- 涉及发布 kind ↔ 特征/字段映射:改 `finboard_data.releases` 一处,
  universe_precheck 的 str-keyed dict 是投影不要手改;漂移由
  `tests/unit/backback/test_issue_253_multi_period_feature_availability.py`
  对 `extract_factor_matrix` 输出的一致性断言锁定。
- 新增入队门控:检查对象、与既有门控的先后顺序(u_ 门控 → 特征门控 →
  空池预检)要在共用函数注释里写明;单测镜像 #203 风格,集成测试注意
  multi_factor 模板默认引用 pb(需要 daily_metrics 发布)。
