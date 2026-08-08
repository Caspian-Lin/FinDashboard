import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { Sparkles, ThumbsUp, ThumbsDown, Check } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Textarea, Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { aiResearchApi, type DraftOut } from "@/lib/ai";
import { timeAgo } from "@/lib/utils";

export function DraftsTab() {
  const qc = useQueryClient();
  const [generating, setGenerating] = React.useState(false);
  const [genQuestion, setGenQuestion] = React.useState("");
  const [genKind, setGenKind] = React.useState<"hypothesis" | "strategy_component" | "strategy_diff">("hypothesis");

  const { data: drafts, isLoading } = useQuery({
    queryKey: ["ai-drafts"],
    queryFn: () => aiResearchApi.listDrafts(),
  });

  const genMutation = useMutation({
    mutationFn: ({ kind, question }: { kind: string; question: string }) => {
      if (kind === "hypothesis") return aiResearchApi.generateHypothesisDraft(question);
      if (kind === "strategy_component") return aiResearchApi.generateStrategyComponentDraft(question);
      return aiResearchApi.generateStrategyDiffDraft(question);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-drafts"] });
      setGenerating(false);
    },
    onError: () => setGenerating(false),
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) => aiResearchApi.approveDraft(id, "user"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-drafts"] }),
  });
  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      aiResearchApi.rejectDraft(id, "user", reason),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-drafts"] }),
  });
  const consumeMutation = useMutation({
    mutationFn: (id: string) => aiResearchApi.consumeDraft(id, "user", "manual"),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["ai-drafts"] }),
  });

  return (
    <div className="space-y-4">
      {/* Generate */}
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">生成 AI 草案</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-[160px]">
              <Label className="text-xs">类型</Label>
              <div className="flex gap-1">
                {(["hypothesis", "strategy_component", "strategy_diff"] as const).map((k) => (
                  <Button
                    key={k}
                    size="sm"
                    variant={genKind === k ? "default" : "outline"}
                    onClick={() => setGenKind(k)}
                    className="text-xs"
                  >
                    {k === "hypothesis" ? "假设" : k === "strategy_component" ? "策略组件" : "策略 diff"}
                  </Button>
                ))}
              </div>
            </div>
            <div className="flex-1">
              <Label className="text-xs">提示</Label>
              <Input
                value={genQuestion}
                onChange={(e) => setGenQuestion(e.target.value)}
                placeholder="描述你想要的假设或策略修改..."
                aria-label="生成提示"
              />
            </div>
            <Button
              onClick={() => {
                if (!genQuestion.trim()) return;
                setGenerating(true);
                genMutation.mutate({ kind: genKind, question: genQuestion.trim() });
              }}
              disabled={!genQuestion.trim() || generating}
            >
              <Sparkles className="mr-2 h-4 w-4" />
              生成
            </Button>
          </div>
        </CardContent>
      </Card>

      {/* Draft list */}
      {isLoading ? (
        <div className="space-y-2">
          {Array.from({ length: 3 }).map((_, i) => (
            <Skeleton key={i} className="h-24 w-full" />
          ))}
        </div>
      ) : drafts && drafts.length > 0 ? (
        <div className="space-y-3">
          {drafts.map((draft) => (
            <DraftCard
              key={draft.draft_id}
              draft={draft}
              onApprove={() => approveMutation.mutate(draft.draft_id)}
              onReject={(reason) => rejectMutation.mutate({ id: draft.draft_id, reason })}
              onConsume={() => consumeMutation.mutate(draft.draft_id)}
            />
          ))}
        </div>
      ) : (
        <Card>
          <CardContent className="py-12">
            <div className="flex flex-col items-center text-center">
              <Sparkles className="mb-3 h-10 w-10 text-muted-foreground/30" />
              <p className="text-sm text-muted-foreground">暂无 AI 草案。生成一个开始吧。</p>
            </div>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function DraftCard({
  draft,
  onApprove,
  onReject,
  onConsume,
}: {
  draft: DraftOut;
  onApprove: () => void;
  onReject: (reason: string) => void;
  onConsume: () => void;
}) {
  const [showReject, setShowReject] = React.useState(false);
  const [reason, setReason] = React.useState("");

  return (
    <Card>
      <CardContent className="p-4">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0 flex-1">
            <div className="mb-2 flex flex-wrap items-center gap-2">
              <Badge variant={draft.kind === "hypothesis" ? "info" : draft.kind === "strategy_component" ? "default" : "warning"}>
                {draft.kind}
              </Badge>
              <StatusBadge status={draft.status} />
              <span className="text-[10px] text-muted-foreground">{draft.provenance.model_version}</span>
              <span className="text-[10px] text-muted-foreground">{timeAgo(draft.created_at)}</span>
            </div>
            <pre className="max-h-[200px] overflow-auto scrollbar-thin rounded bg-muted/50 p-2 text-xs font-mono">
              {JSON.stringify(draft.payload, null, 2)}
            </pre>
            {draft.uncertainty && (
              <p className="mt-2 text-xs italic text-muted-foreground">⚠ {draft.uncertainty}</p>
            )}
            {draft.references.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {draft.references.map((r, i) => (
                  <Badge key={i} variant="secondary" className="text-[10px]">{r}</Badge>
                ))}
              </div>
            )}
          </div>
          <div className="shrink-0">
            {draft.status === "proposed" && (
              <div className="flex flex-col gap-1">
                <Button size="sm" variant="success" onClick={onApprove}>
                  <ThumbsUp className="mr-1 h-3.5 w-3.5" />
                  审批
                </Button>
                <Button size="sm" variant="destructive" onClick={() => setShowReject(true)}>
                  <ThumbsDown className="mr-1 h-3.5 w-3.5" />
                  拒绝
                </Button>
              </div>
            )}
            {draft.status === "approved" && (
              <Button size="sm" variant="default" onClick={onConsume}>
                <Check className="mr-1 h-3.5 w-3.5" />
                消费
              </Button>
            )}
          </div>
        </div>
      </CardContent>
      <Dialog open={showReject} onOpenChange={setShowReject}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>拒绝草案</DialogTitle>
            <DialogDescription>请说明拒绝原因</DialogDescription>
          </DialogHeader>
          <div>
            <Label htmlFor="reject-reason">原因</Label>
            <Textarea
              id="reject-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="拒绝原因..."
            />
          </div>
          <DialogFooter>
            <Button
              variant="destructive"
              onClick={() => {
                if (reason.trim()) {
                  onReject(reason.trim());
                  setShowReject(false);
                  setReason("");
                }
              }}
              disabled={!reason.trim()}
            >
              确认拒绝
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  );
}
