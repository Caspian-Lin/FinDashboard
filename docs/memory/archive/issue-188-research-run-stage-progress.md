---
name: issue-188-research-run-stage-progress
description: research_run job 分阶段进度上报 — total 递增自修正语义与 phase 命名约定(issue #188)
metadata:
  type: project
---

# Issue #188:research_run 分阶段进度上报

`ResearchRunCoordinator.execute` 新增可选 `progress` 钩子(与 background_jobs 的
`ProgressCallback` 结构同构,不反向依赖 background_jobs),在 `_persist_decision`
逐 13 个 stage 与 `_persist_report` 逐段回调
`progress(done, total, "research_run:<stage>")`。worker 的 `finboard_job_get` /
`GET /api/jobs/{id}` 运行中即见 `research_run:<stage>` phase 与逐段
progress_done/total,可区分「正常计算」与「卡死」。高级入口:
`background_jobs/executors/research_run.py` 把 worker 回调原样透传给 Coordinator。

**Why(设计与踩坑):**
- `background_job_repo.update_progress` 对 `progress_total` 采用 **max 语义只增不减**,
  且 `progress_done` 被当前 total 封顶。因此进度 total 必须单调不减,否则 done 会被
  压回旧 total。
- 多期回放(issue #183)时决策总数 N 要跑完 adapter 才知道,无法执行前预知固定
  total。所以采用「随新决策被发现而递增」的自修正 total:第 i 段(0 基)total=
  `(i+1)×13`,done=`i×13+offset+1`;REPORT 段 total=done=`N×13+1`。单快照
  (single_shot,N=1)时 total 恒定 13,恰好精确。
- phase 命名 `research_run:<stage>(stage.value)`,REPORT 段用 `research_run:report`;
  终态仍由执行器回调 `research_run:<status>`(issue #188 前既有的 `1,1` 终态约定
  不变),前端/消费方只需透传渲染,无任何 phase 映射需同步。
- 进度上报是尽力而为可观测性:`_report_progress` 用 `contextlib.suppress(Exception)`
  吞掉非取消异常(`asyncio.CancelledError` 是 BaseException 不被吞,协作式取消照常
  透传),上报故障不改变运行状态机。由此进度回调频率从「仅 start/终态两次」升到
  「每次 decision 13+1 次」,也天然让取消检测更及时。

**How to apply:** 新增研究执行段(如新的 stage / 新 artifact 类型)时,进度单位要按
「(decision, stage) 计 1」补进 done/total 公式,并保持 total 单调不减;phase 沿用
`research_run:<stage>` 命名。写测试用 `InMemoryResearchRunStore` +
`DecisionSequenceAdapter` 直接断言回调序列(计数/phase/done-total),无需 PG;
worker 级"运行中可观察"测试用决策间 sleep 的慢速 adapter 留观察窗口后轮询
`background_jobs` 行。
