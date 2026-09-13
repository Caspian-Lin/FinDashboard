# 研究运行内存:close 池合并肥结果尖峰 + #314 大前缀续跑三重成本 + 投影/预取/arrow 表

**主题**:全市场研究 run 的内存结构性问题全集——close 池 gather-all 尖峰(已修,#468)、#314 续跑通道 O(prefix) 三重成本(前半场已修 #470)、无用户因子 run 的投影/预取浪费(已修)、factor_series arrow 表常驻(#470 后半场待修)。

**结论 / 事实**(2026-09-14 活体六次重启对照,556 期全历史 5215 标的 run):

- close 池合并 `asyncio.gather` 攒全量列式肥结果(每标的 ≈0.7MB)→ 主进程瞬时 +3.5GB;有界在途(信号量 64 + as_completed 逐个转换)后尖峰 4.36→0.68GB。**单点低频 RSS 采样会把尖峰+回落伪装成台阶**。
- #314 大前缀(272 期)续跑三重成本:`list_artifacts` 全量物化(~8GB,arena 残留全程)、种子 bundle 四处引用钉死、`aprefetch_inputs` 缓冲 273 期完整输入(期均数十 MB)。修复 = 读回流式化 + 种子破坏性消费 + `_ResumeReplayStub` 五字段瘦记录(lot_info/prices/execution_prices/business_date/decision_at 是 resume 校验与账本重放的**全部**消费面);种子被拒的全量重算经**流重建**兜底(预取已消费流,回放不可能)。
- 无 `u_` 用户因子的 run,factor screen 恒为 None,但横截面投影仍每期驻留 `others` 全特征截面(~15MB × 556 期 ≈ 8GB)——manifest canonical JSON 扫描 `"u_..."` 门控,当期出现 u_ 观测时逐期兜底回退全量。
- **`LazySeriesValues._ensure_table` 把每条 factor_series parquet(盘上 10-15MB)读成常驻 arrow 表 ~365MB**(4 条 ≈1.5GB);arrow 缓冲不参与 GC 追踪,**gc 普查看不见**,是历次「说不清的台阶」的共同盲区。紧凑化(读后转 numpy 列、弃 arrow 表)在 #470 后半场。
- loader 层本身是干净的(离线受控复现 40 期,每期驻留 ~1.6MB)——「每期几十 MB」的累积全在 signal_engine/pipeline/持久化包装层。
- numpy/arrow 缓冲均不可见 gc 普查;内存归因别用 gc 普查找数组,用分相位 RSS 轨迹 + 受控复现差分。

**Why**:这些形态都藏在观测盲区:watcher 只采主进程单点 RSS、gc 普查看不见 arrow/numpy、resume 固定开销只在真重启时付、投影浪费只在高 symbol 数日期(2020+)显著。不造受控复现就永远只看到「说不清的台阶」。

**How to apply**:
1. 池分发/流式包装新增消费形态时,先问「结果对象全量攒住会多大」——肥结果必须流式转换;给包装层(捕获/预取/缓存)加容器前先审计**全部下游消费字段**,只留消费面。
2. 重启大 run 前看已完成决策数:>200 期评估 resume 税(修复后 ≈ precompute + 分钟级,修复前 35 分钟 + ~14GB)。
3. 离线归因脚本模式:`.tmp/diag/pool_mem2.py`(分相位 + 进程树 RSS)/`pool_mem4.py`(逐期 census 差分);spawn worker 的脚本必须有 `__main__` 守卫(否则 8 worker 递归重跑实验,树 RSS 18GB 假象)。
4. py-spy attach 生产 worker 后若异常死亡,先怀疑采样器(本案 01:36 死亡无法证实/证伪);采样期尽量短 + nonblocking。
