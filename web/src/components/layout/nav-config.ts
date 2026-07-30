import type { LucideIcon } from "lucide-react";
import {
  Home,
  Database,
  Atom,
  SlidersHorizontal,
  TestTube,
  GitBranch,
  Scale,
  PlayCircle,
  Sparkles,
  FileText,
  LayoutDashboard,
  CandlestickChart,
  ListOrdered,
  ScrollText,
  ShieldAlert,
  TrendingUp,
  Bookmark,
  Settings,
  HardDriveDownload,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

export interface NavGroup {
  label: string;
  items: NavItem[];
  restricted?: boolean;
}

export const navGroups: NavGroup[] = [
  {
    label: "研究",
    items: [
      { to: "/research", label: "研究首页", icon: Home, end: true },
      { to: "/research/data", label: "数据与标的", icon: Database },
      { to: "/research/factors", label: "因子实验室", icon: Atom },
      { to: "/research/strategy", label: "策略 Studio", icon: SlidersHorizontal },
      { to: "/research/experiments", label: "实验与 OOS", icon: TestTube },
      { to: "/research/runs", label: "研究运行", icon: GitBranch },
      { to: "/research/portfolio", label: "组合与风险", icon: Scale },
      { to: "/research/simulation", label: "模拟盘", icon: PlayCircle },
      { to: "/research/ai", label: "AI 助手", icon: Sparkles },
      { to: "/research/reports", label: "研究报告", icon: FileText },
    ],
  },
  {
    label: "实盘交易",
    restricted: true,
    items: [
      { to: "/", label: "仪表盘", icon: LayoutDashboard, end: true },
      { to: "/positions", label: "持仓", icon: CandlestickChart },
      { to: "/orders", label: "订单", icon: ListOrdered },
      { to: "/fills", label: "成交", icon: ScrollText },
      { to: "/control", label: "控制台", icon: ShieldAlert },
    ],
  },
  {
    label: "工具",
    items: [
      { to: "/data", label: "行情数据", icon: HardDriveDownload },
      { to: "/backtest", label: "回测", icon: TrendingUp },
      { to: "/strategies", label: "策略预设", icon: Bookmark },
      { to: "/settings", label: "设置", icon: Settings },
    ],
  },
];
