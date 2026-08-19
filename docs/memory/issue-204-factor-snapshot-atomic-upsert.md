# Issue #204:factor_snapshots 原子 upsert 与确定性并发测试编排

- 主题:`save_factor_snapshot` 从 SELECT-then-INSERT(TOCTOU)改为 PostgreSQL
  `INSERT ... ON CONFLICT (checksum) DO NOTHING RETURNING id`,修复 selection
  回测重复/并发运行的 `ix_factor_snapshots_checksum` UniqueViolation。
- 日期:2026-08-19(issue #204,PR 见关联)。

## 结论 / 事实

- 旧实现的竞态窗口:后台任务并发/重叠运行时,两个事务各自 SELECT 都看不到
  对方未提交的行 → 双双 INSERT 相同 checksum → 撞唯一索引。
- 修复形态:唯一索引原子仲裁。**冲突方(未真正插入)的 `RETURNING` 返回空**,
  需再 SELECT 复用已存在行 id;**只有真正插入方才写 `FactorValueModel`**,
  否则值行会随重复运行翻倍。
- ON CONFLICT 的仲裁是**阻塞语义**:并发事务未提交时,后到方的 INSERT 会
  在服务端等待先到方的事务结局(提交则跳过、回滚则自己插入),不会立即报错。
- checksum 不含 capital(selection 快照 payload = decision_at/config/values 等,
  decision_at 又来自 `_market_close(business_date)`),所以「不同资金同配置」
  重复回测逐字节同 checksum,必须走幂等复用路径。

## Why

- 集成测试里用 `asyncio.gather` 同时跑两个保存**不可靠复现**该竞态:本机实测
  旧实现也通过——psycopg 每条语句的响应到达顺序不定,两个协程可能完全串行
  化,B 的 SELECT 排在 A 提交之后(退化为顺序幂等,旧实现本就正确)。
- 确定性复现必须**显式编排时序**(见下),验证过:旧实现稳定报
  `UniqueViolation`,新实现稳定通过。

## How to apply

- 复现 DB 层 TOCTOU 竞态的编排模式(见
  `tests/integration/test_factor_snapshot_persistence.py::test_factor_snapshot_concurrent_sessions_same_checksum`):
  1. 任务 A:save(flush 未提交)→ `b_started.set()` → 挂在 `a_may_commit` 上;
  2. 任务 B:等 `b_started` 再发起 save,INSERT 自然卡在 A 的未提交行;
  3. 释放任务:等 `b_started` → `sleep(0.2)`(让 B 的语句到达服务端)→ 放 A 提交。
  无死锁环:B 依赖 A 提交,A 依赖释放事件,释放只依赖 `b_started`。
- 回测引擎层的重复运行测试:用假 bar provider + 裸 bars reader + 真实
  `FactorSnapshotRepository` writer,两次 `engine.run()` 传不同
  `initial_capital`,断言两轮 `snapshot_id` 一一相等、库中每 checksum 只一行、
  factor_values 行数等于单轮值数。
- 后续写同类「按内容幂等」的保存逻辑,直接用
  `pg_insert(...).on_conflict_do_nothing(index_elements=[列]).returning(主键)`
  单语句实现;参考先例:`factor_repo.py` 与
  `InstrumentLifecycleEventModel` 的 `on_conflict_do_nothing(constraint=...)`。
- 测试引用 `_engine` fixture 参数触发 ruff PT019(误报),仓库既有惯例是
  行尾 `# noqa: PT019`(test_factor_laboratory_persistence.py:118)。
