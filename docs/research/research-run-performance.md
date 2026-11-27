# 研究运行性能复测

目标是在 8 核、16 GiB 的部署环境中，至少 95% 的代表性研究运行在 30 分钟内完成。统计从 worker 领取任务开始，到结果可读为止。另报排队耗时、运行失败和重试。

性能样本按候选池规模、窗口长度、调仓期数、用户因子、冷暖缓存及断点恢复分层。分别报告样本量、p50、p95 和峰值进程树 RSS。按实际部署请求的占比合成总体分位数。记录 CPU、内存限制、PostgreSQL 和冻结发布版本，以及同时运行的任务。

## 复测入口

准备独立 PostgreSQL 基准库，例如 `findashboard_bench_research`，执行现有迁移。在该库导入内置研究规格 manifest 引用的研究快照、因子序列及需要的日历和停复牌行。磁盘上的冻结发布与因子序列可以只读复用。保存 manifest JSON，并用 `FINBOARD_BENCH_DB_URL` 注入独立库连接。脚本使用 worker 的短会话 store，从 coordinator 启动计时。真实 worker 领取和恢复由集成测试验证。

```powershell
uv run python scripts/benchmark_research_run.py --manifest case.json --release-root data_releases --cache-state warm --run-key baseline --no-prefetch --output baseline.json
uv run python scripts/benchmark_research_run.py --manifest case.json --release-root data_releases --cache-state warm --run-key prefetch --prefetch --output prefetch.json --compare baseline.json
```

两次 fresh 运行使用不同运行键。按 A/B/A 或 B/A/B 顺序，在同一机器和缓存条件下对照。比较分块第一个决策（mod 4 为 0）与其余三个位置的落库间隔、首个新决策等待、端到端墙钟、进程树 CPU 与 RSS。累计子阶段耗时包含重叠，运行墙钟单独取值。

验证 272 期断点时，用同一 manifest 和运行键先执行 `--interrupt-after 272`，再从新进程执行 `--resume`。只测到首个新决策时可追加 `--interrupt-after 273`。#470 的阈值为续跑到首个新决策落库 ≤10 分钟，进程树峰值 RSS ≤8 GB。前缀准备耗时另记。全量对照以 `--compare` 核验状态、每期各阶段的 checksum 与结果 checksum。历史 #475 样本在第 555 期被硬约束拒绝，可以用 `--expected-status rejected` 核对原拒绝与 checksum，并另用能完成的样本验证终态。

合成冻结样本、真实 PostgreSQL 和 worker 的 CI 覆盖见 `tests/integration/test_issue_314_checkpoint_resume.py`。它执行 26 个决策，跨分块中断并续跑，对照关闭预取的运行逐项比较 payload、checksum、检查点时间与重试状态，并断言只有后缀调用批量写入。另有损坏 checksum、变化载荷重算自身 checksum、并发重复写入、唯一序号冲突整批回滚的测试。

## 冻结样本的基准证据

