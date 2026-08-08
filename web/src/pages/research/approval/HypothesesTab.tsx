import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ThumbsUp, ThumbsDown, FileText, AlertCircle } from "lucide-react";
import { Card, CardHeader, CardTitle, CardContent, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Textarea } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { aiResearchApi } from "@/lib/ai";
import { cn, formatDateTime } from "@/lib/utils";

export function HypothesesTab() {
  const qc = useQueryClient();
  const [selected, setSelected] = React.useState<string | null>(null);

  const { data: hypotheses } = useQuery({
    queryKey: ["ai-hypotheses"],
    queryFn: () => aiResearchApi.listHypotheses(),
  });

  const { data: detail } = useQuery({
    queryKey: ["ai-hypothesis", selected],
    queryFn: () => aiResearchApi.hypothesisDetail(selected!),
    enabled: !!selected,
  });

  const { data: experiments } = useQuery({
    queryKey: ["ai-hypothesis-experiments", selected],
    queryFn: () => aiResearchApi.listExperiments(selected!),
    enabled: !!selected,
  });

  const approveMutation = useMutation({
    mutationFn: (id: string) => aiResearchApi.approveHypothesis(id, "user"),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-hypotheses"] });
      qc.invalidateQueries({ queryKey: ["ai-hypothesis"] });
    },
  });
  const rejectMutation = useMutation({
    mutationFn: ({ id, reason }: { id: string; reason: string }) =>
      aiResearchApi.rejectHypothesis(id, "user", reason),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["ai-hypotheses"] });
      qc.invalidateQueries({ queryKey: ["ai-hypothesis"] });
    },
  });

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-3">
      {/* List */}
      <div className="lg:col-span-1">
        <Card>
          <CardHeader>
            <CardTitle className="text-sm">因子假设</CardTitle>
          </CardHeader>
          <CardContent className="p-2">
            <ScrollArea className="max-h-[600px]">
              <div className="space-y-1">
                {hypotheses?.map((h) => (
                  <button
                    key={h.hypothesis_id}
                    onClick={() => setSelected(h.hypothesis_id)}
                    className={cn(
                      "w-full rounded-md px-3 py-2 text-left transition-colors",
                      selected === h.hypothesis_id
                        ? "bg-primary/10 text-primary"
                        : "hover:bg-accent text-muted-foreground",
                    )}
                  >
                    <div className="flex items-center justify-between">
                      <span className="truncate text-sm font-medium">{h.name}</span>
                      <StatusBadge status={h.status} />
                    </div>
                    <div className="mt-0.5 truncate text-xs text-muted-foreground/70">{h.economic_mechanism}</div>
                  </button>
                ))}
                {(!hypotheses || hypotheses.length === 0) && (
                  <p className="py-4 text-center text-sm text-muted-foreground">暂无假设</p>
                )}
              </div>
            </ScrollArea>
          </CardContent>
        </Card>
      </div>

      {/* Detail */}
      <div className="lg:col-span-2">
        {!detail ? (
          <Card>
            <CardContent className="py-12">
              <div className="flex flex-col items-center text-center">
                <FileText className="mb-3 h-10 w-10 text-muted-foreground/30" />
                <p className="text-sm text-muted-foreground">请从左侧选择一个假设</p>
              </div>
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardHeader>
              <div className="flex items-start justify-between">
                <div>
                  <CardTitle className="text-base">{detail.name}</CardTitle>
                  <CardDescription className="mt-1">{detail.economic_mechanism}</CardDescription>
                </div>
                <StatusBadge status={detail.status} />
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-2 gap-3 text-sm">
                <div>
                  <span className="text-xs text-muted-foreground">方向</span>
                  <p className="font-medium">{detail.direction}</p>
                </div>
                <div>
                  <span className="text-xs text-muted-foreground">决策时点</span>
                  <p className="font-medium">{detail.decision_timing}</p>
                </div>
              </div>

              <Separator />

              <div>
                <div className="mb-1 text-xs text-muted-foreground">公式</div>
                <code className="block rounded bg-muted/50 p-2 font-mono text-xs">{detail.formula}</code>
              </div>

              {detail.input_fields.length > 0 && (
                <div>
                  <div className="mb-1 text-xs text-muted-foreground">输入字段</div>
                  <div className="flex flex-wrap gap-1">
                    {detail.input_fields.map((f) => (
                      <Badge key={f} variant="secondary" className="text-[10px]">{f}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {detail.applicable_assets.length > 0 && (
                <div>
                  <div className="mb-1 text-xs text-muted-foreground">适用资产</div>
                  <div className="flex flex-wrap gap-1">
                    {detail.applicable_assets.map((a) => (
                      <Badge key={a} variant="info" className="text-[10px]">{a}</Badge>
                    ))}
                  </div>
                </div>
              )}

              {detail.expected_failure_scenarios.length > 0 && (
                <div>
                  <div className="mb-1 text-xs text-muted-foreground">预期失效场景</div>
                  <ul className="space-y-1">
                    {detail.expected_failure_scenarios.map((s, i) => (
                      <li key={i} className="flex items-start gap-2 text-xs">
                        <AlertCircle className="mt-0.5 h-3 w-3 shrink-0 text-warning" />
                        {s}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {detail.references.length > 0 && (
                <div>
                  <div className="mb-1 text-xs text-muted-foreground">参考文献</div>
                  <ul className="space-y-1">
                    {detail.references.map((r, i) => (
                      <li key={i} className="text-xs text-primary">{r}</li>
                    ))}
                  </ul>
                </div>
              )}

              {experiments && experiments.length > 0 && (
                <>
                  <Separator />
                  <div>
                    <div className="mb-2 text-xs text-muted-foreground">实验</div>
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead className="h-8 text-xs">实验 ID</TableHead>
                          <TableHead className="h-8 text-xs">状态</TableHead>
                          <TableHead className="h-8 text-xs">验证实验</TableHead>
                          <TableHead className="h-8 text-xs">时间</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {experiments.map((exp) => (
                          <TableRow key={exp.experiment_id}>
                            <TableCell className="font-mono text-xs">{exp.experiment_id}</TableCell>
                            <TableCell><StatusBadge status={exp.status} /></TableCell>
                            <TableCell className="font-mono text-xs">{exp.validation_experiment_id ?? "—"}</TableCell>
                            <TableCell className="text-xs text-muted-foreground">{formatDateTime(exp.created_at)}</TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </>
              )}

              {detail.status === "proposed" && (
                <div className="flex gap-2">
                  <Button variant="success" size="sm" onClick={() => approveMutation.mutate(detail.hypothesis_id)}>
                    <ThumbsUp className="mr-1.5 h-4 w-4" />
                    审批
                  </Button>
                  <RejectHypothesisButton
                    onReject={(reason) => rejectMutation.mutate({ id: detail.hypothesis_id, reason })}
                  />
                </div>
              )}
            </CardContent>
          </Card>
        )}
      </div>
    </div>
  );
}

function RejectHypothesisButton({ onReject }: { onReject: (reason: string) => void }) {
  const [open, setOpen] = React.useState(false);
  const [reason, setReason] = React.useState("");

  return (
    <>
      <Button variant="destructive" size="sm" onClick={() => setOpen(true)}>
        <ThumbsDown className="mr-1.5 h-4 w-4" />
        拒绝
      </Button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>拒绝假设</DialogTitle>
            <DialogDescription>请说明拒绝原因</DialogDescription>
          </DialogHeader>
          <div>
            <Label htmlFor="hyp-reject-reason">原因</Label>
            <Textarea
              id="hyp-reject-reason"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </div>
          <DialogFooter>
            <Button
              variant="destructive"
              disabled={!reason.trim()}
              onClick={() => {
                if (reason.trim()) {
                  onReject(reason.trim());
                  setOpen(false);
                  setReason("");
                }
              }}
            >
              确认拒绝
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
