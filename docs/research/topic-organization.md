# FinDashboard 课题组织与已有证据归档

2026-10-03，#500。课题是可持续更新的研究锚点：一个问题、当前目标版本、工作摘要、开放问题、下一步，以及历次尝试的证据目录。它可跨会话暂停、分支和续接，不等于一次 ResearchRun，也不是一次性的报告。

## 关联语义

课题通过追加轮次的 `source_refs` 组织运行、研究记忆、实验、精确策略版本和文档；`#515` 起课题还经不可变的 `research_topic_links` 显式关联 ResearchRun（从课题发起的运行自动建链，已存在的独立运行可后来关联）。一个产物可以被多个课题引用。原来的运行、记忆或实验仍有原 ID、状态、创建时间和内容；关联不修改冻结输入、checksum 或记忆纠正链。

因此，“把已有证据归入新课题”是创建课题并追加证据引用，不是将所有产物改写成唯一的 topic_id。发现误关联时追加 `supersedes_id` 纠正记录，旧记录仍留审计。研究记忆正文有错误时才走原 `finboard_memory_correct`，不能为了归类而改写旧记忆。

当前目录保存在轮次引用与运行链接中，`finboard_topic_read(entries=true)` 分页读取；`view=branches` 看分支聚合，`view=refs` 逐条核验断链，`view=context` 一次取回有界续接上下文（含 `active_links`）。`#515` 起用 `finboard_topic_run_list` 读课题运行、`finboard_run_topics` 反查一个 run 被哪些课题引用；关联元数据独立于 manifest，不写回运行或轮次。课题工作结论与自动产物事实、canonical 文档分别保留，不能把“归档动作完成”当作“策略通过验证”。

## 生命周期与两步建题（#514）

```text
draft --activate(需目标)--> active
active --pause--> paused --resume--> active
active|paused --close(原因)--> closed --reopen(原因)--> active
draft --discard(原因)--> closed
```

1. 先 `finboard_topic_write(operation=draft_create)` 只保存 title/question，得到稳定 `RT-`；草稿没有目标版本，`goal=null`。建题可带 `idempotency_key`，回执丢失后用同一键重试返回原课题（`created=false`），不会重复建题。
2. 核对来源后 `operation=activate`，传 `topic_id`、`expected_revision` 与 `payload.goal{version,criteria,source}`，可同时补 summary/open_questions/next_step。目标必须含版本、门槛和可核验来源；已激活且目标相同是幂等成功，目标不同会被拒绝，不能借激活覆盖目标。
3. 日常用 `operation=transition`：`payload.action=pause|resume|close|reopen|discard`，close/reopen/discard 必须写原因。每次转移都要求 `expected_revision`，服务端加锁比对并追加 `entry_type=lifecycle` 轮次；非法转移返回 `conflict`（附 `allowed`/`current`），版本冲突附 `current_revision`/`changed_fields`。
4. 被放弃的草稿没有目标版本，不能直接重开；需要继续时新建课题并补齐目标。

保存类操作（草稿、激活、转移、追加轮次）都不会启动回测、模拟或实盘，也不会改写历史轮次与目标快照。跨会话续接优先读 `view=context`：它包含当前目标、最近生命周期、开放问题、最近轮次摘录、断链引用与 `next_actions`（服务端派生建议，不是执行），载荷 ≤64 KiB，截断时 `excerpted=true` 且 `truncated_sections` 具名。

## Agent 操作步骤

