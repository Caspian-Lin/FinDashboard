# FinDashboard Docker 研究版

支持 Windows x64、Docker Desktop 的 Linux 容器模式，以及 Codex、DeepSeek Harness（DSH）或 OpenCode。镜像架构为 `linux/amd64`。Web、研究 REST 和 MCP 共用一个仅发布到 `127.0.0.1` 的端口。PostgreSQL 留在 Compose 网络内。

希望通过桌面接入 Codex，或安装时无法访问 Docker Hub 和 GHCR，可使用含固定镜像和合成样例的离线 MSI。前置条件和数据卷归属见[桌面研究版指南](research-desktop.md)。

## 安装与接入

从 [GitHub Releases](https://github.com/Caspian-Lin/FinDashboard/releases) 下载 `FinDashboard-research-windows-amd64.zip`，解压后在该目录打开终端。核对 Release 附带的 `SHA256SUMS`，然后运行安装命令。

```powershell
.\finboard.cmd setup --client codex
```

OpenCode 用 `--client opencode`。DSH 用 `--client dsh`，默认写入 `desktop` profile，其他 profile 可用 `--profile headless` 等参数。DSH 首次运行时会创建 profile。安装后在 DSH 中打开命令输出的工作目录来加载 Skill。

安装器启动数据库、应用迁移、Web、研究 worker 与 HTTP MCP，并验证所签发凭据可以初始化 MCP。Codex 使用本地插件市场，OpenCode 使用本机 MCP 配置，DSH 使用当前 profile 的配置补丁。三个客户端都使用随包发布的 `finboard-research` Skill。首次接入默认 `readonly`。需要写课题、提交研究运行或提交研究代码时，在安装器中明确选择 `topic-writer`。

```powershell
.\finboard.cmd setup --client codex --preset topic-writer
```

每个客户端拥有独立凭据。宿主机通过 Windows DPAPI 为当前用户保护凭据，数据库保存摘要。再次运行相同安装命令复用可用凭据。切换预设或补发凭据时，旧凭据会撤销。重新打开客户端后可让 Agent 调用 `finboard_topic_read`，或从 Web 的 Agent 接入页把原 `RT-` 带回对话。

Web 默认入口为 `http://127.0.0.1:8176/`，MCP 为同端口的 `/mcp`。端口在用时，用 `setup --client … --port 18176` 指定空闲端口。DSH 已追加多个 profile 时，在端口变化后逐一重跑相关 profile 的设定。Docker 未启动时，启动 Docker Desktop 后重跑。凭据过期、撤销或本机解密失败时，重跑 setup。

```powershell
.\finboard.cmd status
.\finboard.cmd stop
.\finboard.cmd start
.\finboard.cmd revoke --client codex
```

## 准备数据

### 离线入门包

从同一 Release 单独下载 `synthetic-starter-v1.zip`。导入和样例研究可以在数据源离线时进行。

```powershell
.\finboard.cmd import --bundle .\synthetic-starter-v1.zip
```

导入会验证发布清单、逐文件校验和和交易日轴，再原子登记发布、标的与日历。终端显示发布版本、来源、日期和校验和。重复导入复用原发布。入门包仅含合成行情，发布 ID 为 `synthetic-starter-v1`，包含 8 只样例标的、1 个仅作基准的指数和预热历史。目录和报告中的 `synthetic-starter` 来源以及冻结发布名标明数据性质。

让 Agent 用 `FBDEMO01.SH` 至 `FBDEMO08.SH` 和基准 `FBDEMOIDX.SH`，锁定 `synthetic-starter-v1` 发布。先在策略 Studio 保存并发布使用 `momentum` 与 `volatility_20d` 的无代码规格。然后在课题下预览 2024-01-02 至 2024-02-23、每周调仓、有至少一个尾部交易日的数据范围。将预览返回的 `resolution_id` 与 scope 一起提交 `finboard_topic_run_start`，沿用原 `BJ-` 等待，并读取原 `RR-` 的摘要和冻结依据。固定复测规格可查 `scripts/smoke_research_distribution.py`。

### 桌面 Agent 接入

Windows MSI 将 Codex CLI、DSH、OpenCode 的预设与凭据绑定到当前桌面实例。通用 Streamable HTTP 客户端可领取独立的只读配置。每个客户端的切换、撤销和停启验证见桌面指南。 [桌面接入操作](research-desktop.md#agent-接入)

### 自备数据源

Windows MSI 可在桌面控制台的「Tushare 数据源」保存凭据、重启研究服务并查看接口权限。下列命令适用于在线 ZIP。Web 可查看已配置状态，凭据由桌面控制台或命令行维护。 [桌面操作指南](research-desktop.md#tushare-数据源)

```powershell
.\finboard.cmd source-configure
.\finboard.cmd source-status
```

在隐藏输入提示中提供自己的 Tushare token。成功连接后，服务从 Tushare 登记沪深交易日历，并将凭据保存在专用持久卷的受限文件中。诊断区分缺少凭据、认证失败、权限不足、请求配额和探针窗口覆盖不足，并分别检查日线、复权因子、指数、每日指标和财务因子。AkShare 和 yfinance 的既有行情适配器可查行情覆盖，其财务因子支持以数据目录为准。

在 Web 的「数据基座」同步标的目录，再选定候选、基准、数据源与复权口径。将基准同时纳入行情补数标的，预览并提交补数，在任务中心核对质量和冻结发布回执。Tushare 单源的多资产行情发布会逐标的核对来源。将成功发布的版本和校验和绑定进策略规格，按同一数据范围预览与研究提交链路运行。真实行情留在本机。 [Tushare 数据服务协议](https://tushare.pro/document/1?doc_id=405) · [yfinance 数据使用说明](https://github.com/ranaroussi/yfinance#legal-stuff)

### 自定义代码

随包 Skill 说明了 `compute_series(ctx)` 因子与 `decide(ctx)` 策略的版本化提交。研究版的 Agent 接入页显示沙箱状态。当前镜像默认关闭自定义代码执行。需要运行自定义代码时，使用支持研究沙箱的开发部署，按需拉取 `ghcr.io/caspian-lin/findashboard-research-sandbox:<Release 标签>` 并配置 `FINBOARD_RESEARCH_SANDBOX_IMAGE`。自定义代码继续通过 PIT、screen、OOS 和晋级门。 [研究代码沙箱](dev-guide.md#研究代码沙箱issue-216可选)

## 升级、备份和恢复

升级前执行 `stop`，备份 Docker Desktop 中的 `finboard-research_postgres` 与 `finboard-research_research` 卷，以及 `%LOCALAPPDATA%\FinDashboard`。数据库保存课题、运行和客户端授权，研究卷保存冻结发布、工件和数据源配置。保护备份中的凭据文件。换 Windows 用户或主机后重新签发客户端凭据。解压新版本到新目录后运行 setup，数据库迁移和镜像版本由新安装包决定。

正常停启保留两个卷，重启后通过相同 `RT-` 查看原 `RR-` 与冻结发布。默认容器入口包括研究网关、研究 worker 与迁移。实盘交易路由未注册。

这对应 `phase1_doc.md` §3.4 第 11 项的重启恢复验证。研究版冒烟测试核对原课题、运行和数据发布校验和，另核对交易表在全程保持为空。

## 构建与复测

GitHub Actions 构建研究镜像并在空库验证迁移、HTTP MCP、离线入门包、只读拒写和重启回查。打 `research-v*` 标签后，工作流发布版本镜像并准备 Release 草稿。发布前检查 GHCR 包允许匿名拉取，并确认三个客户端的实测记录。

开发者可以运行 `scripts/build_research_starter.py` 生成独立数据包，随后用 `scripts/smoke_research_distribution.py` 复测。复测输出记录镜像、数据版本、启动、导入、入队、worker 起止与结果可读时间。性能对比必须使用同一候选池、日期范围和调仓频率，镜像下载另计。

### 首版性能记录

2026-11-19 在 Windows x64、Core i9-13900H（20 逻辑处理器）、Docker Desktop 4.85.0（约 7.75 GiB VM 内存）实测。样例为 8 只合成股票、1 个合成指数、7 个周频决策。入门包压缩后 70,245 字节，发布校验和为 `581d1fcdb3f25f39313d1f2bbf92fcc8fdaa117122ef5035529364055bb575ae`。

| 计量项 | 基线 | 研究版 |
| --- | ---: | ---: |
| PostgreSQL linux/amd64 冷拉取压缩层 | 161,363,956 字节（bookworm） | 116,235,896 字节（alpine） |
| 必需镜像去重压缩层总量（本地复压估计） | 348,853,760 字节 | 332,113,714 字节 |
| 已有库启动至 HTTP MCP 响应 | 17.40 秒 | 15.89 秒 |
| 入门包复核及登记 | 8.63 秒 | 8.56 秒 |
| 入队请求往返 | 4.14 秒 | 3.47 秒 |
| 任务队列等待 | 0.48 秒 | 0.26 秒 |
| worker 执行 | 17.33 秒 | 1.69 秒 |
| 从提交至可读结果 | 26.00 秒 | 10.38 秒 |

对照运行以 `FINBOARD_RESEARCH_MIN_POOL_SYMBOLS=0` 启用原进程池。研究版默认在发布少于 32 个标的时复用进程内特征计算，较大的发布沿用原进程池。两次样例的策略/基准收益、回撤、期末权益、决策数、成交数与对账状态的合并摘要校验和一致。优化后整栈停止、启动及 MCP 就绪用时 18.83 秒，原课题、运行及发布校验和可回读。这是同机各一次测量，下载耗时单独计量。

复测时设 `FINBOARD_SMOKE_OFFLINE=1` 在隔离 Docker 网络运行样例，先用门槛 `0` 跑对照，再用默认门槛跑研究版。CI 使用空库迁移，样例 worker 预算 10 秒。Release 工作流对安装器和入门包各设 256 KiB 预算，必需镜像冷拉取去重压缩层设 384 MiB 预算。可选沙箱镜像单列。用 `scripts/measure_research_images.py` 读取镜像发布后的 amd64 层清单。

同日完成真实 Tushare 验证。同步标的目录和沪深交易日历后，以 8 只股票和 `000001.SH` 为样例冻结 qfq 发布，运行 2024-06-03 至 2024-06-28 的周频研究。发布为 `DR-bars-qfq-BF-F040D84748D84E32`，运行 `RR-8db262f16501f747ad50eeca` 已完成。凭据和真实数据不随分发包发布。
