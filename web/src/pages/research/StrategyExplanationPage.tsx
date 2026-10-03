import { useSearchParams } from "react-router-dom";
import { PageHeader } from "@/components/ui/page-header";
import StrategyExplanation from "@/components/research/StrategyExplanation";
export default function StrategyExplanationPage() {
  const [p] = useSearchParams();
  const runId = p.get("run") ?? undefined;
  const strategyId = p.get("strategy") ?? undefined;
  const version = Number(p.get("version"));
  return (
    <div className="space-y-6">
      <PageHeader title="策略说明书" description="冻结规则、实际方向与执行证据" />
      {runId || (strategyId && version > 0) ? (
        <StrategyExplanation runId={runId} strategyId={strategyId} version={version} />
      ) : (
        <p>请从指定策略版本或研究运行进入，避免用最新策略解释历史运行。</p>
      )}
    </div>
  );
}
