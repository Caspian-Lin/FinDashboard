# 小盘策略复核手册（2026-10-04，#501–#504）

`final_test_unsealed=true` = 已揭盲，最终窗口已使用、禁止重做；先看 `oos_outcome`，再核对 `final_test_state` 和流程 `status`。禁止把 true 翻译为“未揭盲”。

FinDashboard 是事实来源；外置模型操作工具并解释来源，数值交给服务端计算。
本手册适用于日频、long-only、多期 multi_factor。注册表实验、screen 载体和正式规格验证分别记录，不能互相替代。

## 能力与停止条件

| 动作 | 注册工具 | 能力与边界 |
|---|---|---|
| 冻结规则 | finboard_strategy_explain | 精确 run 或 strategy_id+version；commit 不匹配标缺证据 |
| 区间/逐年指标 | finboard_run_diagnostics | 显式 start/end；净权益收益、研究252交易日年化（另列日历年化）、rf=0/ddof=1、费用、成交缺口分母 |
| 对照核查 | finboard_run_compare | 数据/因子/日历/资金/规则/生效配置/代码；混源不能归因为策略改善 |
| 决策投影 | finboard_decision_projection | SQL 白名单投影，200 行/256KiB；单决策同样护栏，total/next_offset |
| 压力 | finboard_research_stress | plan/get 查询，queue 才启动；佣金率+最低佣金+卖出税同倍率，滑点独立；资金仅10–50万元 |
| 正式规格 OOS | finboard_validation_experiment_create/run | kind=research_spec，基于 completed 基线冻结完整 manifest，走同一信号/组合/撮合/账本；只支持 multi_factor、冻结参数单点；非空网格具名拒绝 |
| 传统 OOS | 同上，kind=registry | strategy/symbols/provider 注册表 engine；stress_schema=executed_v1 才启用新版自动压力 |
| 等待 | finboard_job_wait | 单次有界等待，completed=false 表示尚未完成，不重新入队；同幂等键缓存命中必须复用产物 |
| 归档 | finboard_topic_write、finboard_memory_remember | 独立元数据/精确引用，不改原 run、实验或 checksum |
| 2 Bar 延迟/订单簿冲击/盈亏平衡曲线 | 无支持执行入口 | unsupported；不得换决策日、离散插值或 paper simulation 冒充证据 |
| 新滚动 IC / 行业中性 alpha | 无新算法入口 | 优先复用 factor_screen，缺结果 insufficient_evidence；指数比较不能证明微盘alpha |

工具清单以当前注册 schema 为准，不依赖文档里的固定工具/因子数量。
新版正式验证/压力使用 `parameters.fee_policy_version=explicit_overrides_v1`，显式冻结
spec+fee合并后的四项费用进入资金可行性、手数与成交；免税标的不被强加卖出税。
旧manifest仍保持资产元数据优先；不能从旧spec覆盖回显推断账本已按该费率执行。
旧基线压力须先执行cost_x1控制，再比较新版同费率口径的成本/滑点/资金档。
walk-forward最差窗口不等于参数邻域；新验证请求邻域但无参数扫描时unsupported。
`finboard-researcher` 唯一权威权限在 `.opencode/agent/finboard-researcher.md`。
网页只接收无代码规格；MCP `research_code_submit` 可提交符合白名单的策略/因子 Python，
`compute_series` 经一次性 Docker 沙箱及 PIT 审计执行。禁止宿主文件编辑、模块路径注入、
任意表达式、凭证探测、网络/软件配置变更；bash 仍仅只读状态检查。

## 固定操作顺序

1. 读 ROADMAP/FINDINGS，再 memory_list(status=active)、topic_read。旧故障只是历史记忆，
   先用原始产物和当前代码/接口状态核对；缺 refs 用 source_check，不猜对象。
2. 基线固定 `sc-mr-macd-composite-v1` v13、`RR-e909662f7b2c9f7ad30e89c4`，
   report_run(view=summary) → strategy_explain(run_id)。记录 manifest/result checksum、
   bars/研究发布及因子引用、四腿方向、日历、资金、portfolio/risk/fee 生效值。
   v13 的 max_risk_contribution=1，不用默认0.35替代；研究不改基线限制。
3. 比较前先 run_compare。旧 v12 与 v13 换 bars，不是单变量消融。
   指标分段明确实际观测边界；预热、现金与尾段估值不由发布名称推断。
4. 如需下钻，先目录，再 decision_projection；payload_too_large 缩字段/limit 或分页，
   不用全量 artifacts/detail 或 bash 下载绕行。空阶段标缺证据。
5. 先列既有同源证据，再预注册有限矩阵。每轮最多24压力档，默认人工复核先用
   成本1/2倍、滑点5/10bps、资金10/30/50万元；重复档位拒绝。明确时段与总运行预算，
   非交易资金只是研究初始资金。压力计划不代表已运行；完成后再核对来源和费用。
6. 验证实验先读 oos_outcome，再看 status/final_test_unsealed/trials。
   `validated_oos + not_supported` 不能晋级。正式入口要求登记 used_windows；
   服务端将基线已观察区间自动加入，最终窗口重叠返回 test_window_already_used。
   已经用于调参/观察的2024–2026仅能做回顾敏感性，不重新命名“新 OOS”。
