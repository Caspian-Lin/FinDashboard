---
name: issue-190-mcp-tool-usability
description: MCP 回测/入队工具易用性修复 — factor_version 与 framework_version 双命名空间、grid_get 默认瘦身、run_queue payload 模板(issue #190)
metadata:
  type: project
---

# Issue #190:MCP 工具易用性修复(枚举提示 / 默认返回体 / payload 模板)

OpenCode 研究 agent 实测反馈的三个误导性 UX 缺陷(P2),2026-08-18 修复:

1. **`selection.factor_version` 报错列合法枚举**:`FactorSelectionConfig.__post_init__`
   (`packages/finboard-data/src/finboard_data/factors.py`)失败文案现含合法值
   `[v1]`。语义澄清:**`factor_version`(选股规则目录版本,`FACTOR_VERSION`=v1)
   与特征快照 `framework_version`(`FACTOR_LAB_SCHEMA_VERSION`=v2,快照 schema
   版本)是两个独立命名空间**——agent 看到快照 `framework_version:"v2"` 误传入
   `factor_version` 是主要误用;文档(类 docstring / backtest_run 工具描述 /
   `_INSTRUCTIONS` / Skill `tools.md`)已标注不要混传。
2. **`finboard_backtest_grid_get` 默认不返回 equity 曲线**:default
   `equity_mode="none"`,只保留 `equity_point_count` 点数提示;显式
   `summary`/`full` 才返回曲线。`downsample.resolve_equity_mode` 现接受
   `none/summary/full`,`apply_equity_mode("none")` 返回 `[]`。9 组合 x 200 点约
   100-200KB 不再默认灌爆响应(此前 summary 默认仍易被 MCP 客户端截断)。
3. **`finboard_run_queue` 描述附完整 payload 模板 + 字段取值来源**:
   `code_version` 澄清为**本 run 自身的冻结字段**(7-64 字符,任意字符串可过,
   冻结进 manifest/checksum 供追溯),**与数据集发布的 code_version 同名但互不
   校验**——agent 传数据集 git hash 只是碰巧通过,语义必须澄清。

**Why(关键决策):**
- `none` 用字符串字面量而非 `None` 默认值,让 JSON schema 明确枚举,并经
  `resolve_equity_mode` 统一校验,非法值路径与 summary/full 完全一致。
- `equity_point_count` 即使在 none 模式也保留:点数是一个 int,体积可忽略,
  却能提示 agent「如需曲线可显式请求」,避免误以为没有曲线数据。
- 只改 `grid_get` 默认;`backtest_run` / `history_get` / `report_backtest` 仍
  `summary` 默认——它们返回单条曲线(体积可控),网格才是 N 条曲线叠加的主
  压体积源。

**How to apply:**
- 新增带枚举校验的报错文案时,直接把合法枚举写进消息(`合法值仅 [...]`),
  让错误本身自解释,别只留工具描述。
- 工具默认返回体设计:「默认最轻、显式才重」——对比/列表类聚合工具优先默认
  `none` 保留少量提示字段,单对象查询仍可保留合理默认。
- 改 MCP 工具契约后按 #123 同步四件套:工具描述(`tools/*.py`)、`server.py`
  `_INSTRUCTIONS`、Skill `references/tools.md`(+ `SKILL.md` 工具表行)、
  `ROADMAP.md`;有单元测试断言默认返回体就同步改测试并新增断言。