- 以全市场历史运行 RR-169678a2196b8842ac8f28fb 的冻结行情、daily_metrics 和四个因子序列为输入，截取 2023-04-14 至 2023-10-27 的 26 个决策，执行到结果可读。热身使用 Windows 11、Python 3.12.12、8 个可用逻辑 CPU、4 个特征进程和实际 42,660,126,720 字节宿主内存。未控制缓存。耗时 411.599 秒，首个决策 310.157 秒，进程树峰值 RSS 1,549,299,712 字节。产物校验和 `cdc93adb352b9f406b3fa5304871debe01f961bcfe22e929825e2f10eb1e2f4f`。
- 同一冻结输入运行最终代码并关闭预取，墙钟 350.177 秒，首个决策 240.084 秒，进程树峰值 RSS 1,524,215,808 字节，抽样 CPU 841.359 秒。逐阶段产物及结果 checksum 与热身一致。相邻落库间隔的分块首槽均值 8.557 秒，其余三槽约 2.53–2.56 秒。权益回读均涉及 168 个标的，累计读取从 61.028 秒（整 Bar）到 35.140 秒（时间戳和收盘价），后一次权益输入读取墙钟 9.864 秒。两次运行的缓存与预取条件有差异。
- 之后开启预取，墙钟 327.286 秒，首个决策 224.875 秒，进程树峰值 RSS 1,527,083,008 字节，抽样 CPU 807.875 秒。分块首槽均值 7.423 秒，其余三槽约 2.41–2.60 秒。两次均完成 339 条产物，逐条 checksum、结果 checksum 和载荷字节一致。开启预取在后，缓存更暖，关闭预取早期与前缀复制重叠。23 秒墙钟差值包含这些影响。
- 上述热身的 26 次初始协方差累计 8.66 秒，25 次目标覆盖重建累计 1.85 秒，初次全市场 close 历史加载约 199 秒。开关预取的收益以同缓存条件下的对照为准。初始协方差矩阵仍进入组合输入 checksum。复用同次 Ledoit–Wolf 的样本矩阵时保留数值运算、初始矩阵和覆盖重建。
- 合成日收益各 252 项，在同一进程交替执行两版各 12 次。145 个标的估计中位数由 10.592 降至 9.888 ms，200 个标的由 17.791 降至 13.982 ms，600 个标的由 439.261 降至 411.262 ms。矩阵字节、顺序、收缩强度相同。
- 对 64 个真实冻结标的的 173,713 根 Bar，子进程返回值在相同 pickle 协议下由 9,750,502 降至 5,584,142 字节。一次编码累计耗时由 0.759 降至 0.019 秒。如实记录跨进程编码，完整任务耗时另测。
- 272 期前缀从 PostgreSQL 游标按组复验。续跑仍保留已重建的前缀 bundle 直到适配器接收，消费后释放。普通因子只为后缀预建 daily 与价格特征截面。用户因子保留完整前缀装载以构造 screen。
- 组合管线接受普通因子种子后，coordinator 原样校验账本，复查候选池与特征的可逆编码、其余阶段的 canonical checksum、产物身份及数据库现存摘要。验证通过的前缀保留阶段进度，跳过重复的大截面编码和批量写入。适配器拒绝种子、源行丢失或校验不一致时进入现有重算或幂等写入口。
- 从原 556 期样本只读复制最初 272 期的 3,536 条原始产物到隔离库。续跑进程通过 272 期校验并接受种子，第 273 期新生成的 13 项 checksum 与历史逐一一致。启动至新期落库 987.510 秒，进程树峰值 RSS 9,208,459,264 字节。原 #470 两个门槛均未通过。约 433 秒用于读回与重建、47 秒用于种子行情、203 秒用于前缀幂等重放、305 秒用于后缀预建和新决策。前缀复制另计。这次执行早于按期复用 FeatureValue 重复元数据的补丁。
- 单独用上述第 273 期的 56,869 个特征观测回建，复用重复元数据后，`tracemalloc` 观测到的活跃 Python 堆由 21,993,614 减至 6,159,848 字节，单组峰值由 41,717,653 减至 35,209,541 字节。canonical 特征区 checksum 一致。
- 在同一机器和冻结输入上重新克隆 272 期前缀，使用最终代码续跑。首个新决策在 964.603 秒落库，进程树峰值 RSS 为 5,044,293,632 字节，较前次测量下降 45.2%。3,549 条阶段 checksum 逐项一致。#470 的内存门槛通过，10 分钟门槛仍未通过。前缀读回与重建约 417 秒，种子行情与前缀幂等重放共约 242 秒，后缀预建及首期约 308 秒。两次缓存未受控，前缀克隆时间另计。
- 增加逐阶段证明复用后，再次克隆上述 272 期。全部 272 个断点通过复查，第 273 个决策在 819.541 秒落库，3,549 条阶段 checksum 逐项一致。峰值 RSS 为 5,068,939,264 字节，抽样 CPU 为 1,655.875 秒。源读取与重建仍约 7 分钟，种子行情与前缀重放约 81 秒，后缀预建及首期约 304 秒。相比上轮，首个新决策提前 145.062 秒。缓存未受控，10 分钟门槛仍未通过。

## 存储

基准的 `stored_payload_bytes` 是 PostgreSQL 逐载荷 `pg_column_size` 的和。26 期样本为 37,615,298 字节。数据库占用另用 `pg_total_relation_size`，同时计入关系、索引和 TOAST 相关开销。WAL 单独测量。

本地只读分组统计显示，9,684 条 features 的载荷占 10,542,577,764 字节，2,640 个不同 checksum 各保留一份时约 2,604,749,731 字节。universe 对应 344,475,149 与 47,045,344 字节。内容寻址可能消除约 8.24 GB 重复载荷。若实施，应先验证历史 checksum、事务内索引与现有 SQL 投影读取的等价性。

```sql
WITH payloads AS (
    SELECT stage, checksum, count(*) AS rows,
           sum(pg_column_size(payload)) AS stored_bytes,
           min(pg_column_size(payload)) AS single_copy_bytes
    FROM research_run_artifacts
    GROUP BY stage, checksum
)
SELECT stage, sum(rows) AS rows, count(*) AS unique_payloads,
       sum(stored_bytes) AS stored_bytes,
       sum(single_copy_bytes) AS single_copy_bytes
FROM payloads
GROUP BY stage
ORDER BY stored_bytes DESC
```

128 个真实行情文件的编码对照中，Snappy 共 14,049,854 字节，Zstd 3 共 11,666,240 字节。单轮编码分别为 0.352 和 0.441 秒，解码分别为 0.215 和 0.211 秒，逐值一致。可在下一批新发布上测试完整读取链路后再选择编码。当前冻结文件与 TOAST 压缩设置沿用发布时的值。
