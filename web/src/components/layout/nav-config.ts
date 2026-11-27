import type { LucideIcon } from "lucide-react";
import type { LocalizedText } from "@/i18n";
import {
  Activity,
  Atom,
  BookOpen,
  CandlestickChart,
  Database,
  FileText,
  GitBranch,
  LayoutDashboard,
  ListOrdered,
  ListTodo,
  PlayCircle,
  Plug,
  Scale,
  ScrollText,
  Settings,
  ShieldAlert,
  SlidersHorizontal,
  TestTube,
  TrendingUp,
  Wrench,
} from "lucide-react";

interface NavItem {
  to: string;
  label: LocalizedText;
  icon: LucideIcon;
  restricted?: boolean;
}

export interface ResearchTool extends NavItem {
  description: LocalizedText;
  fullEditionOnly?: boolean;
}

export const researchTools: ResearchTool[] = [
  {
    to: "/research/factors", label: { zh: "因子实验室", en: "Factor Lab" }, icon: Atom,
    description: { zh: "查找因子、预览信号和复核冻结实验。", en: "Find factors, preview signals, and inspect frozen experiments." },
  },
  {
    to: "/research/strategy", label: { zh: "策略 Studio", en: "Strategy Studio" }, icon: SlidersHorizontal,
    description: { zh: "用无代码配置构建策略，比较并发布版本。", en: "Configure a strategy without code, then compare and publish versions." },
  },
  {
    to: "/backtest", label: { zh: "探索回测", en: "Exploratory backtest" }, icon: TrendingUp,
    description: { zh: "用预设快速试验并查看历史结果。", en: "Run exploratory checks from presets and see previous results." },
  },
  {
    to: "/research/experiments", label: { zh: "实验与 OOS", en: "Experiments & OOS" }, icon: TestTube,
    description: { zh: "查看正式样本外验证与揭盲结果。", en: "Review formal out-of-sample validation and revealed results." },
  },
  {
    to: "/research/runs", label: { zh: "研究运行", en: "Research runs" }, icon: GitBranch,
    description: { zh: "追踪运行、冻结输入与结果。", en: "Follow research runs, frozen inputs, and results." },
  },
  {
    to: "/research/portfolio", label: { zh: "组合与风险", en: "Portfolio & risk" }, icon: Scale,
    description: { zh: "查看目标权重与三档资金可行性。", en: "Inspect target weights and capital feasibility across three tiers." },
  },
  {
    to: "/research/simulation", label: { zh: "模拟盘", en: "Simulation" }, icon: PlayCircle, fullEditionOnly: true,
    description: { zh: "查看模拟账户、会话和撮合记录。", en: "Inspect paper accounts, sessions, and matching records." },
  },
  {
    to: "/research/reports", label: { zh: "研究报告", en: "Research reports" }, icon: FileText,
    description: { zh: "查看绩效、归因和报告导出。", en: "Inspect performance, attribution, and report exports." },
  },
  {
    to: "/research/strategy-explanation", label: { zh: "策略说明书", en: "Strategy explanation" }, icon: ScrollText,
    description: { zh: "按指定策略版本或运行复核生效规则。", en: "Inspect effective rules for a strategy version or run." },
  },
  {
    to: "/research/docs", label: { zh: "研究资料", en: "Research records" }, icon: BookOpen,
    description: { zh: "阅读正式结论与历轮复核记录。", en: "Read published findings and review history." },
  },
  {
    to: "/research/agent-connect", label: { zh: "Agent 接入", en: "Agent connection" }, icon: Plug,
    description: { zh: "接入外部研究客户端，继续已有课题。", en: "Connect an external research client and resume an existing topic." },
  },
];

const topicNav: NavItem = { to: "/research/topics", label: { zh: "课题", en: "Topics" }, icon: BookOpen };
const dataNav: NavItem = { to: "/data", label: { zh: "数据基座", en: "Data Base" }, icon: Database };
const jobsNav: NavItem = { to: "/jobs", label: { zh: "任务与审计", en: "Tasks & audit" }, icon: ListTodo };
const liveNav: NavItem = { to: "/live", label: { zh: "实盘", en: "Live trading" }, icon: Activity, restricted: true };
const settingsNav: NavItem = { to: "/settings", label: { zh: "设置", en: "Settings" }, icon: Settings };

export const toolsNav: NavItem = {
  to: "/research/tools",
  label: { zh: "高级工具", en: "Advanced tools" },
  icon: Wrench,
};

export const livePages: NavItem[] = [
  { to: "/positions", label: { zh: "持仓", en: "Positions" }, icon: CandlestickChart, restricted: true },
  { to: "/orders", label: { zh: "订单", en: "Orders" }, icon: ListOrdered, restricted: true },
  { to: "/fills", label: { zh: "成交", en: "Fills" }, icon: ScrollText, restricted: true },
  { to: "/control", label: { zh: "控制台", en: "Control console" }, icon: ShieldAlert, restricted: true },
];

export function primaryNavigation(researchEdition: boolean): NavItem[] {
  return researchEdition
    ? [topicNav, dataNav, jobsNav, settingsNav]
    : [topicNav, dataNav, jobsNav, liveNav, settingsNav];
}

export function availableResearchTools(researchEdition: boolean): ResearchTool[] {
  return researchTools.filter((tool) => !researchEdition || !tool.fullEditionOnly);
}

export function isCurrentPath(pathname: string, to: string): boolean {
  return pathname === to || pathname === `${to}/`;
}

export function navigationContext(pathname: string, researchEdition: boolean): NavItem | undefined {
  const pages = [
    ...primaryNavigation(researchEdition),
    toolsNav,
    ...availableResearchTools(researchEdition),
    ...(researchEdition ? [] : livePages),
    { to: "/research", label: { zh: "研究总览", en: "Research overview" }, icon: LayoutDashboard },
    { to: "/", label: { zh: "工作总览", en: "Workspace overview" }, icon: LayoutDashboard },
  ];
  return pages.find((page) => isCurrentPath(pathname, page.to));
}
