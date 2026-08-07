import * as React from "react";
import { Link } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  Sparkles,
  Send,
  ThumbsUp,
  ThumbsDown,
  Check,
  FileText,
  MessageSquare,
  History,
  ShieldCheck,
  AlertCircle,
  ExternalLink,
  Workflow,
  Database,
  Atom,
  SlidersHorizontal,
  TestTube,
  PlayCircle,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardHeader, CardTitle, CardContent, CardDescription } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Textarea, Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Separator } from "@/components/ui/separator";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
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
import { aiResearchApi, type DraftOut, type AnswerOut } from "@/lib/ai";
import { cn, formatDateTime, timeAgo } from "@/lib/utils";

export default function AIResearch() {
  return (
    <div>
      <PageHeader
        title="AI 研究助手"
        description="因子假设草案、策略 diff、金融问答与假设审批闭环"
      />

      <Alert variant="warning" className="mb-4">
        <ShieldCheck className="h-4 w-4" />
        <AlertTitle>AI 边界</AlertTitle>
        <AlertDescription>
          AI 助手只读研究上下文，不连接实盘账户、不发送订单、不修改持仓。
          AI 输出必须经过人工审批后才可消费。金融问答引用项目来源，数据不足时明确声明。
        </AlertDescription>
      </Alert>

      <Tabs defaultValue="ask">
        <TabsList>
          <TabsTrigger value="orchestrate"><Workflow className="mr-1.5 h-4 w-4" />研究编排</TabsTrigger>
          <TabsTrigger value="ask"><MessageSquare className="mr-1.5 h-4 w-4" />金融问答</TabsTrigger>
          <TabsTrigger value="drafts"><Sparkles className="mr-1.5 h-4 w-4" />AI 草案</TabsTrigger>
          <TabsTrigger value="hypotheses"><FileText className="mr-1.5 h-4 w-4" />假设管理</TabsTrigger>
          <TabsTrigger value="audit"><History className="mr-1.5 h-4 w-4" />审计</TabsTrigger>
        </TabsList>

        <TabsContent value="ask"><AskTab /></TabsContent>
        <TabsContent value="drafts"><DraftsTab /></TabsContent>
        <TabsContent value="hypotheses"><HypothesesTab /></TabsContent>
        <TabsContent value="audit"><AuditTab /></TabsContent>
        <TabsContent value="orchestrate"><OrchestrateTab /></TabsContent>
      </Tabs>
    </div>
  );
}

/* ============================================================ */
/* Orchestrate Tab                                              */
/* ============================================================ */

const orchestrateTemplates = [
  {
    label: "动量因子研究",
    prompt:
      "我想研究动量因子在 A 股大盘 ETF 上的有效性。请帮我设计一个完整的研究方案：选择数据、构建动量因子、定义信号规则、设置验证计划。",
  },
  {
    label: "多因子选股策略",
    prompt:
      "帮我设计一个多因子选股策略，结合价值、动量和低波动因子。目标是构建一个 A 股市场的 long-only 组合。",
  },
  {
    label: "ETF 轮动策略",
    prompt:
      "我想研究一个基于动量和趋势的 ETF 轮动策略，在股票、债券和商品 ETF 之间切换。",
  },
];

const workflowCards: { to: string; icon: LucideIcon; title: string; desc: string }[] = [
  { to: "/research/data", icon: Database, title: "数据准备", desc: "拉取行情数据并发布研究数据集" },
  { to: "/research/factors", icon: Atom, title: "因子选择", desc: "浏览因子目录、创建因子实验" },
  { to: "/research/strategy", icon: SlidersHorizontal, title: "策略配置", desc: "用无代码组件构建策略规格" },
  { to: "/research/experiments", icon: TestTube, title: "实验验证", desc: "OOS 检验排除过拟合" },
  { to: "/research/simulation", icon: PlayCircle, title: "模拟交易", desc: "纸面撮合验证实际表现" },
];

