# FinDashboard 前端产品文档

## 信息架构

```
研究（主流程）
├── 研究首页      /research            — 工作流概览、最近活动、快捷入口
├── 数据与标的    /research/data       — 数据发布、数据集清单、标的元数据、生命周期
├── 因子实验室    /research/factors    — 因子目录、特征快照、信号预览、因子实验
├── 策略 Studio   /research/strategy   — 无代码结构化配置、即时校验、版本管理
├── 实验与 OOS    /research/experiments — 机器验证实验、阈值与稳健性检验
├── 研究运行      /research/runs       — 冻结输入、血缘追踪、运行重放
├── 组合与风险    /research/portfolio  — 目标权重分配、离散交易、资金可行性
├── 模拟盘        /research/simulation — 纸面撮合、目标仓位决策、绩效报告
├── 研究工作台    /research/workbench  — OpenCode Web 研究交互（iframe 直连，AI 能力由 OpenCode 承担）
└── 研究报告      /research/reports     — 聚合展示运行结果与绩效归因、CSV/Markdown 导出

实盘交易（受控区域）
├── 仪表盘        /                    — 内核状态、账户资金、活动订单
├── 持仓          /positions           — 本地/券商持仓
├── 订单          /orders              — 手工下单/撤单（二次确认）
├── 成交          /fills               — 成交记录
└── 控制台        /control             — Kill Switch、核对、审计

工具
├── 回测          /backtest            — 策略回测、历史记录
├── 策略预设      /strategies          — 预设管理
└── 设置          /settings            — 定时任务配置
```

## 用户旅程

### 研究主流程

```
数据发布 → 因子研究 → 无代码策略配置 → 实验与 OOS → 研究运行 → 组合构建 → 模拟盘 → 报告
```

1. **数据准备**：在「数据与标的」页查看数据发布质量、资产覆盖和标的生命周期
2. **因子研究**：在「因子实验室」浏览因子目录、预览信号、冻结因子实验
3. **策略配置**：在「策略 Studio」用白名单组件配置策略、即时校验、版本 diff 与发布
4. **实验验证**：在「实验与 OOS」创建验证实验、设置阈值与稳健性检验
5. **研究运行**：在「研究运行」冻结输入、追踪血缘、查看结果或重放
6. **组合构建**：在「组合与风险」分配权重、离散交易求解、三档资金可行性
7. **模拟交易**：在「模拟盘」创建账户与会话、提交目标仓位决策、查看绩效
8. **报告分析**：在「研究报告」聚合查看绩效指标、权益曲线与归因

### AI 辅助流程

```
OpenCode Agent 研究交互（工作台 iframe 直连）
研究写操作（ResearchRun/回测/模拟盘）由 agent 经 MCP 自主执行（#122）
Agent（OpenCode）分析 → 创建验证实验 / 因子实验 → 机器验证
```

「研究工作台」页面顶部 banner 展示 OpenCode Web 与 FinBoard MCP 运行状态;
`opencode_web_enabled=false` 时工作台显示降级提示。
Agent 行为审计可查 `GET /api/mcp/audit`（`mcp_audit_persist` 开启时持久化）。

## 三区边界

| 维度 | 研究区 | 模拟区 | 实盘区 |
|------|--------|--------|--------|
| 路由前缀 | `/research/*` | `/research/simulation` | `/` `/positions` `/orders` 等 |
| 数据表 | `factor_*` `ai_*` `research_*` | `simulation_*` | `orders` `fills` `positions` |
| 订单 | 不发单 | 纸面撮合 | 真实券商 |
| Kill Switch | 无关 | 无关 | 核心安全机制 |
| 标识 | 蓝色/默认 | 蓝色/默认 | 黄色「受控区域」标记 |
| Python | 禁止 | 禁止 | 不适用 |

## 设计 Token

### 颜色（CSS 变量，HSL 格式;issue #162 深色基础表面已中性灰阶化）

| Token | 暗色 | 亮色 | 语义 |
|-------|------|------|------|
| `--background` | 0 0% 8% | 0 0% 100% | 页面背景 |
| `--foreground` | 0 0% 93% | 0 0% 11% | 主要文字 |
| `--card` | 0 0% 11% | 0 0% 100% | 卡片背景 |
| `--popover` | 0 0% 13% | 0 0% 100% | 浮层背景 |
| `--primary` | 199 89% 48% | 199 89% 42% | 主色调/链接/激活 |
| `--secondary` | 0 0% 15% | 0 0% 96% | 次要背景 |
| `--muted` | 0 0% 15% | 0 0% 96% | 弱化背景 |
| `--accent` | 0 0% 19% | 0 0% 94% | hover/选中反馈 |
| `--destructive` | 0 72% 55% | 0 84% 55% | 危险/错误 |
| `--success` | 142 71% 45% | 142 71% 40% | 成功/正面 |
| `--warning` | 38 92% 50% | 38 92% 45% | 警告/阻塞 |
| `--info` | 199 89% 48% | 199 89% 42% | 信息 |
| `--up` | 0 84% 62% | 0 72% 48% | A 股涨(红) |
| `--down` | 142 71% 45% | 142 72% 35% | A 股跌(绿) |
| `--border` | 0 0% 24% | 0 0% 88% | 边框 |
| `--chart-1..8` | 见 index.css | 见 index.css | 图表系列色 |

### A 股颜色惯例

- 红色（`text-up`，涨）= 涨/盈利
- 绿色（`text-down`，跌）= 跌/亏损

设计细节（无阴影 / 圆角 / 触控目标 / 键盘 / 刷新策略）见 `DESIGN.md`。

### 字体

- Sans: Inter / system-ui / PingFang SC / Microsoft YaHei
- Mono: JetBrains Mono / SF Mono / Consolas
- 数字: `tabular-nums` / `font-feature-settings: "tnum"`

### 主题

- 默认暗色（`dark` class on `<html>`）
- 切换持久化到 `localStorage`
- 通过 `ThemeProvider` + `useTheme()` 管理

## 页面状态矩阵

每个页面需覆盖以下状态：

| 状态 | 组件 | 说明 |
|------|------|------|
| 加载 | `<LoadingState>` / `<Skeleton>` | 数据获取中 |
| 空 | `<EmptyState>` | 无数据 |
| 错误 | `<ErrorState>` / `<Alert variant="destructive">` | API 失败 |
| 阻塞 | `<BlockedState>` / `<Alert variant="warning">` | 权限/依赖未满足 |
| 数据 | 正常渲染 | 有数据 |

## 安全约束

1. **禁止 Python**：策略 Studio 只接受结构化无代码配置，不存在 Python 编辑器/上传/导入入口
2. **保存不启动运行**：策略规格保存/发布不会自动启动任何回测或运行
3. **模拟盘隔离**：模拟盘不连接实盘 Broker，订单只能由结构化目标仓位决策生成
4. **实盘二次确认**：下单和撤单操作均需 Dialog 二次确认
5. **AI 审批闭环**：AI 草案须人工审批后才可消费，金融问答须引用来源并声明不确定性
