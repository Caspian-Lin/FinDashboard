# FinDashboard Web 设计规范（DESIGN.md）

本文档是前端可执行的视觉规范(issue #162/#163/#164)。所有页面与共享组件
必须遵循本规范;与规范冲突的写法按本规范治理,不再新增例外。token 与组件
契约变更时先更新本文档,再改代码。

## 1. 设计原则

- **弱描边,无阴影**:表面层级用低对比度中性描边 + 表面色区分,**不使用
  `box-shadow`**。深色模式下的浮层(弹窗/下拉/提示)同样无阴影,靠
  `border + bg-popover` 与内容区区分,避免脏重的浮层感。
- **中性灰阶基础表面**:深色基础表面为低饱和中性灰(无蓝色色相),主色
  (品牌蓝)与语义色(涨跌/成功/警告/错误)保留作为强调色。
- **Token 优先**:颜色、圆角、字体全部走 CSS 变量 / Tailwind token;
  禁止页面内 raw hex、`text-white` 与主题表面硬编码组合、绕过 token 的
  原始 Tailwind 颜色类(`bg-blue-600` / `bg-gray-50` 等)。

## 2. 主题 token 表

主题切换:`.light` class 挂在 `<html>` 上(默认深色,`:root` 即深色值),
localStorage key `findashboard-theme`;`web/index.html` 头部内联脚本在
首帧前恢复主题,`body` 背景走 `var(--background)` 兜底,禁止 `bg-gray-50`
之类写死。

### 深色(:root)

| token | 值 (HSL) | 用途 |
|---|---|---|
| `--background` | `0 0% 8%` | 页面底色 |
| `--card` | `0 0% 11%` | 卡片/侧栏/顶栏表面 |
| `--popover` | `0 0% 13%` | 浮层(弹窗/下拉/提示)表面 |
| `--secondary` | `0 0% 15%` | 次级表面(进度轨、骨架、表头) |
| `--muted` | `0 0% 15%` | 弱化表面/代码块 |
| `--accent` | `0 0% 19%` | hover/选中反馈表面 |
| `--border` | `0 0% 24%` | 描边 |
| `--input` | `0 0% 26%` | 输入控件描边(略亮于 border) |
| `--foreground` | `0 0% 93%` | 主文字 |
| `--muted-foreground` | `0 0% 62%` | 次要文字 |
| `--primary` / `--ring` | `199 89% 48%` | 品牌主色 / 焦点环 |
| `--up` / `--down` | `0 84% 62%` / `142 71% 45%` | **A 股涨/跌(红涨绿跌)** |
| `--success` / `--warning` / `--destructive` | 绿/琥珀/红 | 状态语义色 |
| `--chart-1..8` | 见 index.css | 图表系列色板 |

### 浅色(.light)

中性灰阶:`--background 0 0% 100%`、`--card 0 0% 100%`、`--secondary/muted
0 0% 96%`、`--accent 0 0% 94%`、`--border 0 0% 88%`、`--input 0 0% 84%`、
`--foreground 0 0% 11%`、`--muted-foreground 0 0% 47%`;语义色与图表色板
同步加深保证白底对比度(见 index.css)。

## 3. 描边 / 阴影 / 层级

- 默认无 `box-shadow`;任何新代码不得引入 `shadow-*` / `box-shadow`。
- 层级表达:
  - 页面内容区 → `bg-background`;
  - 卡片/面板/表格容器 → `border border-border bg-card rounded-lg`;
  - 浮层(popover/dropdown/tooltip/dialog/sheet)→ `border border-border
    bg-popover`(+ 对应组件),无阴影;
  - 分隔线 → `divide-border` / `border-border`。

## 4. 圆角

| 档位 | 值 | 用途 |
|---|---|---|
| `rounded-lg` | `var(--radius)` = 0.5rem | 面板/卡片/表格容器/大按钮 |
| `rounded-md` | `calc(radius - 2px)` | 默认控件(Button/Input/Select/搜索框) |
| `rounded-sm` | `calc(radius - 4px)` | 紧凑控件(checkbox 等) |
| `rounded-full` | 100% | **仅**状态胶囊、开关、圆点、进度条轨道等语义场景 |

禁止容器使用 `rounded-xl/2xl/3xl`;已有页面已统一到 `rounded-lg`。

## 5. 语义色与 A 股惯例

- **A 股涨跌:红涨绿跌**(与产品文档一致)。涨用 `text-up`(可配
  `bg-up/10` 等),跌用 `text-down`;禁用 `text-red-500/text-green-500`
  等原始色。注意:非 A 股语义的「错误」仍是 `destructive`(红),「成功」
  仍是 `success`(绿),两者与 up/down 是不同维度。
- 状态色只用 `success/warning/destructive/info` token;图表系列色只用
  `chart-1..8`;焦点环只用 `ring-ring`。
- 语义不只依赖颜色:错误/成功等关键状态同时提供图标或文字(见 §8)。

## 6. 共享组件契约

- 页面容器统一 `PageHeader`(唯一 `h1` + description + actions)与
  `PageContainer`(max-w-7xl 居中)。
- 表格统一 `Table` 组件(自带 `overflow-x-auto` 横向滚动);
  行级操作不用整行点击,交互元素必须是真实按钮。
- 状态展示统一 `States` 组件:`EmptyState / LoadingState / ErrorState(onRetry)
  / BlockedState`;动态结果用 `role="status"` / live region。
- 表单控件必须有可见 `Label` 或 `aria-label`;placeholder 只作示例。
- 按钮尺寸:`Button` 默认 h-9;触控目标规则见 §7。

## 7. 触控目标与键盘

- 交互元素可点击外框 **≥ 44px** 优先(移动端)。图标按钮
  (`size="icon"`)用 `h-11 w-11`(44px);提示入口(InfoHint 等)加
  `p-2` 保证命中区。
- **登记例外**:桌面端高密度表格工具栏的紧凑按钮(`size="sm"`,约 32px)
  与行内内联按钮允许缩小,但必须带可见文字或 `aria-label`,且按钮间距
  ≥ 8px;移动端断点下恢复 44px 或改用图标按钮。
- 可点击 div/tr 必须改为 `button` / `a` 等语义元素:支持 Enter/Space、
  可见焦点(ring)、`aria-expanded` 等状态属性。

## 8. 可访问性基线

- 对比度满足 WCAG AA:普通文本 `--foreground` vs `--background`,
  次要文本 `--muted-foreground`(深色 62% 亮度)通过。
- 状态播报:错误/成功反馈用 `Alert`(自带 `role="alert"`)或
  `aria-live="polite"`;避免只靠颜色传达。
- 页面唯一语义标题:AppShell 顶栏**不渲染** `h1`,页面内容区每个路由
  有且只有一个 `h1`(由 PageHeader 提供)。

## 9. 数据刷新策略

- React Query **默认不轮询**(`refetchInterval` 为查询级 opt-in)。
- 需要实时性的查询(账户/持仓/订单/成交/任务中心)显式声明
  `refetchInterval`;静态研究查询(列表/报告/因子)不轮询。
- 任务类查询在存在非终态任务时轮询,全部终态后自动停止
  (参考 `/jobs` 任务中心页的 `refetchInterval` 函数式写法)。

## 10. 页面使用示例

- 列表页(任务中心 `/jobs`):`PageHeader` + 筛选区(`Label`+`Select`)+
  `Table`(状态徽章 `StatusBadge`,进度 `Progress`)+ `States` + 轮询
  函数式 `refetchInterval`。
- 图表(回测权益曲线 / 组合饼图):recharts 的 `stroke/fill/contentStyle`
  一律用 `hsl(var(--x))` 形式引用 token,禁止 hex 字面量。

## 11. 回滚

本规范只约束前端展示与交互;回退方式为回退 token/组件提交,不涉及
数据库迁移或研究/实盘数据。