function useAIAnswerStream() {
  const [answer, setAnswer] = React.useState<AnswerOut | null>(null);
  const [reasoning, setReasoning] = React.useState("");
  const [status, setStatus] = React.useState("AI 正在思考");

  const askMutation = useMutation({
    mutationFn: (question: string) => {
      setAnswer(null);
      setReasoning("");
      setStatus("AI 正在思考");
      return aiResearchApi.askStream(question, {
        onStatus: setStatus,
        onReasoning: (text) => setReasoning((current) => current + text),
        onToolCall: () => setStatus("AI 正在处理研究工具结果"),
      });
    },
    onSuccess: setAnswer,
    onError: () => setStatus("AI 请求失败"),
  });

  return { answer, reasoning, status, askMutation };
}

function ReasoningPanel({ reasoning, status }: { reasoning: string; status: string }) {
  if (!reasoning && !status) return null;
  return (
    <div className="rounded-md border border-primary/20 bg-primary/5 p-3">
      <div className="mb-1 flex items-center gap-2 text-xs font-medium text-primary">
        <Sparkles className="h-3.5 w-3.5" />
        思考过程
        <span className="font-normal text-muted-foreground">{status}</span>
      </div>
      <p className="max-h-56 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-muted-foreground">
        {reasoning || "等待模型输出思考 token..."}
      </p>
    </div>
  );
}

