# 研究运行内存:close 池合并肥结果尖峰 + #314 大前缀续跑三重成本

**主题**:全市场研究 run 的两个内存结构性问题——close 矩阵池分发的 gather-all 瞬时尖峰(已修,#468)与 #314 断点续算通道的 O(prefix) 成本(#470,未修)。

**结论 / 事实**(2026-09-14 实测,556 期全历史 5215 标的 run):

- `_load_close_histories_via_pool` 原实现 `asyncio.gather` 把全部标的的列式肥结果(每标的 4 列 × 全历史 ≈0.7MB Python 对象)攒在一个 list,5215 全部完成后才开始转紧凑 numpy → 主进程瞬时 +3.5GB(受控实验:close 阶段 RSS 尖峰 4.36GB)。此前生产观测的「precompute done 前后 +3.3GB 台阶」即此;**单点低频 RSS 采样会把尖峰+回落伪装成台阶**。
- 修复 = 有界在途(信号量 64)+ `as_completed` 逐个到达逐个转换,尖峰 0.68GB;语义保持:全部任务仍被消费、任一异常整体降级进程内路径、打断异常优先上抛。daily/price 池路径结果本身紧凑(gather-all 无害),未动。
- 大前缀(272 期)续跑实测:`store.list_artifacts` 一次性回读全量 payload(272 期 × 9.35MB JSON)物化 ≈8GB 且 arena 残留贯穿全 run(峰值 20-22GB);种子肥 bundle 被 runner 局部变量 / signal_engine 局部变量 / `_resume_bundles` 元组三处钉死(features 置空会使重持久化 checksum 漂移,构造契约也不允许);`aprefetch_inputs` 还要把整个前缀的期输入重新装配一遍(~2.5s/期)。三者和都与「已完成决策数」成正比——**越到 run 尾部重启越危险**,40GB 机器在 500+ 期重启会拒绝服务。→ issue #470。
- numpy 数值数组不参与 GC 追踪(`PyObject_GC_UnTrack`),`gc.get_objects()` 普查**看不见**常驻 ndarray——内存归因别用 gc 普查找 numpy,用 RSS 轨迹 + 分阶段差分。

**Why**:这三个形态都藏在大 run 的低频观测盲区里:watcher 只采主进程单点 RSS(池子进程、20s 间隔)、gc 普查看不见数组、resume 固定开销只在真重启时付。不专门造受控复现(进程内逐相位 RSS 采样 + 分阶段差分)就永远只看到「说不清的台阶」。

**How to apply**:
1. 给池分发路径加新消费形态时,先问「结果对象全量攒住会多大」——肥结果(全历史列式/逐期对象)必须流式转换,紧凑结果(矩阵)才可以 gather。
2. 重启大 run 前先看已完成决策数:>200 期先评估 #470 的 resume 税(固定 ~35 分钟 + 数 GB),必要时先修 #470 再重启。
3. 离线归因脚本模式见 `.tmp/diag/pool_mem2.py`(分相位计时 + 进程树 RSS 采样;注意 spawn worker 必须有 `__main__` 守卫,否则 8 worker 递归重跑实验本身,树 RSS 18GB 假象)。
