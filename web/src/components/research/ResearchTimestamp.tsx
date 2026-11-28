import { useT } from "@/i18n";

const options: Intl.DateTimeFormatOptions = {
  timeZone: "Asia/Shanghai",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
};
const formatters = {
  zh: new Intl.DateTimeFormat("zh-CN", options),
  en: new Intl.DateTimeFormat("en-GB", options),
};

export function ResearchTimestamp({ value }: { value?: string | null }) {
  const { lang, tl } = useT();
  if (!value) return <span>{tl({ zh: "时间未记录", en: "Time not recorded" })}</span>;
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return <span title={value}>{tl({ zh: "时间无法解析", en: "Invalid timestamp" })}</span>;
  }
  // Legacy timestamps without an offset cannot establish an absolute instant.
  const hasZone = /(?:z|[+-]\d{2}:\d{2})$/i.test(value);
  return (
    <time dateTime={value} title={value} className="tabular-nums">
      {hasZone
        ? `${formatters[lang].format(date)}${tl({ zh: "（北京时间）", en: " (Beijing time)" })}`
        : `${value.replace("T", " ").replace(/\.\d+$/, "")}${tl({ zh: "（时区未记录）", en: " (time zone not recorded)" })}`}
    </time>
  );
}
