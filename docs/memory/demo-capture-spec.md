# FinDashboard Demo 截图与媒体规格

## 主题

FinDashboard 对外 Demo、README 与发布页所用产品截图 / GIF 的统一录制规格。

## 结论 / 事实

### 产品截图基线

- **产品界面语言：** 英文。录制前设置 `localStorage["finboard-lang"] = "en"` 并刷新。
- **主题：** 深色默认主题。清除 `findashboard-theme`，或确保其值不是 `light`。
- **浏览器：** Chromium / Chrome，缩放 100%，无扩展 UI、无浏览器边框进入画面。
- **视口：** `1440 × 960` CSS px，`deviceScaleFactor = 1`，非 mobile emulation。
- **格式：** PNG，RGB，截取当前 viewport；不先裁剪、不拉伸、不额外锐化。
- **导航：** 左侧导航保持展开；页面滚动位置应让标题、主要操作和证据区同时可辨认。
- **状态：** API 请求完成、Skeleton 消失、WebSocket 与健康状态稳定后再截图。
- **悬浮层：** 截图中不得出现非叙事所需的 tooltip、popover、select menu、toast、拖拽态或 focus ring。操作完成后发送 `Escape`、让活动元素 `blur()`、把指针移出目标区域并等待至少 250 ms。
- **数据真实性：** 只展示真实本地研究记录；不得通过手写数据库状态或营销 mock 补齐模拟业绩。
- **安全：** 使用 Mock broker、关闭后台 Worker；截图过程不得创建订单、修改持仓或提交研究写操作。

### 固定场景与文件名

| 文件 | 页面 / 状态 |
|---|---|
| `research-home.png` | `/research`，研究入口与最近记录已加载 |
| `data-release.png` | `/research/data`，数据覆盖与发布入口可见 |
| `factor-lab.png` | `/research/factors`，因子目录首屏 |
| `oos-detail.png` | `/research/experiments`，选中真实 `VALIDATED_OOS` 实验，详情已加载且无悬浮层 |
| `run-lineage.png` | `/research/runs`，选中 completed ResearchRun，冻结输入与 checksum 可见 |
| `portfolio-risk.png` | `/research/portfolio`，信号与组合约束入口 |
| `simulation-empty.png` | `/research/simulation`，保留真实账户 / 会话状态 |
| `job-audit.png` | `/jobs`，任务状态、阶段和时间信息可见 |

源 PNG 统一放在 `docs/demo/assets/`。替换现有文件名，避免页面和 README 因版本号改链接。

### GIF 与 Demo 页面 QA

- OOS 与 ResearchRun 的短 GIF 使用同规格 `1440 × 960` 源帧，输出 `960 × 640`、8 fps、循环播放、最多 128 色。
- GIF 第一段展示列表，第二段展示选中后的详情；不包含悬浮提示、加载骨架或无意义等待。
- Demo 页面桌面 QA 使用 `1440 × 1000`；移动 QA 使用 `390 × 844`，DPR 均为 1。
- 滚动叙事必须核对 active step、active scene、标题和进度四者一致；英文根页面与 `/zh/` 翻译页都要检查。
- README 首屏预览使用 Demo 英文页面的 `1440 × 1000` viewport，并保存为 `demo-page-preview.png`。

### 推荐录制收尾检查

1. 验证 `document.documentElement.lang === "en"`。
2. 验证视口为 `1440 × 960` 且 `devicePixelRatio === 1`。
3. 验证页面不存在 `[role="tooltip"]`、打开的 dialog / popover、Skeleton 或错误提示。
4. 检查按钮没有越过卡片边界，文本没有裁切，主从栏没有相互遮挡。
5. 截图后人工查看原始 PNG，不以缩略图代替最终 QA。
6. 运行 Demo 的内部资源检查、HTML 校验、桌面 / 移动浏览器检查。

## Why（为什么）

首版 OOS 截图在较窄主从布局里暴露了 `MasterList` 头部操作区越界，同时把被遮挡的按钮固化进 Demo 媒体。没有固定语言、视口、悬浮层和加载状态约束时，即使功能正确，不同轮次生成的画面也会在密度、比例和可读性上漂移，且截图可能掩盖真实 UI 回归。

## How to apply（下次如何应用）

- 任何新增或替换 `docs/demo/assets/` 媒体的任务，先按本规格启动和稳定页面，再录制。
- 若页面在 `1440 × 960` 仍出现遮挡，先修产品 UI 并增加回归测试，不得通过裁图绕开。
- 只有叙事明确需要时才保留弹层；否则执行悬浮层清理步骤后再截图。
- 如确需改变尺寸、语言或编码参数，必须同步修改本文件、Demo 计划和全部受影响媒体，禁止单文件例外。
