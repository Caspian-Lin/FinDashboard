# Windows 桌面研究版端到端验收

在 Windows x64 运行脚本前，启动 Docker Desktop 的 Linux 引擎。准备同一版本离线 MSI 的 `edition` 资源目录。可直接使用打包脚本生成的目录，也可使用已安装版本的资源目录。运行脚本需要 Python 3.10 或以上。

```powershell
python -m scripts.smoke_research_desktop --run-isolated `
    --resources "C:\path\to\edition" --port 18377
```

传入 `--with-codex` 时，脚本还会用真实 Codex CLI 安装并移除本地插件。测试使用仓库 `.tmp` 中的新配置目录和合成项目，关闭客户端分析并固定凭据存储为文件，过程无需登录。脚本使用插件对应的 stdio 桥，验证只读拒绝提交、写入档完成合成研究、切回只读后再次拒绝，并查询隔离数据库确认旧凭据已撤销。

每次运行随机创建独立 Docker 项目和数据卷，另创建一个控制项目的卷检查卸载范围。端口必须空闲。脚本使用同一离线归档启动、导入样例并复用研究版首次运行测试。随后停启、保留卸载、重装并读回原课题、运行及冻结发布校验和。正常退出时清理本次测试的容器、网络和卷，证据写入终端打印的 `.tmp` 目录。如果运行期间中断，请从该目录的 `state` 中确认本次生成的项目名，再继续清理同一个隔离实例。

报告记录归档体积和 SHA-256、镜像身份、首次就绪、导入、研究结果可读时间、保留重装回读和隔离校验。首次就绪时间包含实例预检和状态复核。记录当时是否已有相同镜像标签以及机器运行负载，便于复测时比较。

## 未安装版启动验收

在 `desktop/src-tauri` 用 `cargo build --release --features standalone` 构建未安装版。将打包资源 `edition/` 放在可执行文件同级的可写目录，直接启动程序，确认启动器可见且同级生成 `webview2-profile/`。

## MSI 图形验收

上述脚本覆盖 Windows 宿主命令、真实 Docker 和可选 Codex CLI。安装程序界面、工作台 WebView2、重开 Codex 后的真实对话以及 Windows 卸载界面须在单独的干净 Windows x64 环境验证。

### 固定预览候选

从同一个 Actions 执行下载 `research-desktop-pr-msi`，安装包文件名包含 `Preview` 和 Windows Installer 的产品版本。`desktop-pr-candidate.json` 记录实际检出的提交和 Git 树、来源 PR 提交、执行编号及重试次数、镜像身份、MSI 产品版本，以及 MSI 和离线资源的字节数与 SHA-256。比较 Git 树可确认预览内容对应准备验收的里程碑版本。里程碑内容变化后使用重新构建的候选。安装后，在管理台导航下方核对版本。

```powershell
$candidate = Get-Content -Raw -LiteralPath .\desktop-pr-candidate.json | ConvertFrom-Json
$msi = Get-Item -LiteralPath $candidate.msi.file
$starter = Get-Item -LiteralPath $candidate.starter_file
$starterExpected = $candidate.installed_resources.'starter.zip'
if ($msi.Length -ne $candidate.msi.bytes -or
    (Get-FileHash -LiteralPath $msi.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $candidate.msi.sha256 -or
    $starter.Length -ne $starterExpected.bytes -or
    (Get-FileHash -LiteralPath $starter.FullName -Algorithm SHA256).Hash.ToLowerInvariant() -cne $starterExpected.sha256) {
    throw 'MSI 或合成样例与候选记录不一致。'
}
$candidate | Select-Object checkout_commit, source_tree, run_id, run_attempt, images
```

Actions 的 WiX 与 MSI 管理安装只验证打包、布局和资源一致性。记录 `administrative_install_verified` 时，将资源验证和图形验收分别记账。执行 #567 的预览构建已通过 Windows 原生测试、WiX 与 MSI 管理安装，后续图形验收按所选候选重新留证。[预览工作流](https://github.com/Caspian-Lin/FinDashboard/actions/runs/46041518329)

Windows CI 另用 MSI 管理安装验证安装布局与镜像归档 checksum。Rust 回归测试用实际 Windows PowerShell 验证含空格的安装路径可解析 `$PSScriptRoot`。本机面板可在系统深色和浅色外观、默认尺寸及最窄窗口下复测。用 Tab 检查焦点、状态刷新、诊断展开与卸载确认，检查日志和复制命令是否可读。

在启动器和研究工作台分别验证右上角的最小化、最大化与还原和关闭按钮，以及在现有页面顶栏留白处拖动窗口。工作台关闭后应能从启动器重新打开。缩短研究窗口，确认弹层不会盖住窗口按钮，窗口操作也不会关闭弹层。断开研究服务后重载工作台，确认本机错误页面仍显示同一组按钮。在最窄窗口用滚轮和键盘访问底部维护区。

让两个窗口反复跨过窄布局断点，再最大化和还原。检查按钮到窗口右上角的距离一致，三个点击区等宽，按钮上缘能点击，窗口顶边仍能拖动缩放。

1. 在干净 Windows x64 测试机登记 Windows、Docker Desktop、WebView2 版本及镜像缓存。准备已校验产物和前置依赖，阻断 Docker Hub 与 GHCR，再通过 MSI 图形界面安装。按管理窗口完成服务启动与样例导入，分别记录首次就绪和导入耗时。
2. 在应用内及系统默认浏览器分别打开研究工作台，核对本机端口和合成发布校验和。用合成输入完成一次研究运行，记录 worker 领取、完成与结果可读时间。工作台访问限定为研究功能，管理操作从控制台进入。
3. 在已登录的实际 Codex 对话以「只读」读取课题并验证拒写，切为「课题与研究运行」后在隔离实例创建课题和运行，切回只读后再次验证拒写。每次切换按提示重开客户端，记录脱敏的拒绝码、运行状态和课题标识。复测后撤销测试凭据。
4. 停启并重开工作台，复核原 `RT-`、`RR-` 和冻结发布校验和。用 Windows「已安装的应用」验证系统卸载入口，保留数据后重新安装，复核相同证据。彻底卸载另用一个独立测试实例，确认外部控制项目的容器、卷和接入仍可使用。
5. 候选通过后记录正式发布所用提交和镜像摘要。分别核验同一 Release 的在线 ZIP、离线 MSI、合成样例和校验和，登记实际包体与备份回退路径。正式在线 ZIP 需要对应的仓库镜像地址和摘要，从正式发布流程取证。
