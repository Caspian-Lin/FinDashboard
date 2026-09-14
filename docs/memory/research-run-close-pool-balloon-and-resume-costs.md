# 研究运行内存:close 池合并肥结果尖峰 + #314 大前缀续跑三重成本 + 投影/预取/arrow 表 + 决策段高水位

**主题**:全市场研究 run 的内存结构性问题全集——close 池 gather-all 尖峰(已修,#468)、#314 续跑通道 O(prefix) 三重成本(前半场已修 #470)、无用户因子 run 的投影/预取浪费(已修)、factor_series arrow 表常驻(已修,#470 后半场)、预计算进程池决策段空转(已修)、daily 预计算全期常驻(已修,#438 v2)、决策段 ~2GB pymalloc 高水位(待修,归因已闭合)。

**结论 / 事实**(2026-09-14 活体六次重启对照,556 期全历史 5215 标的 run):

- close 池合并 `asyncio.gather` 攒全量列式肥结果(每标的 ≈0.7MB)→ 主进程瞬时 +3.5GB;有界在途(信号量 64 + as_completed 逐个转换)后尖峰 4.36→0.68GB。**单点低频 RSS 采样会把尖峰+回落伪装成台阶**。
- #314 大前缀(272 期)续跑三重成本:`list_artifacts` 全量物化(~8GB,arena 残留全程)、种子 bundle 四处引用钉死、`aprefetch_inputs` 缓冲 273 期完整输入(期均数十 MB)。修复 = 读回流式化 + 种子破坏性消费 + `_ResumeReplayStub` 五字段瘦记录(lot_info/prices/execution_prices/business_date/decision_at 是 resume 校验与账本重放的**全部**消费面);种子被拒的全量重算经**流重建**兜底(预取已消费流,回放不可能)。
- 无 `u_` 用户因子的 run,factor screen 恒为 None,但横截面投影仍每期驻留 `others` 全特征截面(~15MB × 556 期 ≈ 8GB)——manifest canonical JSON 扫描 `"u_..."` 门控,当期出现 u_ 观测时逐期兜底回退全量。
- **`LazySeriesValues._ensure_table` 把每条 factor_series parquet(盘上 10-15MB)读成常驻 arrow 表 ~365MB**(4 条 ≈1.5GB);arrow 缓冲不参与 GC 追踪,**gc 普查看不见**,是历次「说不清的台阶」的共同盲区。已修(#470 后半场,2026-09-14):首访一次性抽成紧凑 numpy(date int32 + symbol 字典索引 int32 + value float64/null 掩码 ≈17B/行)后丢 arrow 表;null 语义经掩码保留不与 NaN 混同。
- **预计算进程池决策段空转 1.3GB**:8 spawn worker 各 ~160MB 存活期 = 整个生成器作用域,而 multi_period 的池只在预计算段有消费方。已修:`PriceFeatureProcessPool.retire()`(区别于 mark_broken 故障语义)在 `precompute done` 帧后即刻 retire+aclose,降级路径逐值一致。
- **daily 预计算 per-symbol (n_periods×n_cols) 矩阵 run 级常驻 0.45GB/发布**:消费端每期每标的只取一行。已修(#438 v2):构建期散转进逐期槽位(行=标的),分块 gather 完成后逐期真释放(numpy 大数组 >512KB 走 VirtualAlloc,释放即归还 OS);available_at/source memo 去重(~2.9M 串对象 → 每期 1 个);被释放期复读回落逐期路径,值语义不变。
- **决策段高水位归因已闭合(2026-09-14 进程内 tracemalloc + gc 对照,24 期)**:RSS 前期爬到 ~5GB 后**斜率归零**;tracemalloc 活字节 0.08GB(无对象泄漏)、traced 峰值 0.42GB(每期序列化 churn 波)。构成 ≈ 真驻留 2.5GB(close 0.35 + price 0.17 + 4 条全历史序列紧凑化后 ~1.0 + 协方差分块窗 4×~0.1-0.16 + 瘦身 decisions 列表候选池) + **pymalloc 高水位 ~2GB**(0.4GB churn 波 × 反复拍打 + 与长活分配交错碎裂)。剩余 <3GB 的三块石头:①特性/宇宙产物序列化流式化(砍 churn 波,字节级等值要求高)②decisions 列表候选池保留(#463 明确不放松,#314 构造不变量,需 report-from-artifacts 化)③序列文件化(mmap 后 RSS 仍计 page cache,收益存疑)。 tracemalloc 不追踪 numpy 数据缓冲——numpy 侧归因只能靠分相位 RSS + 组件点名。
- loader 层本身是干净的(离线受控复现 40 期,每期驻留 ~1.6MB)——「每期几十 MB」的累积全在 signal_engine/pipeline/持久化包装层。
- numpy/arrow 缓冲均不可见 gc 普查与 tracemalloc;内存归因别用 gc 普查找数组,用分相位 RSS 轨迹 + 受控复现差分 + 已知结构逐组件点名。
- **tracemalloc 必须在预计算后启动**(挂 precompute 前,分配重段慢 2-5×,实验 20 分钟进不了决策段);psycopg 异步脚本要 `loop_factory=SelectorEventLoop`;worker 租约 10 分钟窗,强杀后重启要等 lease 过期才收敛(#306),轮询 `stale_skip_owned_job` warning 判断。

**Why**:这些形态都藏在观测盲区:watcher 只采主进程单点 RSS、gc 普查看不见 arrow/numpy、resume 固定开销只在真重启时付、投影浪费只在高 symbol 数日期(2020+)显著。不造受控复现就永远只看到「说不清的台阶」。

**How to apply**:
1. 池分发/流式包装新增消费形态时,先问「结果对象全量攒住会多大」——肥结果必须流式转换;给包装层(捕获/预取/缓存)加容器前先审计**全部下游消费字段**,只留消费面。
2. 重启大 run 前看已完成决策数:>200 期评估 resume 税(修复后 ≈ precompute + 分钟级,修复前 35 分钟 + ~14GB)。
3. 离线归因脚本模式:`.tmp/diag/pool_mem2.py`(分相位 + 进程树 RSS)/`pool_mem6.py`(组件点名:close/daily/price/序列/池子进程)/`pool_mem8.py`(进程内真 coordinator + tracemalloc 活字节对照);spawn worker 的脚本必须有 `__main__` 守卫(否则 8 worker 递归重跑实验,树 RSS 18GB 假象);psycopg async 脚本必须 SelectorEventLoop。
4. py-spy attach 生产 worker 后若异常死亡,先怀疑采样器(本案 01:36 死亡无法证实/证伪);采样期尽量短 + nonblocking。
5. 采样器别用 wmic(Win11 已移除)、别在 PowerShell -Command 里内嵌 `$_`(Git Bash 会吞);用 psutil process_iter + cmdline 匹配。
