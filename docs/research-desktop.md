# FinDashboard Windows 桌面研究版

离线 `FinDashboard-research-offline-windows-amd64.msi` 适用于 Windows x64，需提前安装 Docker Desktop 并切换到 Linux 容器，以及系统 WebView2。离线包包含固定版本研究镜像、PostgreSQL 镜像和合成入门数据。在线 ZIP 适合通过命令行接入 Codex、DSH 或 OpenCode，安装时从镜像仓库获取镜像。两个安装包均随 `research-vX.Y.Z` 发布。 [GitHub Releases](https://github.com/Caspian-Lin/FinDashboard/releases) · [在线 ZIP 指南](research-edition.md)

## 安装和首次研究

1. 从同一 Release 获取 MSI 与 `SHA256SUMS-desktop`，核对文件的 SHA-256 后安装并打开 FinDashboard Research。
2. 启动 Docker Desktop。在管理窗口确认本机端口后点击「安装并启动」。默认端口为 `8276`。安装器核对归档校验和和已导入镜像身份，使用 Compose 的禁止拉取模式启动服务并迁移数据库。
3. 点击「导入样例」，再打开「研究工作台」。在「数据基座」可查询 `synthetic-starter-v1` 及冻结发布校验和。在工作台使用合成候选 `FBDEMO01.SH` 至 `FBDEMO08.SH` 和基准 `FBDEMOIDX.SH`，按研究版指南中的周频样例保存、发布无代码策略并发起研究。
4. 使用 Codex 时先选「只读」并点击「接入或切换」，按提示重开 Codex。需要让 Codex 创建课题或提交运行时，选择「课题与研究运行」并重新接入。管理窗口显示当前作用域和验连状态，旧凭据由安装器撤销。 [固定样例的输入与步骤](research-edition.md#离线入门包)

「操作记录」显示命令的进度、退出码、耗时、脱敏输出及等价 PowerShell 命令。端口占用时填写其它本机端口。Docker 不可用时先恢复 Docker，再刷新状态并重试。归档校验失败时重新下载同一 Release 的 MSI。

研究工作台在独立的 WebView2 窗口使用本机研究端口。主机操作由管理窗口提供。桌面版的 Docker 项目默认为 `finboard-research-desktop`，在线 ZIP 使用 `finboard-research`。

## 重启、备份与卸载

- 停止后重新启动，原课题、研究运行与冻结发布可继续读取。升级前备份桌面项目的 `finboard-research-desktop_postgres`、`finboard-research-desktop_research` 卷和 `%LOCALAPPDATA%\FinDashboard\Desktop`。其中数据库口令与卷配套，选用同一份备份。Codex 的 DPAPI 凭据只供原 Windows 用户使用，迁移后重新接入。
- 在「安装维护与卸载」选择「卸载并保留研究数据」，以保留卷与恢复配置。随后可从 Windows「已安装的应用」卸载管理程序。重装同版或兼容新版本后点击「安装并启动」，接着恢复 Codex 接入。
- 彻底卸载会列出清理范围，并要求输入确认文字。它同时清理本实例的卷与本机状态，适用于确认无需恢复或已经备份的情况。Docker 不可用或操作中断时，窗口报告未完成的步骤，恢复依赖后可重试。

## 构建与验收

`scripts/package_research_desktop.py` 将从 Release 构建取得的研究镜像、固定 PostgreSQL 镜像、Windows 安装器及合成包打入 MSI 资源。镜像归档使用流式 gzip。发布工作流记录两个镜像 ID、归档字节数、MSI 及 SHA-256。Windows 构建执行 `cd desktop`、`npm ci`、`npm run build`。首次启动计量从点击「安装并启动」至状态就绪，另记归档导入、合成数据导入、worker 运行和结果可读时间。

Windows 11 与 Docker Desktop 29.6.2 的一次本地复测中，原始镜像归档为 989,140,992 字节，流式压缩后为 365,537,205 字节。冷载、空库迁移至 loopback 就绪共 147.832 秒，同机还有构建与基准任务。另一轮隔离测试的合成研究运行耗时 1.908 秒，提交至可读 2.142 秒，停启至就绪 13.878 秒。

- Windows CI 运行原生 Tauri 测试和 `scripts/test_research_desktop_host.ps1`，覆盖操作参数、管理来源、归档损坏、Docker 不可用、端口冲突、跨项目归属及卸载恢复。
- 本仓库桌面 PR 的 Actions 构建会产出 `research-desktop-pr-msi`。预览包用独立的 Windows 产品、Docker 项目、本机状态和 `8376` 端口，构建版本区分运行及重试。正式版的默认端口为 `8276`。
- 本地端到端脚本在独立 Docker 项目中复测离线导入、真实研究、重装和 Codex CLI 权限切换。 [桌面端到端验收](research-desktop-e2e.md)
- 发布验收在具备前置依赖的干净 Windows x64 环境阻断 Docker Hub 和 GHCR。安装 MSI，从管理界面导入合成包并在工作台完成运行。分别验证真实 Codex 的只读、课题与研究运行、切回只读三次授权。
- 用同一 `RT-`、`RR-` 与冻结发布校验和复核停启、升级和保留卸载再重装。另启第二个 Docker 项目，验证两种卸载和故障重试只作用于选中的桌面实例。这覆盖 `phase1_doc.md` §3.4 第 11 项的恢复要求。
