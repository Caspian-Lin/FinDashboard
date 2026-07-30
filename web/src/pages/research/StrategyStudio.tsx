import * as React from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  ShieldCheck,
  Send,
  FilePlus,
  Undo2,
  CheckCircle2,
  Lock,
  Info,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertTitle, AlertDescription } from "@/components/ui/alert";
import { Separator } from "@/components/ui/separator";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/ui/states";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/input";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableHeader,
  TableBody,
  TableRow,
  TableHead,
  TableCell,
} from "@/components/ui/table";
import { strategySpecApi } from "@/lib/research";
import { cn, formatDateTime } from "@/lib/utils";

export default function StrategyStudio() {
  const qc = useQueryClient();
  const [selectedKind, setSelectedKind] = React.useState<string | null>(null);
  const [specText, setSpecText] = React.useState("");
  const [showPublishDialog, setShowPublishDialog] = React.useState(false);
  const [showRollbackDialog, setShowRollbackDialog] = React.useState(false);

  const { data: registry, isLoading: registryLoading } = useQuery({
    queryKey: ["spec-registry"],
    queryFn: strategySpecApi.registry,
  });

  const { data: strategies } = useQuery({
    queryKey: ["spec-list"],
    queryFn: () => strategySpecApi.list(50),
  });

  const { data: template } = useQuery({
    queryKey: ["spec-template", selectedKind],
    queryFn: () => strategySpecApi.template(selectedKind!),
    enabled: !!selectedKind,
  });

  const { data: history } = useQuery({
    queryKey: ["spec-history", selectedKind],
    queryFn: () => strategySpecApi.history(selectedKind!),
    enabled: !!selectedKind,
  });

  React.useEffect(() => {
    if (template?.spec) {
      setSpecText(JSON.stringify(template.spec, null, 2));
    }
  }, [template]);

  const validateMutation = useMutation({
    mutationFn: (spec: Record<string, unknown>) => strategySpecApi.validate(spec),
  });

  const draftMutation = useMutation({
    mutationFn: ({ spec, expectedVersion }: { spec: Record<string, unknown>; expectedVersion: number }) =>
      strategySpecApi.createDraft(spec, expectedVersion),
    onSuccess: () => qc.invalidateQueries({ queryKey: ["spec-list"] }),
  });

  const publishMutation = useMutation({
    mutationFn: ({ strategyId, version, expectedVersion }: { strategyId: string; version: number; expectedVersion: number }) =>
      strategySpecApi.publish(strategyId, version, expectedVersion),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spec-list"] });
      qc.invalidateQueries({ queryKey: ["spec-history"] });
    },
  });

  const rollbackMutation = useMutation({
    mutationFn: ({ strategyId, targetVersion, expectedVersion }: { strategyId: string; targetVersion: number; expectedVersion: number }) =>
      strategySpecApi.rollback(strategyId, targetVersion, expectedVersion),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spec-list"] });
      qc.invalidateQueries({ queryKey: ["spec-history"] });
    },
  });

  const handleValidate = () => {
    try {
      const spec = JSON.parse(specText);
      validateMutation.mutate(spec);
    } catch {
      validateMutation.reset();
    }
  };

  const handleCreateDraft = () => {
    try {
      const spec = JSON.parse(specText);
      draftMutation.mutate({ spec, expectedVersion: template?.version ?? 0 });
    } catch {
      return;
    }
  };

  const validation = validateMutation.data;
  const parseError = React.useMemo(() => {
    if (!specText) return null;
    try {
      JSON.parse(specText);
      return null;
    } catch (e) {
      return (e as Error).message;
    }
  }, [specText]);

  return (
    <div>
      <PageHeader
        title="策略 Studio"
        description="无代码结构化策略配置 — 白名单组件、即时校验、版本管理"
      />

      {/* Security boundary alert */}
      <Alert variant="info" className="mb-4">
        <Info className="h-4 w-4" />
        <AlertTitle>安全边界</AlertTitle>
        <AlertDescription>
          策略 Studio 只接受结构化无代码配置，不支持 Python 编辑、源码上传或模块导入。
          保存和发布不会自动启动任何运行。
          {registry && !registry.accepts_python && (
            <Badge variant="success" className="ml-2">Python 禁用</Badge>
          )}
        </AlertDescription>
      </Alert>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-4">
        {/* Left: Strategy list */}
        <div className="lg:col-span-1">
          <Card>
            <CardHeader>
              <CardTitle className="text-sm">策略类型</CardTitle>
            </CardHeader>
            <CardContent className="p-2">
              {registryLoading ? (
                <div className="space-y-2 p-2">
                  <Skeleton className="h-10 w-full" />
                  <Skeleton className="h-10 w-full" />
                </div>
              ) : (
                <ScrollArea className="h-[200px]">
                  <div className="space-y-1">
                    {registry?.strategies.map((s) => (
                      <button
                        key={s.kind}
                        onClick={() => setSelectedKind(s.kind)}
                        className={cn(
                          "w-full rounded-md px-3 py-2 text-left text-sm transition-colors",
                          selectedKind === s.kind
                            ? "bg-primary/10 text-primary"
                            : "hover:bg-accent text-muted-foreground",
                        )}
                      >
                        <div className="font-medium">{s.display_name}</div>
                        <div className="text-xs text-muted-foreground/70">{s.kind}</div>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              )}
            </CardContent>
          </Card>

          {/* Saved strategies */}
          {strategies && strategies.length > 0 && (
            <Card className="mt-4">
              <CardHeader>
                <CardTitle className="text-sm">已保存策略</CardTitle>
              </CardHeader>
              <CardContent className="p-2">
                <ScrollArea className="h-[200px]">
                  <div className="space-y-1">
                    {strategies.map((s) => (
                      <button
                        key={s.strategy_id}
                        onClick={() => setSelectedKind(s.strategy_id)}
                        className={cn(
                          "w-full rounded-md px-3 py-2 text-left text-sm transition-colors",
                          selectedKind === s.strategy_id
                            ? "bg-primary/10 text-primary"
                            : "hover:bg-accent text-muted-foreground",
                        )}
                      >
                        <div className="flex items-center justify-between">
                          <span className="font-medium">{s.strategy_id}</span>
                          {s.published && <Lock className="h-3 w-3 text-success" />}
                        </div>
                        <div className="text-xs text-muted-foreground/70">v{s.version}</div>
                      </button>
                    ))}
                  </div>
                </ScrollArea>
              </CardContent>
            </Card>
          )}
        </div>

        {/* Center: Editor */}
        <div className="lg:col-span-2">
          <Card className="h-full">
            <CardHeader>
              <div className="flex items-center justify-between">
                <CardTitle className="flex items-center gap-2 text-base">
                  {selectedKind ? `编辑: ${selectedKind}` : "请选择策略类型"}
                </CardTitle>
                {selectedKind && (
                  <div className="flex gap-2">
                    <Button size="sm" variant="outline" onClick={handleValidate} disabled={!specText || !!parseError}>
                      <ShieldCheck className="mr-1.5 h-4 w-4" />
                      校验
                    </Button>
                    <Button size="sm" variant="outline" onClick={handleCreateDraft} disabled={!specText || !!parseError}>
                      <FilePlus className="mr-1.5 h-4 w-4" />
                      保存草稿
                    </Button>
                  </div>
                )}
              </div>
            </CardHeader>
            <CardContent>
              {!selectedKind ? (
                <EmptyState title="未选择策略" description="请从左侧选择一个策略类型开始配置" />
              ) : (
                <div className="space-y-3">
                  {parseError && (
                    <Alert variant="destructive">
                      <AlertTitle>JSON 语法错误</AlertTitle>
                      <AlertDescription className="font-mono text-xs">{parseError}</AlertDescription>
                    </Alert>
                  )}
                  <Textarea
                    value={specText}
                    onChange={(e) => setSpecText(e.target.value)}
                    className="min-h-[500px] resize-y font-mono text-xs"
                    spellCheck={false}
                    aria-label="策略规格 JSON"
                  />
                </div>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: Validation + Version */}
        <div className="lg:col-span-1">
          {/* Validation result */}
          <Card className="mb-4">
            <CardHeader>
              <CardTitle className="flex items-center gap-2 text-sm">
                <ShieldCheck className="h-4 w-4" />
                校验结果
              </CardTitle>
            </CardHeader>
            <CardContent>
              {validateMutation.isPending ? (
                <Skeleton className="h-20 w-full" />
              ) : validation ? (
                <div className="space-y-3">
                  <div className="flex items-center gap-2">
                    {validation.can_execute ? (
                      <Badge variant="success"><CheckCircle2 className="mr-1 h-3 w-3" />可执行</Badge>
                    ) : (
                      <Badge variant="destructive">不可执行</Badge>
                    )}
                  </div>
                  {validation.errors && validation.errors.length > 0 && (
                    <div className="space-y-1">
                      {validation.errors.map((err, i) => (
                        <div key={i} className="rounded bg-destructive/10 px-2 py-1 text-xs text-destructive">
                          {err}
                        </div>
                      ))}
                    </div>
                  )}
                  <Separator />
                  <div className="space-y-1 text-xs">
                    <div className="text-muted-foreground">checksum</div>
                    <code className="break-all text-[10px] text-foreground/70">{validation.checksum}</code>
                  </div>
                  {validation.feature_order.length > 0 && (
                    <div>
                      <div className="mb-1 text-xs text-muted-foreground">特征顺序</div>
                      <div className="flex flex-wrap gap-1">
                        {validation.feature_order.map((f) => (
                          <Badge key={f} variant="secondary" className="text-[10px]">{f}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                  {validation.required_datasets.length > 0 && (
                    <div>
                      <div className="mb-1 text-xs text-muted-foreground">依赖数据集</div>
                      <div className="flex flex-wrap gap-1">
                        {validation.required_datasets.map((d) => (
                          <Badge key={d} variant="info" className="text-[10px]">{d}</Badge>
                        ))}
                      </div>
                    </div>
                  )}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">点击"校验"检查规格有效性</p>
              )}
            </CardContent>
          </Card>

          {/* Version history */}
          {selectedKind && (
            <Card>
              <CardHeader>
                <div className="flex items-center justify-between">
                  <CardTitle className="text-sm">版本历史</CardTitle>
                  <div className="flex gap-1">
                    <Button size="sm" variant="ghost" onClick={() => setShowPublishDialog(true)}>
                      <Send className="h-3.5 w-3.5" />
                    </Button>
                    <Button size="sm" variant="ghost" onClick={() => setShowRollbackDialog(true)}>
                      <Undo2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                </div>
              </CardHeader>
              <CardContent className="p-2">
                <ScrollArea className="max-h-[300px]">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead className="h-8 text-xs">版本</TableHead>
                        <TableHead className="h-8 text-xs">状态</TableHead>
                        <TableHead className="h-8 text-xs">时间</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {history?.map((v) => (
                        <TableRow key={v.version}>
                          <TableCell className="py-1.5 text-xs font-mono">v{v.version}</TableCell>
                          <TableCell className="py-1.5">
                            {v.published ? <Badge variant="success" className="text-[10px]">已发布</Badge> : <Badge variant="secondary" className="text-[10px]">草稿</Badge>}
                          </TableCell>
                          <TableCell className="py-1.5 text-xs text-muted-foreground">{formatDateTime(v.created_at)}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </ScrollArea>
              </CardContent>
            </Card>
          )}
        </div>
      </div>

      {/* Publish dialog */}
      <PublishDialog
        open={showPublishDialog}
        onOpenChange={setShowPublishDialog}
        strategyId={selectedKind}
        latestVersion={history?.[0]?.version ?? template?.version ?? 0}
        onConfirm={(version, expectedVersion) =>
          publishMutation.mutate(
            { strategyId: selectedKind!, version, expectedVersion },
            { onSuccess: () => setShowPublishDialog(false) },
          )
        }
      />

      {/* Rollback dialog */}
      <RollbackDialog
        open={showRollbackDialog}
        onOpenChange={setShowRollbackDialog}
        history={history ?? []}
        onConfirm={(targetVersion, expectedVersion) =>
          rollbackMutation.mutate(
            { strategyId: selectedKind!, targetVersion, expectedVersion },
            { onSuccess: () => setShowRollbackDialog(false) },
          )
        }
      />
    </div>
  );
}

function PublishDialog({
  open,
  onOpenChange,
  strategyId,
  latestVersion,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  strategyId: string | null;
  latestVersion: number;
  onConfirm: (version: number, expectedVersion: number) => void;
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>发布策略版本</DialogTitle>
          <DialogDescription>
            发布策略 {strategyId} 的指定版本。发布不会自动启动运行。
          </DialogDescription>
        </DialogHeader>
        <PublishForm latestVersion={latestVersion} onConfirm={onConfirm} />
      </DialogContent>
    </Dialog>
  );
}

function PublishForm({
  latestVersion,
  onConfirm,
}: {
  latestVersion: number;
  onConfirm: (version: number, expectedVersion: number) => void;
}) {
  const [version, setVersion] = React.useState(latestVersion);
  const [expectedVersion, setExpectedVersion] = React.useState(latestVersion);

  React.useEffect(() => {
    setVersion(latestVersion);
    setExpectedVersion(latestVersion);
  }, [latestVersion]);

  return (
    <div className="space-y-4">
      <div>
        <Label htmlFor="publish-version">发布版本号</Label>
        <Input
          id="publish-version"
          type="number"
          value={version}
          onChange={(e) => setVersion(Number(e.target.value))}
        />
      </div>
      <div>
        <Label htmlFor="expected-version">期望当前版本（乐观锁）</Label>
        <Input
          id="expected-version"
          type="number"
          value={expectedVersion}
          onChange={(e) => setExpectedVersion(Number(e.target.value))}
        />
      </div>
      <DialogFooter>
        <Button onClick={() => onConfirm(version, expectedVersion)}>
          <Send className="mr-2 h-4 w-4" />
          确认发布
        </Button>
      </DialogFooter>
    </div>
  );
}

function RollbackDialog({
  open,
  onOpenChange,
  history,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  history: { version: number; published: boolean; created_at: string }[];
  onConfirm: (targetVersion: number, expectedVersion: number) => void;
}) {
  const [target, setTarget] = React.useState(history[0]?.version ?? 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>回滚策略版本</DialogTitle>
          <DialogDescription>选择要回滚到的目标版本</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="max-h-[200px] overflow-y-auto scrollbar-thin">
            {history.map((v) => (
              <button
                key={v.version}
                onClick={() => setTarget(v.version)}
                className={cn(
                  "flex w-full items-center justify-between rounded-md px-3 py-2 text-sm transition-colors",
                  target === v.version ? "bg-primary/10 text-primary" : "hover:bg-accent",
                )}
              >
                <span className="font-mono">v{v.version}</span>
                {v.published && <Badge variant="success" className="text-[10px]">已发布</Badge>}
              </button>
            ))}
          </div>
          <DialogFooter>
            <Button onClick={() => onConfirm(target, history[0]?.version ?? 0)}>
              <Undo2 className="mr-2 h-4 w-4" />
              回滚到 v{target}
            </Button>
          </DialogFooter>
        </div>
      </DialogContent>
    </Dialog>
  );
}