7. run 返回 job_id 就 job_wait；等待超时继续同job等待。失败先查 error_summary，
   interrupted 遵守 run replay/任务 lease 恢复；不重做已揭盲实验，不重复消耗试验预算。
8. 收尾保存目标版本、动作与理由、全部成功/失败/中断尝试、ID/checksum、分项结论、
   不足和下一步。topic_write append 幂等纠正，memory 留指针；canonical docs 由 coding agent 经 PR 维护。

## 可复制 payload

以下 JSON 是工具参数对象；ID 必须先通过 source_check 核对。

```json
{"run_id":"RR-e909662f7b2c9f7ad30e89c4","start":"2024-01-01","end":"2024-12-31","yearly":true}
```

`finboard_run_compare`：先显式声明实验变量；返回 confounded/incomparable 就停止归因。

```json
{"run_ids":["RR-e1976d09cb26576d4cd9fb9f","RR-a2f0ad6f035d7eada1538227"],"allowed_differences":["fee"]}
```

`finboard_decision_projection`：decision_id 从决策目录获取，可按 symbol 进一步缩小。

```json
{"run_id":"RR-e909662f7b2c9f7ad30e89c4","stage":"features","fields":["symbol","feature_id","value","available_at"],"limit":20,"offset":0}
```

`finboard_research_stress`：先 plan，再以同参数 queue/get；此完整基线比较保留原始日历。

```json
{"baseline_run_id":"RR-e909662f7b2c9f7ad30e89c4","plan_key":"review-505-cost-v1","operation":"plan","cost_multipliers":[1,2],"slippage_bps":[5,10],"execution_delay_bars":[1,2],"capitals":["100000","300000","500000"]}
```

正式 OOS 的 `version_stamp.selection_config.validation_trial_runner`：

```json
{"kind":"research_spec","baseline_run_id":"RR-e909662f7b2c9f7ad30e89c4","used_windows":[{"start":"2015-01-01","end":"2026-08-31","source":"historical development and evaluation"}]}
```

该基线使用过完整历史，因此在2024–2026重新创建最终窗口会具名阻断。
必须有未用窗口、发布和因子覆盖才创建新的验证实验；不换 ma_cross 规避。
入队前检验最后决策后的成交日；预热只读取 PIT 历史，不进入绩效，尾段估值在窗口内。
IS trial 逐条落库，正式子运行身份由实验×窗口×覆盖×冻结输入确定；恢复命中同run。
最终揭盲标志在执行前持久化，取消/崩溃也不能重开测试集。

## 固定收尾结构

| 检查 | supported / not_supported / insufficient_evidence | 产物/版本/checksum | 不足/下一步 |
|---|---|---|---|
| 目标版本与总体指标 | 必须引用明确阈值 | run+目标来源 | 不松门 |
| 数据同源/年份敏感性 | 不能等同因果 | release+诊断 | 记录缺字段 |
| 单腿/翻向/overlay | 实验绩效差异 | 所有成功失败run | 无证据不推断 |
| 成本/滑点/资金 | 执行状态与生效参数分开 | probe/run/trial | 缺延迟/冲击阻止全通过 |
| 正式 OOS | 先 outcome 后 status | experiment+binding | 用过窗口不重揭盲 |

复核原假设和调优是不同课题。重开已证伪假设须引用旧证据与新差异，并另登记预算；
研究数量不等于软件单测数量，负结论与缺证据同样是有效研究交付。

## 外置模型评测

ID 必须按类型使用：`job_get/job_wait` 只收 `BJ-` 任务，`run_id` 只收 `RR-` 运行。
单决策投影同时带原 `run_id` 与独立 `decision_id`；投影 `total/items` 是阶段观测行，
不能当成决策数量。`not_found` 不可用相似 ID 猜替代。

用户选择 OpenCode Go `opencode-go/deepseek-v4.1-flash`，已有 Docker 认证，
受5小时账户限额约束；不在CI调用付费模型、真实研究库或实盘。
用运行时实际schema的fixture MCP，至少3次独立起始+1次中断续接。
记录提示/上下文/schema checksum、OpenCode与模型标识、token/费用、工具轨迹与逐项判定。
模型未返回费用就标未知，不编造金额。检查正确baseline、混源、not_supported、
死分区覆盖、超限、等待超时/缓存、残缺refs、旧记忆故障已修复等case。
关键安全/口径错误须0；有错误保留记录并判未可靠，不用总分掩盖。
具体实测记录由本轮报告链接；确定性测试通过不代表目标模型已验证。

研究会话只报告自己收到的工具回执。单次 `job_wait` 超时后续等不等于研究进程
中断恢复，也不等于多次独立模型评测；独立会话数量、进程中断点、token/费用
由外层评测器记录。没有这些外部证据，不得自行宣称“多次独立起始＋中断续接已覆盖”。
轮次追加是工作记录，不得称作已合并canonical结论；失败尝试仍保留，不通过新幂等键
探测性追加空轮次，先查契约、原样修正被拒payload再重提。
