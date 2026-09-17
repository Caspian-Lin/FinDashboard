# issue #173:selection bars/snapshot 输入模式

2026-08-16 完成并合入。`FactorSelectionConfig.inputs_mode` 支持
`research_db`(默认)/ `bars` / `snapshot`;`required_datasets` 按
`required_factors` 依赖推导,纯价格因子(momentum / volatility_20d)配置不再
要求 daily_metrics 发布。`FACTOR_CATALOG` 新增 `volatility_20d`(与
`factor_lab.FACTOR_LAB_CATALOG` 同名同窗口口径)。`FactorSnapshot` 新增
`warnings` 字段(bars/snapshot 模式 dataset 未发布、instrument_profiles 缺失
降级提示,随快照落库,`factor_snapshots.warnings` JSON 列迁移
`c3d9a1b2e4f5`)。snapshot 模式由新适配器
`finboard_backtest.selection_snapshot.FeatureSnapshotFactorReader` 按
`snapshot_ids` 反序列化观测并做 `available_at <= decision_at` 过滤。

**Why:** 踩坑点有三处容易再犯:

1. **同名异类陷阱**:`finboard_persistence` 有两个带 Snapshot 的 Repository——
   `FactorSnapshotRepository`(factor_repo.py,`save_factor_snapshot` / 按
   checksum 幂等)与 `FeatureSnapshotRepository`(factor_lab_repo.py,按
   `snapshot_id` 的 `get` / `list` / `publish`)。snapshot 输入模式的 provider
   必须用后者(有 `get`),组装点里两个类都 import 时才容易拿错。
2. **集成测试库不迁移旧表**:`tests/integration/conftest.py` 用
   `Base.metadata.create_all`,已存在的旧表不加新列;改表后需对
   `findashboard_test` 手工 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`。
3. **sum 类型**:`sum(生成器)` 空序列返回 int,`variance.sqrt()` 在 mypy 下
   报 `Decimal | float`;`sum((..., Decimal(0)))` 显式给起始值。

**How to apply:** 扩展 selection 新输入模式/新因子时,遵循
`required_datasets` 按 `required_factors` 推导的路径(`_DAILY_DEPENDENT_FACTORS`),
bars/snapshot 模式下 dataset 未发布走 `_degradable_issues` 降级为 warnings
而非整日 SKIPPED;MCP/Skill 契约已随 #123 同步。相关:#170 的
signal_engine 直接读冻结快照;snapshot 适配器照抄
`research_run/frozen_loader.py` 的 `FeatureSnapshotProvider` 回调注入模式。
