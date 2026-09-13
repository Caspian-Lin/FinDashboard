# Issue #117 状态（统一持久化后台任务队列）

**主题**:#117(建立统一持久化后台任务队列 `background_jobs`)的 5 个 sub-issue
功能层面**全部交付并合并**(截至 2026-08-13):#142(PR #145 基础设施)、#143
(PR #146 research-run 迁移)、#144(PR #147 数据类任务迁移)、#136(PR #149 MCP
任务队列工具)、#148(PR #150 前端 JobOut 契约适配)。但 **#117 本身尚未关闭**。

**Why**:用户询问 #142-144/136/148 完成后 #117 是否全部完成;核对后发现是
「功能完成但文档 + 基准测试收尾未做」。

**How to apply**(收尾 #117 需补 3 项,或新开收尾 issue):

1. **README 未文档化统一队列契约**(8 态状态机/轮询/取消语义)—— 代码注释有,
   README:260-261 描述的是 ResearchRun 7 态,不是 background_jobs 8 态。
2. **README 未文档化 Worker 启动方式** —— `finboard worker run [--poll-interval]
   [--max-concurrent] [--queues]` 已在 cli.py 实现,但 README CLI 子命令清单漏了
   `worker`,Makefile 也无 worker target。
3. **缺 5k 标的批量任务 API 健康响应基准测试** —— 全仓无 5000/5k/benchmark/p95
   相关测试(注:2026-08-15 已见 `tests/integration/test_background_jobs_api_responsiveness.py`,
   该项可能已落地,收尾时核对)。

次要缺口:backtest_run 无完整 executor E2E(只有 payload 单测)、无专门
「API 重启期间 worker 仍跑」故障注入(lease/crash 恢复在 worker 层已覆盖)。

相关:[[issue-137-status]](#137 的 blocked-by 已满足)。
