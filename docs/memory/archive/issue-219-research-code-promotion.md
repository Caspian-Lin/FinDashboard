# Issue #219:研究代码验证门与晋级链路

## 已落地事实

- `finboard_research_code_submit` 和历史回滚只创建 `draft`，并写入
  `promotion_status=pending`；失败门证据保留为 `draft/failed`，不会替换当前
  正式版本。
- `finboard_research_code_promote` 是唯一正式晋级入口。它要求同一
  `artifact/name/kind/commit` 的 screen 运行和 #57 实验；实验必须是
  `validated_oos` 且 `final_test_unsealed=true`。screen 默认要求绝对 rank IC
  不低于 0.02、平均换手率不高于 0.80、绝对相关性不高于 0.80，至少 2 期。
- 只有 `status=active` 且 `promotion_status=passed` 的代码可被因子目录、用户
  策略编译/入队、沙箱执行默认路径和模拟盘创建入口消费。active 新版本晋级
  会自动退役同名旧版本；retired、draft 和 failed 引用均须 fail-visible。
- 晋级 evidence 与沙箱报告保存代码 commit、数据发布及 checksum、参数及
  checksum、输出 checksum 四向引用，并保存镜像 digest、容器日志、退出码和
  资源归档位置。`VersionStamp` 的代码绑定字段经过数据库序列化往返保留。

## 关键实现位置

- `packages/finboard-backtest/src/finboard_backtest/research_code/promotion.py`：
  screen + OOS 纯函数门控、阈值和可审计结果。
- `packages/finboard-mcp/src/finboard_mcp/tools/research_code.py`：提交、回滚、
  晋级 MCP 入口及证据聚合。
- `packages/finboard-persistence/src/finboard_persistence/research_code_repo.py`：
  draft/active/retired 生命周期与晋级证据持久化。
- `migrations/versions/f0a1b2c3d4e5_research_code_promotion_219.py`：晋级字段、
  索引和既有 active 数据兼容回填。

## 边界

研究代码只能进入正式研究组合和独立 `simulation_*` 模拟盘；模拟盘不导入
broker，不自动晋级影子盘或实盘。本 issue 不改实盘下单、持仓恢复或风控逻辑。

## 验证注意

仓储生命周期集成测试需要先应用 #219 migration；对已有测试库只执行
`create_all` 不会自动增加列。沙箱容器 E2E 仍由既有 `FINBOARD_SANDBOX_E2E=1`
开关控制。
