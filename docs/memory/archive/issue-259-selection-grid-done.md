# Issue #259:backtest 网格 selection_grid 选股维度组合展开完成(2026-09-02,PR #280 待合并)

## 主题

`finboard_backtest_grid_submit` 新增独立参数 `selection_grid`(与 `params_grid`
同构 `{selection字段: 值列表}`),做选股维度笛卡尔积(因子 × 窗口等),可单独
使用(纯选股扫描)也可与 params 侧(params_list/params_grid)做笛卡尔积;逐组合
过 `FactorSelectionParams` 校验;总组合数受 max_combos(20/硬上限 50)约束。
PR:#280(base `m/research-backtest`),CI 通过。

## 结论 / 事实

- **方案取舍**:选独立 `selection_grid` 参数,否决「params_grid 允许
  `selection.*` 键」——params 逐项经策略 `params_model` 校验
  (`_validate_backtest_params`),混入带点号键须在每条 params 链路前预拆分,
  回归风险高;独立参数让 params 校验链路零改动。
- **幂等 checksum 兼容**:`_combos_checksum` 只在 combos 携带 selection 键时
  才把它纳入指纹;无 `selection_grid` 时 label/combos 结构与历史字节级一致,
  旧网格升级后幂等重提交不误报 conflict。
- **执行端零改动**:worker 从 payload 重建 `BacktestRunRequest` 时本就校验
  selection,组合级 selection 进 payload 即全链路生效。

## Why

- JSON 列承载新字段时,checksum/label 这类「派生指纹」必须考虑旧数据兼容,
  否则升级即制造幂等冲突假阳性。
- 网格类工具的「维度扩展」优先做正交独立参数,不做键名魔法(前缀路由),
  校验边界清晰、文档好写。

## How to apply(下次如何应用)

- 给已有幂等提交类工具加新维度时:派生指纹(label/checksum)按「键存在才
  纳入」演化,旧输入哈希不变。
- 中文文案/注释里不要用全角乘号 `×`——ruff RUF001/RUF002/RUF003 会挂 CI,
  统一写 `x`。
- `backtest_grid_submit` 被 `tests/integration/test_backtest_grid_mcp_e2e.py`
  以显式 kwargs 直接调用:MCP 工具函数加新参数时给默认值 None,否则集成测试
  签名不兼容。
- #123 三处同步清单(本次实际改动):MCP `_INSTRUCTIONS`、Skill
  `references/tools.md`、`SKILL.md` 工具选择表,外加工具 description 本身。
