# Issue #137 状态（数据写操作 MCP 工具）

**主题**:#137「feat(mcp): 数据写操作工具」已实现,**PR #152 待用户确认合并**(目标分支 `m/opencode-research-agent`)。2026-08-13 已合并(PR #152 → `m/opencode-research-agent`)。

**事实**:实现 12 个工具(原 issue 列 13 个,`bulk_download_status` 因 #117 已合并而下线
—— REST `GET /api/data/bulk-download/status` 已删除,状态查询走 `finboard_job_get`)。

工具分布:

- 5 个任务化(返回 job_id,走 `BackgroundJobRepository.create_or_get`,kind 白名单
  复用 #136 已含):`fetch_all` / `data_sync` / `bulk_download` / `quality_repair` / `dataset_publish`
- 1 个同步单标的拉取(`data_fetch`,不进队列)
- 2 个配置读写(`data_config_get` 只读 / `data_config_update` 写,文件 `data_config.json`)
- 4 个 ETF(`etf_sync` 默认 dry_run / `etf_batch_confirm` / `etf_update` / `etf_review_queue` 只读)

**关键决策**:

- 新建 `packages/finboard-mcp/src/finboard_mcp/tools/data_write.py`(不扩展 data.py,保持只读语义清晰)
- idempotency_key 公式与 REST 完全一致 → agent 与 REST 提交同一任务命中同一 job_id
- `requested_by` 用 `mcp:<kind>` 前缀(REST 是 `api:<kind>`,审计区分入口)
- 任务化工具的 `_job_out` / `_payload_checksum` 复用 `to_jsonable(JobOut.model_validate(row))`(与 jobs.py 同口径)
- 总工具数 86 → 98;`_INSTRUCTIONS` / `tools.md` / `SKILL.md` / `ROADMAP.md` 四处已同步(#123 规范)

相关:[[issue-117-status]](统一任务队列,#137 的 blocked-by 已满足)。
