# 记忆索引

按 AGENTS.md「记忆（Memory）规范」维护：记忆文件统一存放在本目录，
本文件是唯一索引；禁止把记忆写入任何 coding 框架自有的路径。

归档记忆在 `archive/` 子目录：issue 已关闭、内容已被 GitHub PR/issue 与
AGENTS.md git 历史承载，仅留档备查；活跃记忆只保留「不读代码 / 不翻历史
就不知道」的环境、工作流与跨 issue 坑位。

## 活跃记忆

- [跨 issue 持久坑位速查](durable-gotchas.md) — 迁移号撞号/测试库 schema 漂移/kit 五处联动/ruff 坑/as_dict checksum 契约/job phase 隐性契约等一次性速查
- [dataset_sync 同步运营 runbook](dataset-sync-runbook.md) — 首次全量过夜跑（~5s/片×小时计）/增量每日一片；配额是进程内锁须 worker 单进程；daily 截面恒全市场；PIT 锚点有回归测试锁边界
- [WSL PostgreSQL 空闲自动关机](wsl-postgres-keepalive.md) — 本机集成测试卡住的根因；跑 DB 测试前须唤醒并保活 WSL VM
- [PostgreSQL 僵尸锁卡死测试的根因与防护](pg-lock-hygiene.md) — idle-in-transaction 僵尸事务让测试无限等锁；conftest 已设 lock_timeout，附 pg_stat_activity 排查命令
- [测试基建关键坑：pytest-timeout / SQLAlchemy URL / 守卫注入](test-infra-timeout-and-testdb.md) — thread 方式 os._exit 杀进程、pytest-asyncio 1.4 无内置超时、str(URL) 脱敏密码、循环闭包 late-binding
- [测试库 schema 漂移：create_all 不 ALTER 已有表](test-db-create-all-schema-drift.md) — 模型加列后测试库须手动 ALTER（跑 alembic 撞 DuplicateTable）；.env URL 提取用 Python 勿用 sed
- [test_releases.py 在 coverage 下偶发失败（非回归信号）](test-releases-flaky-under-coverage.md) — 并行发布/staging 清理两用例被 coverage tracer 放大竞态偶发失败；--no-cov 或重跑即过，勿误判为回归去改业务代码
- [本机 akshare 复现实验的 ProxyError 陷阱](akshare-em-proxy-error-repro-trap.md) — 系统代理对 push2his.eastmoney.com 偶发 ProxyError（真股票也失败），接口行为判定以源码+请求 URL secid 参数为准；集成测试一律 mock 网络
- [多 worktree 并行开发：本机落地参数与坑位（#289）](multi-worktree-parallel-dev.md) — junction 共享 data_cache/data_releases + 独立库/端口；删 junction 禁 rm -rf；vite 端口/代理可环境变量覆盖
- [里程碑分支别名与 dev/main 集成时点](milestone-alias-and-dev-integration.md) — 「m/opencode-research-mcp」是别名，实名 m/opencode-research-agent；2026-09-17 起 dev/main/里程碑三者同树，先后关系 git fetch + merge-base 现算不凭印象
- [OpenCode Web 工作台：SPA 状态与容器环境踩坑](opencode-web-state-and-home.md) — 项目/最近会话在浏览器 IndexedDB，服务端无法预置；HOME/XDG 决定文件选择器与数据落点；坏路径会话清理方法
- [OpenCode provider 持久化与模型目录合并](opencode-provider-persistence-and-model-sync.md) — UI 连接=auth.json（data 卷），config provider=仓库文件；agent 定义以 .opencode/agent/*.md 为准；UI 写配置落 ：ro 挂载必失，重启回落仓库冻结态
- [OpenCode finboard-researcher：bash 放行 + exa 搜索 MCP](opencode-agent-bash-exa-mcp.md) — 容器无 JS 运行时，local MCP 不可行只能 remote；exa 托管端点匿名可用；改权限须重启容器
- [tushare 因子清单调研：内部因子路线图](tushare-factor-roadmap-survey-20260909.md) — 202 因子 9 类全清单含公式；202≈60-70 算子组合；数据缺口三层；分批方案批次进度以 issue #391 为准
- [统一数据链路方案：拉取重构+数据类型全景+因子批次整合](unified-data-pipeline-plan-20260909.md) — ≤2000 积分全纳入；SyncSpec 数据集驱动统一框架（dataset_sync）；批次落地状态以 #391 批次地图为准
- [MCP run 摘要查询 13GB 内存飙升根因与修复（#478）](mcp-run-summary-db-aggregation-478.md) — view=summary 曾全量物化 7203 artifacts 顶到 13.27GB；修复=summarize_artifacts 库内有界聚合（::jsonb 归一化 + jsonb 函数只放 CASE THEN）；新增聚合查询禁走 list_artifacts 全量物化

## 归档记忆（archive/，issue 已关闭留档备查）

- [Issue #157 状态](archive/issue-157-status.md) — #66 端到端交付缺口修复已实现，PR #158 待合并；合并后需跑迁移 a9d4e6f8b1c2
- [Issue #117 状态](archive/issue-117-status.md) — 统一任务队列 5 个 sub-issue 已全部合并，但 #117 未关：还差 README 队列契约/Worker 启动文档 + 5k 基准测试
- [Issue #137 状态](archive/issue-137-status.md) — 数据写操作 MCP 工具（12 个）已合并；bulk_download_status 因 #117 已合并而下线
- [Issue #173：selection bars/snapshot 输入模式](archive/issue-173-bars-snapshot-selection.md) — required_datasets 按因子推导；同名异类 Repository 陷阱；集成测试库 create_all 不加新列需手工 ALTER
- [Issue #185：instrument 元数据回填贯通发布链路](archive/issue-185-instrument-metadata-backfill.md) — list_date/industry 从「无写入者」到 profiles 回填 + 发布兜底；发布单资产测试须显式 required_capabilities
- [Issue #186：universe 预检与候选池诊断](archive/issue-186-universe-precheck.md) — 静态预览只对确定性元数据判空；入队空池秒级失败；enqueue 创建路径 updated_at 懒加载 MissingGreenlet 已顺带修复
- [Issue #187：冻结发布多数据集联合发布](archive/issue-187-joined-releases-factor-snapshot.md) — daily_metrics/financial_indicators 从 research 表冻结 + 联合 bars 产出基本面因子快照；market_cap 须注册 catalog 因子
- [Issue #188：research_run 分阶段进度上报](archive/issue-188-research-run-stage-progress.md) — progress total 递增自修正（worker 只增不减）+ phase 命名 research_run:<stage>
- [Issue #189：backtest_run strategy 形态异步化](archive/issue-189-backtest-async.md) — run_async 命名（async 保留字）；工作量=标的不数×交易日，阈值 default 15000
- [Issue #190：MCP 回测/入队工具易用性修复](archive/issue-190-mcp-tool-usability.md) — factor_version 与快照 framework_version 双命名空间勿混传；grid_get 默认 none 瘦身
- [冻结发布校验和 DB 锚定（issue #202）](archive/issue-202-release-checksum-db-anchor.md) — manifest 完整性比对 DB release_checksum 不做代码敏感重算；删改 manifest 字段先迁 schema_version
- [Issue #203：multi_period 快照依赖文档与报错对齐](archive/issue-203-multiperiod-snapshot-docs-alignment.md) — 多期免快照仅限决策日推导与价格因子；single_shot 零快照入队秒级拒（共享门控函数）
- [Issue #204：factor_snapshots 原子 upsert](archive/issue-204-factor-snapshot-atomic-upsert.md) — ON CONFLICT 冲突方 RETURNING 为空须再 SELECT 复用；gather 复现不了 TOCTOU 须 Event 编排时序
- [Issue #205：回测 fills.date 取实际交易日](archive/issue-205-fills-date-trading-day.md) — Fill.filled_at 默认 _utcnow() 陷阱；收盘约定收敛到 clock.market_close
- [Issue #206：研究/回测 MCP 返回瘦身](archive/issue-206-mcp-response-slimming.md) — MCP SDK 会把 dataclass 返回注解重建为同名 pydantic 模型，信封须 BaseModel+model_serializer
- [Issue #214：因子目录收敛 v2 唯一事实来源](archive/issue-214-factor-catalog-unification.md) — FACTOR_CATALOG 从 FACTOR_LAB_CATALOG 投影生成；v1 名称缺失导入期 fail-loud
- [Issue #215：研究代码仓库与服务端提交工具](archive/issue-215-research-code-repo.md) — git 读命令必须显式 --git-dir；register 返回冻结快照
- [Issue #216：研究代码沙箱执行器](archive/issue-216-research-sandbox-run.md) — PIT=物理隔离（挂载生成端 fail-closed）；/out 用 bind 而非 tmpfs；镜像 tag↔kit 版本三处同步
- [Issue #217：沙箱因子接入选股管线](archive/issue-217-sandbox-factor-pipeline.md) — u_ 前缀全链路判据；as_dict 可选键 None 不输出保旧 checksum；NaN/覆盖率 0.5 阈值互补
- [Issue #218：沙箱策略代码接入 multi_period 回测](archive/issue-218-sandbox-strategy-backtest.md) — decide(ctx)→targets 逐决策日容器执行；direct_weights 不重缩放；空 targets=合法全现金
- [Issue #219：研究代码验证门与晋级链路](archive/issue-219-research-code-promotion.md) — submit/rollback 只建 draft；screen + #57 validated_oos/final_test_unsealed 晋级门
- [#233/#234：首次晋级闭环落地](archive/promotion-chain-closure-233-234.md) — OOS 执行任务化揭盲不可重做；screen 绑定声明放 spec payload；factor screen 需 ≥2 份 RCR 快照
- [第二轮 L3 E2E：验证实验三项阻断修复（#244）](archive/e2e-round2-validation-executor-fixes-244.md) — PBO 矩阵取 IS 竞争 trial 非顺序窗口；capital 预校验；异常兜底不替实验下 REJECTED
- [Issue #226：复合评分目录语义字段投影](archive/issue-226-scorer-catalog-projection.md) — 评分参数本地、假设/失效/来源/方向自 FACTOR_LAB_CATALOG 投影；EXPOSURE_ONLY 无方向映射导入期拒绝
- [Issue #253：multi_period 因子可用性入队门控](archive/issue-253-multi-period-feature-gate.md) — derived_features 冻结进 manifest（空值不序列化保旧 checksum）；门控查全部 identity 源非仅 FACTOR
- [Issue #254/#255：universe 评估域收窄与 research_db 选股 fail-visible](archive/issue-254-255-universe-domain-and-selection-gate.md) — explicit_symbols 收窄共用 explicit_symbol_domain；摄取 ≠ 发布，三层门控共用 repo 方法
- [Issue #256：指数标的数据链路入口](archive/issue-256-index-data-pipeline-done.md) — bars 主发布唯一→指数基准须与股票同份 mixed 发布；is_benchmark_only_instrument 双枚举点排除
- [Issue #259：backtest 网格 selection_grid 选股维度组合展开](archive/issue-259-selection-grid-done.md) — 独立 selection_grid 参数而非 selection.* 键；全角乘号 × 触发 RUF001-003；MCP 工具函数加参须给默认值
- [Issue #261：发布标的集来源三选一完成](archive/issue-261-release-symbol-source-done.md) — 入队期解析成具体 symbols 执行器零改动；mixed scope 门要求三类型齐备是 #184 遗留
- [Issue #385/#386：publish full_market 板块过滤 + 发布质量门口径拆分](archive/issue-385-386-publish-board-filter-quality-gate.md) — 920 系 64.8%「异常」=代码切换史早于 list_date，非坏数据；boards 过滤仅 full_market 生效与内联清单互斥