function OrchestrateTab() {
  const [prompt, setPrompt] = React.useState("");
  const { answer, reasoning, status, askMutation } = useAIAnswerStream();

  const handleSubmit = () => {
    if (!prompt.trim()) return;
    askMutation.mutate(prompt.trim());
  };

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Workflow className="h-4 w-4" />
            研究编排
          </CardTitle>
          <CardDescription>描述研究目标，AI 引导完成完整研究流程</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="flex flex-wrap gap-2">
            {orchestrateTemplates.map((t) => (
              <Button
                key={t.label}
                size="sm"
                variant="outline"
                onClick={() => setPrompt(t.prompt)}
              >
                {t.label}
              </Button>
            ))}
          </div>
          <div>
            <Label htmlFor="orchestrate-prompt">研究目标</Label>
            <Textarea
              id="orchestrate-prompt"
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="描述你想研究的策略或因子..."
              className="min-h-[120px]"
              aria-label="研究目标输入"
            />
          </div>
          <Button onClick={handleSubmit} disabled={!prompt.trim() || askMutation.isPending}>
            <Sparkles className="mr-2 h-4 w-4" />
            开始研究
          </Button>
          {askMutation.isError && (
            <Alert variant="destructive">
              <AlertTitle>请求失败</AlertTitle>
              <AlertDescription className="text-xs">
                {askMutation.error instanceof Error ? askMutation.error.message : "AI 服务不可用，请稍后重试"}
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      {askMutation.isPending && (
        <Card>
          <CardContent className="py-12">
            <div className="flex flex-col items-center text-center">
              <Workflow className="mb-3 h-10 w-10 animate-pulse text-primary/50" />
              <p className="mb-4 text-sm text-muted-foreground">{status}</p>
              <div className="w-full max-w-2xl text-left">
                <ReasoningPanel reasoning={reasoning} status={status} />
              </div>
            </div>
          </CardContent>
        </Card>
      )}

      {answer && (
        <>
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <Sparkles className="h-4 w-4" />
                AI 引导
              </CardTitle>
            </CardHeader>
            <CardContent>
              <ScrollArea className="max-h-[500px]">
                <div className="space-y-4">
                  <ReasoningPanel reasoning={reasoning} status={status} />
                  <div>
                    <div className="mb-1 text-xs font-medium text-muted-foreground">回答</div>
                    <p className="text-sm leading-relaxed">{answer.answer}</p>
                  </div>

                  {answer.technical_detail && (
                    <div>
                      <div className="mb-1 text-xs font-medium text-muted-foreground">技术细节</div>
                      <p className="font-mono text-xs leading-relaxed text-muted-foreground">
                        {answer.technical_detail}
                      </p>
                    </div>
                  )}

                  {!answer.data_sufficient && answer.disclaimer && (
                    <Alert variant="warning">
                      <AlertCircle className="h-4 w-4" />
                      <AlertDescription>{answer.disclaimer}</AlertDescription>
                    </Alert>
                  )}

                  {answer.uncertainty && (
                    <div>
                      <div className="mb-1 text-xs font-medium text-muted-foreground">不确定性</div>
                      <p className="text-xs italic text-muted-foreground">{answer.uncertainty}</p>
                    </div>
                  )}

                  {answer.citations.length > 0 && (
                    <div>
                      <div className="mb-2 text-xs font-medium text-muted-foreground">引用来源</div>
                      <div className="space-y-1">
                        {answer.citations.map((c, i) => (
                          <div key={i} className="flex items-start gap-2 text-xs">
                            <Badge variant="secondary" className="shrink-0 text-[10px]">
                              {c.source}
                            </Badge>
                            <span className="text-foreground/80">{c.reference}</span>
                            {c.url && (
                              <a
                                href={c.url}
                                target="_blank"
                                rel="noopener noreferrer"
                                className="text-primary hover:underline"
                              >
                                <ExternalLink className="h-3 w-3" />
                              </a>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}

                  <Separator />
                  <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                    <Badge variant="outline" className="text-[10px]">
                      {answer.provenance.provider}
                    </Badge>
                    <span>{answer.provenance.model_version}</span>
                    <span>·</span>
                    <span>{answer.provenance.latency_ms}ms</span>
                  </div>
                </div>
              </ScrollArea>
            </CardContent>
          </Card>

          <div>
            <div className="mb-2 flex items-center gap-2">
              <Workflow className="h-4 w-4 text-primary" />
              <span className="text-sm font-medium">研究工作流</span>
            </div>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
              {workflowCards.map((card, idx) => {
                const Icon = card.icon;
                return (
                  <Link key={card.to} to={card.to}>
                    <Card className="transition-colors hover:border-primary/50 hover:bg-accent">
                      <CardContent className="flex items-start gap-3 p-4">
                        <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-primary/10 text-primary">
                          <Icon className="h-4 w-4" />
                        </div>
                        <div className="min-w-0">
                          <div className="flex items-center gap-1.5">
                            <span className="text-[10px] text-muted-foreground">
                              {String(idx + 1).padStart(2, "0")}
                            </span>
                            <span className="text-sm font-medium">{card.title}</span>
                          </div>
                          <p className="mt-0.5 text-xs text-muted-foreground">{card.desc}</p>
                        </div>
                      </CardContent>
                    </Card>
                  </Link>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

/* ============================================================ */
/* Ask Tab                                                      */
/* ============================================================ */

function AskTab() {
  const [question, setQuestion] = React.useState("");
  const { answer, reasoning, status, askMutation } = useAIAnswerStream();

  const handleSubmit = () => {
    if (!question.trim()) return;
    askMutation.mutate(question.trim());
  };

  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle className="text-sm">提出问题</CardTitle>
          <CardDescription>基于项目研究上下文的金融问答</CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <div>
            <Label htmlFor="question">问题</Label>
            <Textarea
              id="question"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="例如：动量因子在 A 股小盘股上的有效性如何？"
              className="min-h-[100px]"
              aria-label="金融问题输入"
            />
          </div>
          <Button onClick={handleSubmit} disabled={!question.trim() || askMutation.isPending}>
            {askMutation.isPending ? (
              <>
                <Skeleton className="h-4 w-4 rounded-full" />
                思考中...
              </>
            ) : (
              <>
                <Send className="mr-2 h-4 w-4" />
                提问
              </>
            )}
          </Button>
          {askMutation.isError && (
            <Alert variant="destructive">
              <AlertTitle>请求失败</AlertTitle>
              <AlertDescription className="text-xs">
                {askMutation.error instanceof Error ? askMutation.error.message : "AI 服务不可用，请稍后重试"}
              </AlertDescription>
            </Alert>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2 text-sm">
            <Sparkles className="h-4 w-4" />
            AI 回答
          </CardTitle>
        </CardHeader>
        <CardContent>
          {!answer ? (
            <div className="flex flex-col items-center py-12 text-center">
              <MessageSquare className="mb-3 h-10 w-10 text-muted-foreground/30" />
              {askMutation.isPending ? (
                <div className="w-full text-left">
                  <ReasoningPanel reasoning={reasoning} status={status} />
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">提出问题后，AI 将基于项目上下文回答</p>
              )}
            </div>
          ) : (
            <ScrollArea className="max-h-[600px]">
              <div className="space-y-4">
                <ReasoningPanel reasoning={reasoning} status={status} />
                <div>
                  <div className="mb-1 text-xs font-medium text-muted-foreground">回答</div>
                  <p className="text-sm leading-relaxed">{answer.answer}</p>
                </div>

                {answer.technical_detail && (
                  <div>
                    <div className="mb-1 text-xs font-medium text-muted-foreground">技术细节</div>
                    <p className="font-mono text-xs leading-relaxed text-muted-foreground">{answer.technical_detail}</p>
                  </div>
                )}

                {!answer.data_sufficient && answer.disclaimer && (
                  <Alert variant="warning">
                    <AlertCircle className="h-4 w-4" />
                    <AlertDescription>{answer.disclaimer}</AlertDescription>
                  </Alert>
                )}

                {answer.uncertainty && (
                  <div>
                    <div className="mb-1 text-xs font-medium text-muted-foreground">不确定性</div>
                    <p className="text-xs italic text-muted-foreground">{answer.uncertainty}</p>
                  </div>
                )}

                {answer.citations.length > 0 && (
                  <div>
                    <div className="mb-2 text-xs font-medium text-muted-foreground">引用来源</div>
                    <div className="space-y-1">
                      {answer.citations.map((c, i) => (
                        <div key={i} className="flex items-start gap-2 text-xs">
                          <Badge variant="secondary" className="shrink-0 text-[10px]">{c.source}</Badge>
                          <span className="text-foreground/80">{c.reference}</span>
                          {c.url && (
                            <a href={c.url} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
                              <ExternalLink className="h-3 w-3" />
                            </a>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <Separator />
                <div className="flex items-center gap-2 text-[10px] text-muted-foreground">
                  <Badge variant="outline" className="text-[10px]">{answer.provenance.provider}</Badge>
                  <span>{answer.provenance.model_version}</span>
                  <span>·</span>
                  <span>{answer.provenance.latency_ms}ms</span>
                </div>
              </div>
            </ScrollArea>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

/* ============================================================ */
/* Drafts Tab                                                   */
/* ============================================================ */

function DraftsTab() {
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

/* ============================================================ */
/* Hypotheses Tab                                               */
/* ============================================================ */

function HypothesesTab() {
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

/* ============================================================ */
/* Audit Tab                                                    */
/* ============================================================ */

function AuditTab() {
  const { data: events, isLoading } = useQuery({
    queryKey: ["ai-audit"],
    queryFn: () => aiResearchApi.audit(),
  });

  if (isLoading) {
    return (
      <Card>
        <CardContent className="p-4">
          <Skeleton className="h-32 w-full" />
        </CardContent>
      </Card>
    );
  }

  if (!events || events.length === 0) {
    return (
      <Card>
        <CardContent className="py-12">
          <div className="flex flex-col items-center text-center">
            <History className="mb-3 h-10 w-10 text-muted-foreground/30" />
            <p className="text-sm text-muted-foreground">暂无审计事件</p>
          </div>
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-sm">AI 审计日志</CardTitle>
      </CardHeader>
      <CardContent className="p-0">
        <ScrollArea className="max-h-[600px]">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="text-xs">事件类型</TableHead>
                <TableHead className="text-xs">操作者</TableHead>
                <TableHead className="text-xs">假设 ID</TableHead>
                <TableHead className="text-xs">详情</TableHead>
                <TableHead className="text-xs">时间</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {events.map((e) => (
                <TableRow key={e.event_id}>
                  <TableCell>
                    <Badge variant="outline" className="text-[10px] font-mono">{e.event_type}</Badge>
                  </TableCell>
                  <TableCell className="text-xs">{e.actor}</TableCell>
                  <TableCell className="font-mono text-xs">{e.hypothesis_id ?? "—"}</TableCell>
                  <TableCell>
                    <code className="text-[10px] text-muted-foreground">
                      {JSON.stringify(e.payload).slice(0, 80)}
                    </code>
                  </TableCell>
                  <TableCell className="text-xs text-muted-foreground">{formatDateTime(e.created_at)}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </ScrollArea>
      </CardContent>
    </Card>
  );
}
