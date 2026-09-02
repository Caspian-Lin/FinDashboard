export function formatDuration(seconds: number | null | undefined, lang: "zh" | "en" = "zh"): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const total = Math.max(0, Math.round(seconds));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (lang === "en") {
    if (hours > 0) return `${hours}h ${minutes}m ${remainder}s`;
    if (minutes > 0) return `${minutes}m ${remainder}s`;
    return `${remainder}s`;
  }
  if (hours > 0) return `${hours} 小时 ${minutes} 分 ${remainder} 秒`;
  if (minutes > 0) return `${minutes} 分 ${remainder} 秒`;
  return `${remainder} 秒`;
}
