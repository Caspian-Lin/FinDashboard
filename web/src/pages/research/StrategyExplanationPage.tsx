import { useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/ui/page-header";
import { useT } from "@/i18n";
import StrategyExplanation from "@/components/research/StrategyExplanation";
export default function StrategyExplanationPage() {
  const { tl } = useT();
  const [p] = useSearchParams();
  const runId = p.get("run") ?? undefined;
  const strategyId = p.get("strategy") ?? undefined;
  const version = Number(p.get("version"));
  return (
    <div className="space-y-6">
      <PageHeader title={tl({ zh: "策略说明书", en: "Strategy Explanation" })} description={tl({ zh: "冻结规则、实际方向与执行证据", en: "Frozen rules, realized directions, and execution evidence" })} />
      {runId || (strategyId && version > 0) ? (
        <StrategyExplanation runId={runId} strategyId={strategyId} version={version} />
      ) : (
        <p>{tl({ zh: "请选择指定策略版本或历史研究运行查看冻结证据。", en: "Select a strategy version or historical research run to review its frozen evidence." })}</p>
      )}
    </div>
  );
}
