import type { LucideIcon } from "lucide-react";
import type { LocalizedText } from "@/i18n";
import {
  Home,
  Database,
  Atom,
  SlidersHorizontal,
  TestTube,
  GitBranch,
  Scale,
  PlayCircle,
  MonitorSmartphone,
  BookOpen,
  FileText,
  LayoutDashboard,
  CandlestickChart,
  ListOrdered,
  ScrollText,
  ShieldAlert,
  TrendingUp,
  ListTodo,
  Settings,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: LocalizedText;
  icon: LucideIcon;
  end?: boolean;
}

export interface NavGroup {
  label: LocalizedText;
  items: NavItem[];
  restricted?: boolean;
}

export const navGroups: NavGroup[] = [
  {
    label: { zh: "研究", en: "Research" },
    items: [
      { to: "/research", label: { zh: "研究首页", en: "Research Home" }, icon: Home, end: true },
      { to: "/research/data", label: { zh: "数据与标的", en: "Data & Instruments" }, icon: Database },
      { to: "/research/factors", label: { zh: "因子实验室", en: "Factor Lab" }, icon: Atom },
      { to: "/research/strategy", label: { zh: "策略 Studio", en: "Strategy Studio" }, icon: SlidersHorizontal },
      { to: "/backtest", label: { zh: "回测", en: "Backtest" }, icon: TrendingUp },
      { to: "/research/experiments", label: { zh: "实验与 OOS", en: "Experiments & OOS" }, icon: TestTube },
      { to: "/research/runs", label: { zh: "研究运行", en: "Research Runs" }, icon: GitBranch },
      { to: "/research/portfolio", label: { zh: "组合与风险", en: "Portfolio & Risk" }, icon: Scale },
      { to: "/research/simulation", label: { zh: "模拟盘", en: "Simulation" }, icon: PlayCircle },
      { to: "/research/workbench", label: { zh: "研究工作台", en: "Research Workbench" }, icon: MonitorSmartphone },
      { to: "/research/docs", label: { zh: "研究记录", en: "Research Notes" }, icon: BookOpen },
      { to: "/research/reports", label: { zh: "研究报告", en: "Research Reports" }, icon: FileText },
    ],
  },
  {
    label: { zh: "实盘交易", en: "Live Trading" },
    restricted: true,
    items: [
      { to: "/", label: { zh: "仪表盘", en: "Dashboard" }, icon: LayoutDashboard, end: true },
      { to: "/positions", label: { zh: "持仓", en: "Positions" }, icon: CandlestickChart },
      { to: "/orders", label: { zh: "订单", en: "Orders" }, icon: ListOrdered },
      { to: "/fills", label: { zh: "成交", en: "Fills" }, icon: ScrollText },
      { to: "/control", label: { zh: "控制台", en: "Control Console" }, icon: ShieldAlert },
    ],
  },
  {
    label: { zh: "工具", en: "Tools" },
    items: [
      { to: "/jobs", label: { zh: "任务中心", en: "Jobs" }, icon: ListTodo },
      { to: "/settings", label: { zh: "设置", en: "Settings" }, icon: Settings },
    ],
  },
];
