# 研究代码首次晋级闭环落地(#233/#234,2026-08-31)

## 主题

2026-08-31 完成 issue #233(验证实验执行任务化)与 #234(screen 用途
draft 产物显式绑定通道),L3 晋级链的两个结构性断口合拢,**首次晋级闭环
(factor/strategy 双通道)在集成测试里实测走通**。

## 结论 / 事实

- **#233**:新 kind `validation_experiment`(`ValidationExperimentExecutor`,
  worker 单并发,`max_attempts=1`)+ MCP `finboard_validation_experiment_run`
  (工具总数 124→125)。执行链 = run_in_sample(逐 trial 落库)→
  run_walk_forward → unseal_final_test;揭盲一次性门在**持久化层**把守
  (终态/已揭盲重复执行抛 `experiment_not_runnable`)。
- **#234 设计定稿与 issue 原文不同**:绑定声明放 **spec payload**
  (`ResearchStrategySpec.screen_artifact_bindings`),而非 run_queue payload。
  原因:编译期名单在**规格草稿创建/发布**时就拒绝 draft 因子引用,
  绑定必须先于 run_queue 到达编译层;run_queue 层(REST+MCP 共用
  `resolve_screen_bindings`)只做 DB 实绑校验与冻结。issue 里的
  "purpose=screen 标记"未单独实现 —— spec 声明绑定即 screen 语义,
  避免双事实源。
- **发现并修复 #218 遗留缺陷**:`freeze_user_code_commit` 冻结 commit 改变
  spec payload 后,REST/MCP 入队仍用库存版本 checksum 构造 manifest,
  `ResearchRunManifest.__post_init__` 校验必炸 —— 规格未显式声明 commit 的
  user_code 入队从未可行(#218 测试都直接构造 manifest 或在规格里带
  commit)。现按冻结后规格重算 checksum。
- **promote screen 门要求 n_periods≥2** ⇒ factor 通道 screen RR 必须
  single_shot + **至少两份不同 decision_at 的 RCR 快照**(single_shot 决策
  序列 = 全部冻结快照的 decision_at 排序去重);一份快照的 screen run
  必死于 `screen.n_periods_below_minimum`。
- 环境坑(重申):测试库 `findashboard_test` 的 research 表 schema 落后时,
  `create_all` 不补列 —— 需手动 DROP 过期表(**含 CASCADE 级联掉的外键**,
  本次 drop research_dataset_releases 把 factor_experiments 的 FK 一并删了,
  导致 #138 e2e 的外键保护断言失败,补 drop factor_experiments 才修好)。
  Windows 裸跑 python + psycopg async 会撞 ProactorEventLoop,调试脚本要
  在 pytest 内跑。

## Why

- 断口是链路级集成断点:每块 issue 单测全绿也挡不住「编不了 screen 规格 /
  跑不了 OOS / 兜不住 draft」的组合死锁;只有全链路集成测试能暴露。
- strategy_screen 的指标(≥5 标的重叠、每期非空权重、换手 ≤0.8)对 fake
  权重形态敏感;回测权益曲线必须含回撤(纯单调 → calmar=Infinity →
  Postgres JSON 列拒收 `Infinity` token)。

## How to apply

- agent 走首次晋级:submit → RCR(≥2 个 decision_at,显式 artifact_id)→
  规格声明 `screen_artifact_bindings`(u_ 因子 + 双快照 single_shot;
  strategy 为 user_code 规格)→ run_queue screen RR →
  `finboard_validation_experiment_run`(实验 version_stamp 四向绑定)→
  promote。
- 测试造数:确定性漂移价格轨迹(保 forward return 排序)+ 小幅相位震荡
  (保协方差非退化)+ 含回撤权益曲线(保 calmar 有限)。
- 下次改 `research_*` 域表结构后,直接 DROP 测试库对应表(连 CASCADE
  波及的表一起)再跑测试,别指望 create_all 迁移。
