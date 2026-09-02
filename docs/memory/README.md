# 记忆索引

按 AGENTS.md「记忆（Memory）规范」维护:记忆文件统一存放在本目录,
本文件是唯一索引;禁止把记忆写入任何 coding 框架自有的路径。

- [Issue #157 状态](issue-157-status.md) — #66 端到端交付缺口修复已实现,PR #158 待合并;合并后需跑迁移 a9d4e6f8b1c2
- [Issue #117 状态](issue-117-status.md) — 统一任务队列 5 个 sub-issue 已全部合并,但 #117 未关:还差 README 队列契约/Worker 启动文档 + 5k 基准测试
- [Issue #137 状态](issue-137-status.md) — 数据写操作 MCP 工具(12 个)已合并;bulk_download_status 因 #117 已合并而下线
- [WSL PostgreSQL 空闲自动关机](wsl-postgres-keepalive.md) — 本机集成测试卡住的根因;跑 DB 测试前须唤醒并保活 WSL VM(附本机 pytest tmp_path / mypy 路径坑)
- [PostgreSQL 僵尸锁卡死测试的根因与防护](pg-lock-hygiene.md) — idle-in-transaction 僵尸事务让测试无限等锁;conftest 已设 lock_timeout,本地角色已设 15min 自动回收,附 pg_stat_activity 排查命令
- [测试基建关键坑:pytest-timeout / SQLAlchemy URL / 守卫注入](test-infra-timeout-and-testdb.md) — thread 方式 os._exit 杀进程、pytest-asyncio 1.4 无内置超时、str(URL) 脱敏密码、循环闭包 late-binding;findashboard 角色已授 CREATEDB
- [OpenCode Web 工作台:SPA 状态与容器环境踩坑](opencode-web-state-and-home.md) — 项目/最近会话在浏览器 IndexedDB,服务端无法预置;HOME/XDG 决定文件选择器与数据落点;坏路径会话清理方法
- [Issue #173:selection bars/snapshot 输入模式](issue-173-bars-snapshot-selection.md) — required_datasets 按因子推导;同名异类 Repository 陷阱;集成测试库 create_all 不加新列需手工 ALTER
- [测试库 schema 漂移:create_all 不 ALTER 已有表](test-db-create-all-schema-drift.md) — 模型加列后测试库须手动 ALTER(无 alembic_version,跑 alembic 撞 DuplicateTable);.env URL 提取用 Python 勿用 sed
- [OpenCode finboard-researcher:bash 放行 + exa 搜索 MCP](opencode-agent-bash-exa-mcp.md) — 容器无 JS 运行时,local MCP 不可行只能 remote;exa 托管端点匿名可用;.opencode/.agents 挂载补了 :ro;改权限须重启容器
- [Issue #185:instrument 元数据回填贯通发布链路](issue-185-instrument-metadata-backfill.md) — list_date/industry 从「无写入者」到 profiles 回填 + 发布兜底;缺失统计进 quality_report/job phase;发布单资产测试须显式 required_capabilities
- [Issue #186:universe 预检与候选池诊断](issue-186-universe-precheck.md) — 静态预览只对确定性元数据判空(价格/特征字段只 warning 不误报);入队空池秒级失败;enqueue 创建路径 updated_at 懒加载 MissingGreenlet 已顺带修复
- [Issue #187:冻结发布多数据集联合发布](issue-187-joined-releases-factor-snapshot.md) — daily_metrics/financial_indicators 从 research 表冻结 + 联合 bars 产出基本面因子快照;market_cap 须注册 catalog 因子;测试须显式 volatility_windows、financial report_period 落区间、daily 写满交易日
- [Issue #188:research_run 分阶段进度上报](issue-188-research-run-stage-progress.md) — progress total 递增自修正(worker 只增不减)+ phase 命名 research_run:<stage>;单决策 total=13 精确
- [Issue #189:backtest_run strategy 形态异步化](issue-189-backtest-async.md) — run_async 命名(async 保留字);工作量=标的不数x交易日,阈值 default 15000(≈10ms/段);异步 payload/幂等键与 REST 同口径
- [Issue #190:MCP 回测/入队工具易用性修复](issue-190-mcp-tool-usability.md) — factor_version 与快照 framework_version 双命名空间勿混传;grid_get 默认 none 瘦身(显式 equity_mode 才返回曲线);run_queue payload 模板 + code_version 与数据集同名互不校验
- [冻结发布校验和 DB 锚定(issue #202)](issue-202-release-checksum-db-anchor.md) — manifest 完整性比对 DB release_checksum 不做代码敏感重算;删改 manifest 字段先迁 schema_version 禁止直接改 as_dict();code_version 的 -dirty 语义
- [Issue #203:multi_period 快照依赖文档与报错对齐](issue-203-multiperiod-snapshot-docs-alignment.md) — 多期免快照仅限决策日推导与价格因子,基本面因子仍需快照/研究发布;single_shot 零快照入队秒级拒(共享门控函数);报错按根因细分附 execution_mode
- [Issue #204:factor_snapshots 原子 upsert](issue-204-factor-snapshot-atomic-upsert.md) — ON CONFLICT 冲突方 RETURNING 为空须再 SELECT 复用,仅插入方写值行;gather 自然交错复现不了 TOCTOU,须 Event 编排时序(A 提交前 B 已卡在服务端)
- [Issue #205:回测 fills.date 取实际交易日](issue-205-fills-date-trading-day.md) — Fill.filled_at 默认 _utcnow() 陷阱(回测域构造必须显式传时点);收盘约定收敛到 clock.market_close;旧记录按 created_at 区分不回填
- [Issue #206:研究/回测 MCP 返回瘦身](issue-206-mcp-response-slimming.md) — MCP SDK 会把 dataclass 返回注解重建为同名 pydantic 模型,自定义序列化钩子失效,信封须 BaseModel+model_serializer;详情默认 summary/写回执 ack/fills 有界 200
- [research_data_sync 同步运营 runbook(issue #212)](research-data-sync-runbook.md) — 首次全量过夜跑(~5s/片×小时计)/增量每日一片;配额是进程内锁须 worker 单进程;daily 截面恒全市场;PIT 锚点有回归测试锁边界
- [Issue #214:因子目录收敛 v2 唯一事实来源](issue-214-factor-catalog-unification.md) — FACTOR_CATALOG 从 FACTOR_LAB_CATALOG 投影生成,仅原始值单位留 v1 展示映射(v2 unit=标准化后 z_score);v1 名称缺失导入期 fail-loud;还有第三个目录 finboard_backtest RESEARCH_FACTOR_CATALOG 未动
- [Issue #226:复合评分目录语义字段投影](issue-226-scorer-catalog-projection.md) — 评分参数(winsorize/standardize/missing)本地、假设/失效/来源/方向自 FACTOR_LAB_CATALOG 投影;EXPOSURE_ONLY 无方向映射导入期拒绝
- [Issue #215:研究代码仓库与服务端提交工具](issue-215-research-code-repo.md) — git 读命令必须显式 --git-dir(否则落在本仓库 CWD);路径历史须 log main -- path;register 返回冻结快照;沙箱 issue 复用 Service.read 取版本源码
- [Issue #216:研究代码沙箱执行器](issue-216-research-sandbox-run.md) — PIT=物理隔离(挂载生成端 fail-closed);/out 用 bind 而非 tmpfs(容器停后 tmpfs 丢失);镜像 tag↔kit 版本三处同步;harness 须 spec_from_file_location 防 sys.modules 跨运行污染
- [Issue #217:沙箱因子接入选股管线](issue-217-sandbox-factor-pipeline.md) — u_ 前缀全链路判据;as_dict 可选键 None 不输出保旧 checksum;NaN/覆盖率 0.5 阈值互补;screen forward=相邻决策 PIT close;测试库改列须手动 drop 表
- [Issue #218:沙箱策略代码接入 multi_period 回测](issue-218-sandbox-strategy-backtest.md) — decide(ctx)→targets 逐决策日容器执行;direct_weights 不重缩放、截断权威在管线约束投影;空 targets=合法全现金(空分支须补审计行);commit 由入队冻结、执行期不解析 active;kit 0.2.0 三处同步
- [Issue #219:研究代码验证门与晋级链路](issue-219-research-code-promotion.md) — submit/rollback 只建 draft;screen + #57 validated_oos/final_test_unsealed 晋级门;active+passed 才可正式消费;四向审计引用与沙箱日志/资源归档;模拟盘保持独立隔离
- [#233/#234:首次晋级闭环落地](promotion-chain-closure-233-234.md) — OOS 执行任务化揭盲不可重做;screen 绑定声明放 spec payload(编译期先于 run_queue 拒 draft);#218 遗留 checksum 缺陷顺带修复;factor screen 需 ≥2 份 RCR 快照;测试库 drop 表连带 CASCADE 外键
- [第二轮 L3 E2E:验证实验三项阻断修复(#244)](e2e-round2-validation-executor-fixes-244.md) — PBO 矩阵取 IS 竞争 trial 非顺序窗口(近似 vs 真实交易日点数必不等);capital 预校验;异常兜底不替实验下 REJECTED(会烧掉不可重入的实验);假 runner 曲线必须含回撤(mdd=0→calmar=Infinity 落 JSON 崩)
- [OpenCode provider 持久化与模型目录合并](opencode-provider-persistence-and-model-sync.md) — UI 连接=auth.json(data 卷),config provider=仓库文件(徽标「配置」);agent 定义以 .opencode/agent/*.md 为准(json agent 块是死配置);credential 表恒为空;同 id 与 models.dev 目录合并;UI 写配置落 :ro 挂载必失,重启回落仓库冻结态(#242)
- [Issue #253:multi_period 因子可用性入队门控](issue-253-multi-period-feature-gate.md) — derived_features 冻结进 manifest(空值不序列化保旧 checksum);门控查全部 identity 源非仅 FACTOR;canonical 映射必须在数据层(builder 内填、依赖方向);multi_factor 模板默认引用 pb,「只挂 bars 正对照」此后不成立
- [Issue #254/#255:universe 评估域收窄与 research_db 选股 fail-visible](issue-254-255-universe-domain-and-selection-gate.md) — explicit_symbols 收窄共用 explicit_symbol_domain(静态/运行时一致);benchmark_source 独立字段勿塞 benchmark_config;摄取 ≠ 发布(runs 273-275 根因),三层门控共用 repo 方法;MCP 同步/异步/grid 三路径加校验的位置;叠分支避免文档冲突
- [Issue #256:指数标的数据链路入口](issue-256-index-data-pipeline-done.md) — bars 主发布唯一→指数基准须与股票同份 mixed 发布;is_benchmark_only_instrument 双枚举点排除(不排除=指数被交易);portfolio_config 覆盖必须嵌 overrides 键;WorkerConfig 无 operator/source 字段
- [本机 akshare 复现实验的 ProxyError 陷阱(#257)](akshare-em-proxy-error-repro-trap.md) — 系统代理对 push2his.eastmoney.com 偶发 ProxyError(600519 真股票也失败),接口行为判定以源码+请求 URL secid 参数为准;集成测试一律 mock 网络
- [Issue #259:backtest 网格 selection_grid 选股维度组合展开](issue-259-selection-grid-done.md) — 独立 selection_grid 参数而非 selection.* 键(params 校验链路零改动);label/checksum「键存在才纳入」保旧网格幂等;全角乘号 × 触发 RUF001-003;MCP 工具函数加参须给默认值(集成测试显式 kwargs)
- [Issue #261:发布标的集来源三选一完成](issue-261-release-symbol-source-done.md) — 入队期解析成具体 symbols 执行器零改动;复制 verbatim 勿加 kind 过滤;mixed scope 门要求三类型齐备是 #184 遗留;非 bars 发布有 stock-scope 检查
- [多 worktree 并行开发:本机落地参数与坑位(#289)](multi-worktree-parallel-dev.md) — wt2 已就位(junction 共享 data_cache/data_releases + 独立库/端口);删 junction 禁 rm -rf;vite 端口/代理可环境变量覆盖