1. 读 `ROADMAP.md`、`FINDINGS.md` 和相关记忆，核对目标来源；先 `finboard_topic_read` 分页检查是否已有相同问题（可用 `status` 过滤、`sort=updated|created`），优先续接，避免重复建课题。跨会话恢复时直接 `finboard_topic_read(view=context)` 取回目标、最近轮次与断链。
2. 用 `finboard_memory_page` 读摘录及历史状态/纠正链，必要时用 `finboard_memory_get` 获取选定正文。查询已有运行/实验，选出真正相关的精确 ID。历史数字、旧故障和未支持的假设不自动成为当前事实。
3. 对选定引用调用 `finboard_source_check`。策略补精确 version，checksum 有则传；未知种类、缩写 ID、断链或版本缺失不编造补齐。实验先读 `oos_outcome`，再读流程 `status`。断链可用 `finboard_topic_read(view=refs)` 按最近轮次批量复核。
4. `finboard_topic_write(operation=draft_create)` 先存问题；已有课题则跳过。随后 `operation=activate` 补齐目标、门槛与来源，保存返回的 RT-ID、revision 和目标 version。
5. `finboard_topic_write(operation=append)` 使用 `entry_type=evidence` 归档已有证据；记录为什么有关、哪些仍缺证据，以及原记忆的历史日期/状态。每批最多30个引用，超过时分批，各用稳定幂等键。
6. `finboard_topic_read(topic_id=...,entries=true)` 或 `entry_id=RE-...` 精确读取单条全文与 checksum（跨课题 ID 返回 404）验证归档；返回 has_more 时按 offset 续页，不只读第一页就声称目录完整。选择与本次问题有关的轮次，不把全量长文塞入上下文。
7. 后续真实研究追加 `entry_type=round`；目标或工作摘要更新用 `operation=update`，先读最新 revision。`update` 只改内容、**不能改状态**（显式传不同 status 会被拒绝，须走 `transition`）；草稿可用 update 编辑 title/question/summary/open_questions/next_step，但不能带 goal（须走 `activate`）。目标内容改动必须使用新 goal.version，不能覆盖旧门槛。暂停/继续/收束/重开用 `operation=transition` 并写原因。
8. #516 起课题条目分三层：`entry_type=plan` 只记录设计（因子公式/窗口/方向/预注册假设/试验预算），不执行且 `outcome` 必须 `completed`；`entry_type=round` 记录执行后的尝试；`entry_type=review` 记录复核结论并**必须**带 `document_ref`（**相对 `docs/research`** 的 `.md` 路径，如 `ROADMAP.md` / `rounds/2026-11-09-x.md`，+ sha256 checksum），证据等级为 `reviewed_conclusion`。真正执行只走 `finboard_topic_action`（白名单动作，见下），保存计划/复核不会创建任何 run 或 job。

## 课题内执行动作（#516）

`finboard_topic_action` 与 REST `POST /api/research/topics/{id}/actions` 同源：`factor_experiment.create`（登记因子实验，不执行）、`code_run.execute`（只执行已登记 `research_code_artifacts`，commit 服务端固定）、`validation_experiment.create` / `add_trial`（元数据写）、`validation_experiment.unseal`（一次性揭盲入队）、`run.submit` / `run.replay`（委托 #515）。共同前置是课题 `active` 且 `goal_version` 匹配；每个动作在同一事务写产物/任务 + 不可变 `research_topic_links` + 研究审计，`operation_id` 可找回原链接（跨课题冲突）。

```json
{"topic_id":"RT-<ID>","action":"code_run.execute","goal_version":"<当前目标版本>","operation_id":"OP-<唯一>","payload":{"kind":"factor","artifact_id":"RC-<已登记>","dataset_release_ids":["<发布ID>"],"decision_at":"2024-06-03T15:00:00+08:00"}}
```

**不要**在 payload 里传 Python、模块路径或表达式（服务端直接 422 `code_channel_forbidden`）；代码只经 `finboard_research_code_submit` 登记。已揭盲实验再次 `unseal` 返回 conflict（`already_unsealed`），`final_test_unsealed` 不会被改写。分支/复核续接用 `finboard_topic_read(view=context|artifacts)`：OOS 先读 `oos_outcome`，再看 `status`、`trial_budget{used,limit}`、`final_test_unsealed` 与 `evidence_gaps`。

写操作受 MCP readonly_only 拒绝，创建者由服务器固定；创建/追加/更新/转移课题都不启动回测、模拟或实盘。版本冲突先重新读取并合并，Web 侧可“仅保留我的说明”追加轮次，而不是覆盖对方内容。

