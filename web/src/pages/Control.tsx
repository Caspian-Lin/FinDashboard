import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";

const KS_LEVELS = [
  { value: "off", label: "恢复正常", color: "green" },
  { value: "no_new_orders", label: "暂停新单", color: "yellow" },
  { value: "reduce_only", label: "仅减仓", color: "orange" },
  { value: "cancel_all", label: "撤全部", color: "red" },
  { value: "halt", label: "全局停止", color: "red" },
];

export default function Control() {
  const qc = useQueryClient();
  const [reason, setReason] = useState("manual");
  const { data: ks } = useQuery({ queryKey: ["kill-switch"], queryFn: api.getKillSwitch });
  const { data: logs } = useQuery({ queryKey: ["audit-logs"], queryFn: () => api.getAuditLogs(50) });

  const ksMut = useMutation({
    mutationFn: ({ level, reason }: { level: string; reason: string }) =>
      api.activateKillSwitch(level, reason),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["kill-switch"] }),
  });

  const reconMut = useMutation({
    mutationFn: () => api.triggerReconcile(),
  });

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">控制台</h1>

      {/* Kill Switch */}
      <div className="bg-white rounded-lg shadow p-5 mb-6">
        <h2 className="font-semibold mb-4">Kill Switch</h2>
        <div className="mb-4">
          <span className="text-gray-500 text-sm">当前状态: </span>
          <span className={`font-bold ${ks?.level === "off" ? "text-green-600" : "text-red-600"}`}>
            {ks?.level ?? "—"}
          </span>
        </div>
        <input
          className="border rounded px-3 py-1.5 text-sm mb-3 w-full"
          placeholder="原因"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
        />
        <div className="flex gap-2">
          {KS_LEVELS.map((lvl) => (
            <button
              key={lvl.value}
              onClick={() => ksMut.mutate({ level: lvl.value, reason })}
              className={`px-3 py-1.5 rounded text-sm border ${
                lvl.color === "green"
                  ? "border-green-500 text-green-600 hover:bg-green-50"
                  : lvl.color === "red"
                    ? "border-red-500 text-red-600 hover:bg-red-50"
                    : "border-yellow-500 text-yellow-600 hover:bg-yellow-50"
              }`}
            >
              {lvl.label}
            </button>
          ))}
        </div>
        {ksMut.isError && (
          <div className="mt-2 text-red-500 text-sm">{ksMut.error?.message}</div>
        )}
      </div>

      {/* Reconcile */}
      <div className="bg-white rounded-lg shadow p-5 mb-6">
        <h2 className="font-semibold mb-4">核对</h2>
        <button
          onClick={() => reconMut.mutate()}
          disabled={reconMut.isPending}
          className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700 disabled:opacity-50"
        >
          {reconMut.isPending ? "核对中..." : "触发核对"}
        </button>
        {reconMut.data && (
          <div className={`mt-3 text-sm ${reconMut.data.ok ? "text-green-600" : "text-red-600"}`}>
            {reconMut.data.ok ? "✓ " : "✗ "}
            {reconMut.data.summary}
          </div>
        )}
      </div>

      {/* Audit Logs */}
      <div className="bg-white rounded-lg shadow p-5">
        <h2 className="font-semibold mb-4">审计日志</h2>
        {(logs?.items ?? []).length === 0 ? (
          <div className="text-gray-400 text-sm">无日志</div>
        ) : (
          <table className="w-full text-sm">
            <thead className="text-gray-500">
              <tr>
                <th className="px-2 py-1 text-left">时间</th>
                <th className="px-2 py-1 text-left">操作者</th>
                <th className="px-2 py-1 text-left">动作</th>
                <th className="px-2 py-1 text-left">目标</th>
              </tr>
            </thead>
            <tbody>
              {(logs?.items ?? []).map((log) => (
                <tr key={log.id} className="border-t">
                  <td className="px-2 py-1 text-gray-500">{new Date(log.created_at).toLocaleTimeString()}</td>
                  <td className="px-2 py-1">{log.actor}</td>
                  <td className="px-2 py-1 font-mono">{log.action}</td>
                  <td className="px-2 py-1 font-mono text-gray-500">{log.target ?? "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
