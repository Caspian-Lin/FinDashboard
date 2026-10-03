# FinDashboard 课题组织与已有证据归档

2026-10-03，#500。课题是可持续更新的研究锚点：一个问题、当前目标版本、工作摘要、开放问题、下一步，以及历次尝试的证据目录。它可跨会话暂停、分支和续接，不等于一次 ResearchRun，也不是一次性的报告。

## 关联语义

课题通过追加轮次的 `source_refs` 组织运行、研究记忆、实验、精确策略版本和文档。一个产物可以被多个课题引用。原来的运行、记忆或实验仍有原 ID、状态、创建时间和内容；关联不修改冻结输入、checksum 或记忆纠正链。

因此，“把已有证据归入新课题”是创建课题并追加证据引用，不是将所有产物改写成唯一的 topic_id。发现误关联时追加 `supersedes_id` 纠正记录，旧记录仍留审计。研究记忆正文有错误时才走原 `finboard_memory_correct`，不能为了归类而改写旧记忆。

当前目录保存在轮次引用中，`finboard_topic_read(entries=true)` 分页读取；尚无单独的聚合目录/反向归属 API。课题工作结论与自动产物事实、canonical 文档分别保留，不能把“归档动作完成”当作“策略通过验证”。

## Agent 操作步骤

1. 读 `ROADMAP.md`、`FINDINGS.md` 和相关记忆，核对目标来源；先 `finboard_topic_read` 分页检查是否已有相同问题，优先续接，避免重复建课题。
2. 用 `finboard_memory_page` 读摘录及历史状态/纠正链，必要时用 `finboard_memory_get` 获取选定正文。查询已有运行/实验，选出真正相关的精确 ID。历史数字、旧故障和未支持的假设不自动成为当前事实。
3. 对选定引用调用 `finboard_source_check`。策略补精确 version，checksum 有则传；未知种类、缩写 ID、断链或版本缺失不编造补齐。实验先读 `oos_outcome`，再读流程 `status`。
4. `finboard_topic_write(operation=create)` 新建，保存返回的 RT-ID、revision 和目标 version。已有课题则跳过此步。
5. `finboard_topic_write(operation=append)` 使用 `entry_type=evidence` 归档已有证据；记录为什么有关、哪些仍缺证据，以及原记忆的历史日期/状态。每批最多30个引用，超过时分批，各用稳定幂等键。
6. `finboard_topic_read(topic_id=...,entries=true)` 验证归档；返回 has_more 时按 offset 续页，不只读第一页就声称目录完整。选择与本次问题有关的轮次，不把全量长文塞入上下文。
7. 后续真实研究追加 `entry_type=round`；目标或工作摘要更新用 `operation=update`，先读最新 revision。目标内容改动必须使用新 goal.version，不能覆盖旧门槛。同版本无关摘要仍可更新。

写操作受 MCP readonly_only 拒绝，创建者由服务器固定；创建/追加/更新课题都不启动回测、模拟或实盘。

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

修改摘要/状态/下一步时，将第1步的完整输入形状作为 payload，传 `operation=update`、topic_id 和读取得到的 `expected_revision`。**不要把读取响应中的 topic_id/revision/created_by/created_at/evidence_level 混入 payload**。发生版本冲突先重新读取和合并，不盲目覆盖；改变 criteria/source 时必须改 goal.version。

每轮收尾同时保留课题记录与原研究记忆指针。正式结论依旧经 PR 进入 docs/research，研究 agent 不写只读挂载文件、不自动推送文档 PR。
