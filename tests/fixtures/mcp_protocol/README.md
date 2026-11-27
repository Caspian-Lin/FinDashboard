# MCP 协议夹具(#519)

这里锁定 FinBoard MCP 的**协议形状**,不锁定业务数据、也不锁定工具总数。

| 文件 | 锁定内容 | 何时需要更新 |
|---|---|---|
| `envelope.schema.json` | 统一响应信封 `ToolEnvelope` 的字段、`status` / `error.kind` 枚举、`OP-` ID 形态 | 改 `finboard_mcp/envelope.py` 的信封 / 错误分类 |
| `tool_input_schemas.json` | 少量**入口工具**的调用参数 JSON Schema 快照(topic_read / topic_write / source_check / run_list / run_queue / job_wait) | 改对应工具的入参签名 |

约定:

- **不写死工具总数**:工具清单与作用域的唯一事实源是
  `finboard_mcp.scopes.TOOL_SCOPES`(与 `build_mcp_server().list_tools()` 集合
  相等,由 `tests/unit/test_mcp_server_contract.py` 锁定)。#516 等后续 issue 新增
  工具不会让这里失败,除非它改了上表工具的入参。
- 夹具只覆盖**协议契约**;权限矩阵回归(只读客户端不得调用写工具)在
  `tests/unit/test_mcp_server_contract.py::TestPermissionMatrixRegression`,按
  `TOOL_SCOPES` 全量参数化,不引用本目录。
- 刷新方式:`uv run python .tmp/dump_protocol.py`(开发脚本,不随仓库维护)或按
  `tests/unit/test_mcp_protocol_fixtures.py` 的失败信息逐项更新;提交前确认差异
  是**有意的契约变更**,并在 PR 描述里写明。

来源:手工导出自 `build_mcp_server().list_tools()`(2026-11-10,#519)。
