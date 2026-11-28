# FinDashboard Windows 桌面研究版

离线 `FinDashboard-research-offline-windows-amd64.msi` 适用于 Windows x64，需提前安装 Docker Desktop 并切换到 Linux 容器，以及系统 WebView2。离线包包含固定版本研究镜像、PostgreSQL 镜像和合成入门数据。在线 ZIP 适合通过命令行接入 Codex、DSH 或 OpenCode，安装时从镜像仓库获取镜像。两个安装包均随 `research-vX.Y.Z` 发布。 [GitHub Releases](https://github.com/Caspian-Lin/FinDashboard/releases) · [在线 ZIP 指南](research-edition.md)

## 安装和首次研究

1. 从同一 Release 获取 MSI 与 `SHA256SUMS-desktop`，核对文件的 SHA-256 后安装并打开 FinDashboard Research。
2. 启动 Docker Desktop。在管理窗口确认本机端口后点击「安装并启动」。默认端口为 `8276`。安装器核对归档校验和和已导入镜像身份，兼容 Docker 使用镜像配置摘要或 OCI 描述符摘要的两种存储方式。随后通过 Compose 的禁止拉取模式启动服务并迁移数据库。
3. 点击「导入样例」，再打开「研究工作台」。在「数据基座」可查询 `synthetic-starter-v1` 及冻结发布校验和。在工作台使用合成候选 `FBDEMO01.SH` 至 `FBDEMO08.SH` 和基准 `FBDEMOIDX.SH`，按研究版指南中的周频样例保存、发布无代码策略并发起研究。
4. 在「Agent 接入」选择客户端和「只读」预设，再点击「接入并验连」。按提示重开客户端。需要创建课题或提交运行时，可为所选客户端切换到「课题与研究运行」。 [固定样例的输入与步骤](research-edition.md#离线入门包)

「运行诊断」显示进度、退出码和耗时。展开「日志与命令」可查看脱敏输出并复制等价 PowerShell 命令。端口占用时填写其他本机端口。Docker 不可用时先恢复 Docker，再刷新状态并重试。归档校验失败时重新下载同一 Release 的 MSI。

管理台顶部可切换中英文及浅色、深色主题，重开管理台时沿用上次选择。向下滚动可访问数据源、诊断和维护。研究工作台的语言和主题在工作台内设置。如果服务不可用，内嵌窗口的恢复页使用创建窗口时的管理台偏好。管理台主标题下方显示当前版本，安装预览版后可用它核对下载文件中的版本号。

「研究工作台」提供应用内打开、系统默认浏览器和复制地址，默认使用独立的 WebView2 窗口。「默认打开方式」保存于当前控制台。每次打开或复制都会重新检查本实例和已生效的回环端口。修改端口后先点击「应用端口」，再用工作台入口打开新地址。若浏览器无法打开，可检查系统默认浏览器或复制地址。如果剪贴板不可用，控制台会选中可手动复制的地址。主机操作由管理窗口提供。桌面版的 Docker 项目默认为 `finboard-research-desktop`，在线 ZIP 使用 `finboard-research`。

应用内工作台的窗口按钮位于顶栏右端，按钮左侧留白可拖动窗口。Windows 11 下可悬停最大化按钮或按 Win+Z 使用系统贴靠布局。 [Microsoft Windows Snap 指南](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/ui/apply-snap-layout-menu)

## Tushare 数据源

在「Tushare 数据源」输入获准使用的 Token，再点击「保存并重启研究服务」。研究版在保存前检查日线权限并登记沪深交易日历。凭据保存在当前实例研究卷内的受限文件中。日志、等价命令和接口检查结果只显示脱敏状态。

「检查权限」无需重新输入 Token。展开检查结果可分别查看股票日线、复权、指数、每日指标、财务数据及配置时的交易日历状态。若界面提示配置已保存、服务重启未确认，请刷新状态，再启动研究服务。Web 的数据源状态用于查看配置并继续同步、发布。 [研究数据同步示例](research-edition.md#自备数据源)

## Agent 接入

- Codex CLI 安装当前桌面实例的研究插件。OpenCode 使用本实例的项目配置，点击「打开工作区」，在该目录启动 OpenCode。OpenCode 的全局设置由用户继续管理。 [OpenCode 项目配置](https://opencode.ai/docs/config/)
- 从已有 DeepSeek Harness profile 中选择目标项，再接入。用「打开工作区」或「复制工作区路径」在该 profile 中打开研究 Skill 目录。必要时在 DSH 打开 skill-filesystem。变更服务端口后重新接入 DSH，以更新配置里的本机端点。 [DeepSeek Harness 接入](agent-integration.md#5-deepseek-harness已验-2026-11-10)
- 通用客户端可按名签发 Streamable HTTP 配置，默认使用「只读」。复制本次结果并粘贴到客户端。控制台不保留明文，复制后会收起配置。重新签发同名客户端会撤销旧凭据。选择「撤销所选凭据」可停用该连接。
- 每个预设只作用于所选客户端。卸载按当前桌面实例清理接入配置和研究凭据。Docker 服务停启后可按原端口继续读取同一课题。

## 重启、备份与卸载

- 停止后重新启动，原课题、研究运行与冻结发布可继续读取。升级前备份桌面项目的 `finboard-research-desktop_postgres`、`finboard-research-desktop_research` 卷和 `%LOCALAPPDATA%\FinDashboard\Desktop`。其中数据库口令与卷配套，选用同一份备份。Codex 的 DPAPI 凭据只供原 Windows 用户使用，迁移后重新接入。
- 在「安装维护与卸载」选择「卸载并保留研究数据」，以保留卷与恢复配置。随后可从 Windows「已安装的应用」卸载管理程序。重装同版或兼容新版本后点击「安装并启动」，接着恢复 Codex 接入。
- 彻底卸载会列出清理范围，并要求输入确认文字。它同时清理本实例的卷与本机状态，适用于确认无需恢复或已经备份的情况。清理会报告并保留被其他容器使用或身份无法证实的镜像标签。Docker 不可用或操作中断时，窗口报告未完成的步骤，恢复依赖后可重试。

## 构建与验收

`scripts/package_research_desktop.py` 将从 Release 构建取得的研究镜像、固定 PostgreSQL 镜像、Windows 安装器及合成包打入 MSI 资源。镜像归档使用流式 gzip。发布工作流记录两个镜像 ID、归档字节数、MSI 及 SHA-256。Windows 构建执行 `cd desktop`、`npm ci`、`npm run build`。首次启动计量从点击「安装并启动」至状态就绪，另记归档导入、合成数据导入、worker 运行和结果可读时间。

Windows 11 与 Docker Desktop 29.6.2 的一次本地复测中，原始镜像归档为 989,140,992 字节，流式压缩后为 365,537,205 字节。冷载、空库迁移至 loopback 就绪共 147.832 秒，同机还有构建与基准任务。另一轮隔离测试的合成研究运行耗时 1.908 秒，提交至可读 2.142 秒，停启至就绪 13.878 秒。

- Windows CI 运行原生 Tauri 测试和 `scripts/test_research_desktop_host.ps1`，覆盖操作参数、管理来源、归档损坏、Docker 不可用、端口冲突、跨项目归属及卸载恢复。
- 本仓库桌面 PR 的 Actions 构建会产出 `research-desktop-pr-msi`，其中的安装包名为 `FinDashboard-Research-Preview-<版本>-windows-amd64.msi`。预览包用独立的 Windows 产品、Docker 项目、本机状态和 `8376` 端口，构建版本区分运行及重试。正式版的默认端口为 `8276`。
- 本地端到端脚本在独立 Docker 项目中复测离线导入、真实研究、重装和 Codex CLI 权限切换。 [桌面端到端验收](research-desktop-e2e.md)
- 发布验收在具备前置依赖的干净 Windows x64 环境阻断 Docker Hub 和 GHCR。安装 MSI，从管理界面导入合成包并在工作台完成运行。分别验证真实 Codex 的只读、课题与研究运行、切回只读三次授权。
- 用同一 `RT-`、`RR-` 与冻结发布校验和复核停启、升级和保留卸载再重装。另启第二个 Docker 项目，验证两种卸载和故障重试只作用于选中的桌面实例。这覆盖 `phase1_doc.md` §3.4 第 11 项的恢复要求。

从数据基座的「同步与发布」可打开[冻结发布包](research/release-bundles.md)操作。
