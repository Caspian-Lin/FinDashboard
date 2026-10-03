const formatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

export function ResearchTimestamp({ value }: { value?: string | null }) {
  if (!value) return <span>时间未记录</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return <span title={value}>时间无法解析</span>;
  // Legacy timestamps without an offset cannot establish an absolute instant.
  const hasZone = /(?:z|[+-]\d{2}:\d{2})$/i.test(value);
  return (
    <time dateTime={value} title={value} className="tabular-nums">
      {hasZone
        ? `${formatter.format(date)}（北京时间）`
        : `${value.replace("T", " ").replace(/\.\d+$/, "")}（时区未记录）`}
    </time>
  );
}
