# FinDashboard 前端

量化交易研究平台前端，基于 React 19 + TypeScript + Vite 6 + TailwindCSS 3 + shadcn/ui (Radix)。

## 快速开始

```bash
npm install
npm run dev      # 开发服务器 (http://localhost:5173)
npm run build    # 生产构建 (tsc -b && vite build)
npm run test     # 单元/组件测试 (vitest)
npm run lint     # ESLint
```

开发服务器自动代理 `/api` 和 `/ws` 到 `http://localhost:8000`（后端 FastAPI）。

## 信息架构

详见 [PRODUCT.md](./PRODUCT.md)。

三区分离：
- **研究区** (`/research/*`)：数据、因子、策略、实验、组合、模拟、AI、报告
- **实盘交易区** (`/`, `/positions`, `/orders` 等)：受控区域，标记 QMT/内核状态
- **工具** (`/backtest`, `/strategies`, `/settings`)：回测、预设、调度

## 技术栈

| 层 | 技术 |
|----|------|
| 框架 | React 19 |
| 构建 | Vite 6 |
| 类型 | TypeScript 5.7 (strict) |
| 样式 | TailwindCSS 3 + CSS 变量设计 token |
| 组件 | shadcn/ui (Radix Primitives) |
| 数据 | TanStack Query 5 |
| 路由 | React Router 7 (lazy loading) |
| 图表 | Recharts 3 |
| 测试 | Vitest 3 + Testing Library |

## 设计系统

- **暗色优先**（默认），可切换亮色，持久化到 localStorage
- 设计 token 通过 CSS 变量定义（HSL 格式），见 `src/index.css`
- 组件库在 `src/components/ui/`
- 工具函数在 `src/lib/utils.ts`

## API 层

| 模块 | 文件 | 覆盖域 |
|------|------|--------|
| `api.ts` | 实盘交易 + 数据缓存 + 回测 + 预设 + 标的组 |
| `research.ts` | 研究运行 + 因子实验室 + 验证实验 + 策略规格 + 数据发布 |
| `ai.ts` | AI 研究助手（假设/草案/问答/审计） |
| `simulation.ts` | 模拟盘（账户/会话/决策/订单/持仓/报告） |
| `portfolio.ts` | 组合构建（分配/离散/可行性/归因） |

## 安全边界

- **无 Python**：策略 Studio 只接受结构化无代码配置
- **保存不启动**：策略保存/发布不触发运行
- **模拟盘隔离**：不连接实盘 Broker，订单由目标仓位决策生成
- **实盘二次确认**：下单/撤单均需 Dialog 确认
- **AI 审批**：草案须人工审批后才可消费
