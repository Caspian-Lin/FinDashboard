import { NavLink, Route, Routes } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  LayoutDashboard,
  CandlestickChart,
  ListOrdered,
  ScrollText,
  ShieldAlert,
  Database,
  TrendingUp,
  Settings as SettingsIcon,
} from "lucide-react";
import { api } from "./lib/api";
import { useWebSocket } from "./lib/ws";
import Dashboard from "./pages/Dashboard";
import Orders from "./pages/Orders";
import Positions from "./pages/Positions";
import Fills from "./pages/Fills";
import Control from "./pages/Control";
import Data from "./pages/Data";
import Backtest from "./pages/Backtest";
import Settings from "./pages/Settings";

const navItems = [
  { to: "/", label: "仪表盘", icon: LayoutDashboard },
  { to: "/positions", label: "持仓", icon: CandlestickChart },
  { to: "/orders", label: "订单", icon: ListOrdered },
  { to: "/fills", label: "成交", icon: ScrollText },
  { to: "/control", label: "控制", icon: ShieldAlert },
  { to: "/data", label: "数据", icon: Database },
  { to: "/backtest", label: "回测", icon: TrendingUp },
  { to: "/settings", label: "设置", icon: SettingsIcon },
];

export default function App() {
  const wsConnected = useWebSocket();
  const { data: health } = useQuery({
    queryKey: ["health"],
    queryFn: api.health,
  });

  const kernelOk = health?.kernel_ready ?? false;
  const ksLevel = health?.kill_switch_level ?? "off";

  return (
    <div className="flex h-screen">
      {/* Sidebar */}
      <nav className="w-56 bg-slate-900 text-slate-300 flex flex-col">
        <div className="px-5 py-4 text-white font-bold text-lg border-b border-slate-700">
          FinDashboard
        </div>
        <div className="px-5 py-3 border-b border-slate-700">
          <div className="flex items-center gap-2 text-sm">
            <span
              className={`inline-block w-2 h-2 rounded-full ${
                kernelOk ? "bg-green-400" : "bg-red-400"
              }`}
            />
            <span>{kernelOk ? "内核就绪" : "内核未就绪"}</span>
          </div>
          <div className="flex items-center gap-2 text-sm mt-1">
            <span
              className={`inline-block w-2 h-2 rounded-full ${
                wsConnected ? "bg-green-400" : "bg-gray-500"
              }`}
            />
            <span>WS {wsConnected ? "已连接" : "断开"}</span>
          </div>
          {ksLevel !== "off" && (
            <div className="mt-2 text-xs text-red-400 font-semibold">
              Kill Switch: {ksLevel}
            </div>
          )}
        </div>
        <div className="flex-1 py-2">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 px-5 py-2.5 text-sm transition-colors ${
                  isActive
                    ? "bg-slate-700 text-white"
                    : "hover:bg-slate-800"
                }`
              }
            >
              <item.icon size={18} />
              {item.label}
            </NavLink>
          ))}
        </div>
      </nav>

      {/* Content */}
      <main className="flex-1 overflow-auto bg-gray-50 p-6">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/positions" element={<Positions />} />
          <Route path="/orders" element={<Orders />} />
          <Route path="/fills" element={<Fills />} />
          <Route path="/control" element={<Control />} />
          <Route path="/data" element={<Data />} />
          <Route path="/backtest" element={<Backtest />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