## 完整调用示例

以下是组织流程示例，**占位 ID 必须替换为核查所得完整 ID**；criteria 是复核任务说明，不代替用户确认的数字门槛。现有课题可从第2步继续。

### 1. 创建课题

调用 `finboard_topic_write`：

```json
{
  "operation": "create",
  "payload": {
    "title": "小盘反转可信度复核",
    "question": "收益能否在同源 OOS 与合理执行压力下存活？",
    "goal": {
      "version": "review-2026-10-03-example",
      "criteria": "核对冻结输入、同源OOS与真实压力证据；数字门槛以本次明确约定版本为准",
      "source": {"kind": "document", "ref_id": "ROADMAP.md"}
    },
    "status": "active",
    "conclusion": "unknown",
    "summary": "先整理历史证据，尚未完成本次复核",
    "open_questions": ["当前正式策略同源OOS是否齐备", "成本与延迟压力是否真正执行"],
    "next_step": "核查并关联选定的历史运行、记忆与实验"
  }
}
```

### 2. 核查引用

分别调用 `finboard_source_check`，确认返回 matched 后仍须阅读 facts，存在不代表假设获支持：

```json
{"kind":"research_run","ref_id":"RR-<完整运行ID>"}
```

```json
{"kind":"memory","ref_id":"RM-<完整记忆ID>"}
```

```json
{"kind":"experiment","ref_id":"<完整实验ID>"}
```

### 3. 追加已有证据

调用 `finboard_topic_write`，topic_id 用创建返回值，goal_version 必须是该课题已归档目标版本。只添加已核查引用；实验尚未找到时删除对应项，并写明缺口。

```json
{
  "operation": "append",
  "topic_id": "RT-<创建返回ID>",
  "idempotency_key": "historical-evidence-v1-batch-001",
  "payload": {
    "entry_type": "evidence",
    "goal_version": "review-2026-10-03-example",
    "objective": "组织已有运行、记忆与验证实验",
    "action": "分页查找并核查相关ID，记录历史证据引用",
    "rationale": "让后续会话从稳定课题续接，保留原始产物与历史门槛",
    "outcome": "completed",
    "conclusion": "关联动作完成；旧记忆为历史解释，当前可信度尚未复核",
    "confidence": "unknown",
    "next_step": "核对每项证据的策略版本、数据发布与OOS结论",
    "source_refs": [
      {"kind":"research_run","ref_id":"RR-<完整运行ID>"},
      {"kind":"memory","ref_id":"RM-<完整记忆ID>"},
      {"kind":"experiment","ref_id":"<完整实验ID>"}
    ],
    "branch": "historical-evidence"
  }
}
```

同键重试必须保持 payload 完全一致。变更关联集合或解释应换新幂等键；纠正同课题旧条目时补 `supersedes_id`，不删除旧条目。

### 4. 读取和持续更新

调用 `finboard_topic_read`：

```json
{"topic_id":"RT-<已有ID>"}
```

```json
{"topic_id":"RT-<已有ID>","entries":true,"limit":20,"offset":0}
```

修改摘要/下一步时，将第1步的完整输入形状作为 payload，传 `operation=update`、topic_id 和读取得到的 `expected_revision`。**PUT 不能改变状态**：显式带不同 status 返回 conflict，先重新读取再用 `transition`；草稿 PUT 只接受 title/question/conclusion/summary/open_questions/next_step，带 goal 会被拒绝（走 `activate`）。**不要把读取响应中的 topic_id/revision/created_by/created_at/evidence_level 混入 payload**。发生版本冲突先重新读取和合并，不盲目覆盖；改变 criteria/source 时必须改 goal.version。

每轮收尾同时保留课题记录与原研究记忆指针。正式结论依旧经 PR 进入 docs/research，研究 agent 不写只读挂载文件、不自动推送文档 PR。
