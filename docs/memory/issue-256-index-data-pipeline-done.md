# issue #256 指数标的数据链路入口完成(2026-09-02)

**主题**:#184 指数基准能力运营化——「登记 → 同步 → 发布 → benchmark_return」
四断点闭合,PR #277(目标 `m/research-backtest`,待合并)。

## 结论 / 事实

- **登记**:`UniverseDiscovery.discover_indices` 读受控登记表
  `BENCHMARK_INDEX_REGISTRY`(9 只宽基,导入期断言 `is_index_code`),
  并入 `discover_all()`;data_sync 自动登记 `instrument_type=index`。
  指数无 list_date/industry 上游,保持 null 不虚构。
- **同步**:`bulk_download` 的 `instrument_type=index` 走 akshare 指数日线;
  tushare 源 `tushare_scope_mismatch` 拒绝保持。
- **发布形态硬约束**:`_bars_release_ref` 要求 manifest 恰好一个 bars 主发布,
  指数基准必须与股票候选池**同处一份 multi_asset_mixed 发布**(双 bars 发布
  形态走不通)。
- **候选池边界**:指数不可撮合(#184 既有边界落到实现)——
  `universe_precheck.static_universe_candidates` 与
  `frozen_loader._build_candidates_and_lots` 共用新谓词
  `is_benchmark_only_instrument` 排除指数;`_metadata_warnings` 统计域同步
  跳过指数(否则混发发布永久产出 list_date 噪音 warning)。
- 端到端集成 `tests/integration/test_index_benchmark_chain.py`:真实
  FrozenReleaseProvider worker run,`benchmark_return≈0.5` 非 null +
  UNIVERSE artifact 无指数。

## Why

bars 主发布唯一性使「指数进发布」与「指数不进候选池」必须同时成立,
否则 #256 会把指数送进排名/持仓(违反指数不可撮合);这不是可选清理,
是链路自洽的必要条件。

## How to apply(下次如何应用)

- 测试 manifest 放宽风控参数**必须嵌在 overrides 键下**:
  `portfolio_config={"overrides": {"max_risk_contribution": 1.0}}`——
  `_section_overrides` 只读 `overrides` 键,平铺键静默不生效。
- `WorkerConfig` 没有 operator/source 字段(那是 `FeatureNode` 的);
  照抄其他测试构造参数前先核对 dataclass 定义。
- 共享集成测试库跨 pytest 会话存留:`_clean` 只删本测试 codes,
  断言用终态覆盖而非「本次新增数」(`sync_with_diff` 的 `new` 因历史行偏小)。
- 合成 OHLC 数据若 close 与 low/high 脱节,发布质量门 `anomaly_ratio`
  会正确拦截(意外验证了质量门)。
- 新增基准指数:在 `finboard_data/discovery.py` 登记表加一行即可,
  代码必须满足 `is_index_code`,data_sync 自动落库。

## 附:CI 偶发失败排查(test_partial_fill_restart_recovery,与 #256 主题无关)

**现象**:PR CI 三连败同一实盘域测试;基线分支 8/31 也偶发过。

**根因链**:(1) CI 覆盖率负载下 OrderManager 回报事件消费可超 300ms,测试固定
`sleep(0.1)` 过期后状态仍 ACKNOWLEDGED → 断言失败;(2) 泄漏的 consumer 任务在
teardown 清表后才消费事件 → fills FK 报错只是连带噪音;(3) 若只把等待改成
「状态到位」轮询,会在 handler 中途就返回,`kernel.stop()` 取消任务打断 flush →
事务作废 → `session.commit()` 抛 PendingRollback(空 original exception)。

**修法**:轮询条件用「fills 落库 + 状态推进 + OrderFilled→PositionManager
持仓 upsert」三条件齐备 = `_on_filled` 完整结束、consumer 回到 queue.get
阻塞点,此时 stop() 取消才安全(12 连跑稳定)。

**Why**:AsyncSession 被测试协程与后台 consumer 并发共享;任何「只看单一状态
字段」的等待都可能停在 handler 中途,取消/回滚类失败全都源于此。

**How to apply**:交易域集成测试等待回报消费时,一律轮询到「事件处理链的
最后一步 DB 动作」完成(PositionManager 持仓 / audit 日志),不要用固定 sleep,
也不要只等订单状态字段。
