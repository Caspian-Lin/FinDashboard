# FinDashboard 正式规格验证契约（#502/#503）

入口：REST `POST /api/research/experiments`，MCP `finboard_validation_experiment_create`；两者共用freeze_spec_runner。创建只保存计划，run才入队。旧实验默认registry兼容，不修改已存结论/manifest/checksum。

`version_stamp.selection_config.validation_trial_runner` 明确两种入口：

```json
{"kind":"research_spec","baseline_run_id":"RR-<精确completed基线>","used_windows":[{"start":"2015-01-01","end":"2026-09-04","source":"已经观察/调参的区间"}]}
```

```json
{"kind":"registry","strategy":"ma_cross","symbols":["600000.SH"],"params":{"short_window":5,"long_window":20},"capital":100000,"stress_schema":"executed_v1"}
```

正式入口要求已发布multi_factor版本，冻结整份基线manifest及checksum，包括唯一bars主发布、研究发布、因子引用、日历、资金、代码、组合与风险覆盖。服务端拒绝客户端frozen_manifest、未知字段和非空strategy_params_space；当前支持冻结参数单点，不能用不生效的网格消耗预算。传统provider版本声明不等于物理冻结数据，registry证据注明此限制，不能用于正式规格代验。

used_windows必须显式提供，最多100个区间且start/end须为日期字符串；服务器加入基线权益的实际观测范围。最终test与任一已用闭区间相交即test_window_already_used。正式train/validation/test闭区间不得相交；benchmark必须与冻结基线一致。当前v13完整历史已用于开发/评估，在2024–2026重做仅属回顾敏感性。新OOS需尚未用的冻结发布与因子覆盖，不能重开旧已揭盲实验。

预热从冻结bars发布起点读取到各决策，保持PIT；research_window分别记录warmup_start、decision_start、decision_end、valuation_end。只保留原日历中窗口内决策，不移动决策日；末决策之后必须存在窗口内下一成交日。正式预检覆盖每个IS/WF/最终窗、所有引用factor_series的发布/checksum/日期，最多100个窗口；执行期再由同一信号/组合管线fail-closed。特征预热不进权益，成交/尾段估值不越valuation_end。

每个子run身份由实验×冻结输入×窗口×覆盖确定；普通研究与等价验证使用同一PortfolioPipelineAdapter。子run持久化manifest/result/13阶段产物，trial.robustness_probes追加完成证据，记录run_id、checksum、窗口、生效费用、诊断、成交数及不支持能力。研究指标采用252交易日年化、rf0/ddof1；传统registry保留旧engine口径。换手为成交名义额/平均权益×252/权益点数，缺口使用同决策计划名义额作分母，不是PnL或实际冲击。

IS逐trial提交，失败计预算，续跑复用持久化trial与确定性子run；worker进度/取消传入子run，每操作store超时与BLAS/watchdog不放松。最终揭盲标志在执行前提交，取消/异常后也禁止重新揭盲；留不完整证据，不能把流程完成当假设支持。先读oos_outcome，再看status。

新版自动压力由正式入口或registry `stress_schema=executed_v1`启用；最多24档。成本只缩放佣金率、最低佣金与卖出税，滑点独立。explicit_overrides_v1将合并的四项费用显式冻结进fee_config.overrides，覆盖进入可行性、手数和成交；免税资产保持零税。旧manifest仍资产规则优先，旧基线须有cost_x1控制再归因。资金档10–50万元，只研究资金/成交约束，不模拟订单簿。

2Bar延迟没有执行入口，unsupported；不得移动决策日伪装延迟。WF最差窗口只属窗口敏感性，非参数邻域；请求neighbourhood_steps>0但无扫描时unsupported。缺支持、失败或超时不能全通过。历史探针缺detail.status在UI显示执行未核验，不追改旧通过结论。

错误契约：REST缺引用404、参数/窗口/覆盖422、载荷413；MCP not_found/invalid_argument/payload_too_large。研究读投影SQL在加载前估计，单决策也不豁免。所有能力仅研究域；对应phase1_doc.md §3.4 3–14的离线等价验证，不声称券商或实盘验收。
