# issue-217 沙箱因子接入选股管线

**主题**:issue #217(2026-08-30 完成)——沙箱执行输出落库为 feature snapshot、
`u_` 前缀用户因子可被规格引用、输出质量门、run report 的 factor_screen 指标。

**结论 / 事实**:

- 用户因子名约定 `u_<artifact_name>`(`finboard_data.factor_lab.USER_FACTOR_PREFIX`),
  全链路统一判据:`FeatureObservation` 目录校验豁免、编译器分流、入队门控、
  screen 识别,都靠这个前缀;`sandbox_factor_name()` 负责加前缀(幂等)。
- 沙箱快照锚定:`factor_feature_snapshots.dataset_release_id` 放宽可空
  (迁移 e7d8f9a0b1c2),新 `source_run_id` 指向 `RCR-` run;
  `dataset_release_checksum` 字段被复用承载 mount manifest checksum。
  `as_dict` 只在 `source_run_id` 非 None 时输出该键 → **旧发布快照 payload 的
  checksum 不变**(checksum = as_dict 的 JSON sha256,多一个 null 键就会全变)。
- 质量门数学上 NaN 比例与覆盖率在阈值 0.5/0.5 时互补(n≤universe 约束下
  不可能只挂一个),单测要单独触发 NaN 超标须把 min_coverage 调低。
- screen 的 forward return:相邻决策期用「下一期 inputs.prices」(PIT 口径
  一致,零额外 IO),只有最后一期拉发布区间末 close;`decisions()` 里算
  (async 上下文),`build_report()` 里 replace 进 report(sync)。
- 入队沙箱快照校验:`sandbox_snapshot_dataset_release_ids` 查锚定 run 的
  dataset_release_ids ⊆ 冻结清单(run 缺失 = QualityGateError,fail-visible)。

**Why**:agent 自主因子挖掘闭环(假设→提交→执行→screen→复合→OOS)需要
自定义因子成为选股管线一等公民;retired artifact 若静默放行,已冻结 run 的
复现性与新 run 的可解释性都会断裂。

**How to apply**:

- 改 `FeatureSnapshot` 字段时,凡影响 `as_dict` 输出的都要先想旧 payload
  checksum 兼容(可选字段 None 不输出)。
- 给 user 因子加新校验时,三道闸(编译期 `user_factor_sources` /
  入队期 `user_factor_reference_gate_error` / 执行期冻结 manifest)语义不同:
  编译与入队管「当下可引用性」,执行期数据永远来自冻结快照(artifact 后续
  retired 不影响已入队运行)——这是有意设计,不要「统一」成执行期也查 DB。
- 集成测试的表结构来自 `Base.metadata.create_all`,改已有表列时测试库不会
  自动加列,须手动 drop 旧表(`FINBOARD_TEST_DB_URL` 从 .env 取,默认密码
  CHANGE_ME 连不上;Windows 下 psycopg async 需
  `asyncio.WindowsSelectorEventLoopPolicy`)。
- E2E 新用例 `test_container_output_publishes_as_snapshot` 验证「真实容器
  输出 → 质量门 → 快照」接缝(FINBOARD_SANDBOX_E2E=1,镜像 0.1.0)。
