import { useQuery } from "@tanstack/react-query";
import {
  Atom,
  Layers,
  Signal,
  TestTube,
  RefreshCw,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import { EmptyState, ErrorState, LoadingState } from "@/components/ui/states";
import {
  factorLabApi,
  type FactorCatalogEntry,
  type FeatureSnapshot,
  type FactorSignal,
  type FactorExperiment,
} from "@/lib/research";
import { cn, formatDateTime, formatNumber } from "@/lib/utils";

function CatalogTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-catalog"],
    queryFn: () => factorLabApi.catalog(),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载因子目录"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {data.map((factor: FactorCatalogEntry) => (
            <Card key={factor.name}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate text-base font-mono">
                      {factor.name}
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      v{factor.version}
                    </p>
                  </div>
                  <Badge variant="info">{factor.role}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <p className="text-sm text-muted-foreground line-clamp-2">
                  {factor.description}
                </p>
                <div className="flex flex-wrap gap-1">
                  {factor.dependencies.length > 0 ? (
                    factor.dependencies.map((dep) => (
                      <Badge key={dep} variant="secondary" className="font-mono">
                        {dep}
                      </Badge>
                    ))
                  ) : (
                    <span className="text-xs text-muted-foreground">
                      无依赖
                    </span>
                  )}
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Atom className="h-8 w-8" />}
          title="暂无因子"
          description="因子目录注册后将在此列出，包含角色、版本与依赖关系。"
        />
      )}
    </div>
  );
}

function FeaturesTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["feature-snapshots", { limit: 50 }],
    queryFn: () => factorLabApi.features(undefined, 50),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个特征快照` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载特征快照"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead>快照 ID</TableHead>
                  <TableHead>数据发布 ID</TableHead>
                  <TableHead className="text-right">因子数</TableHead>
                  <TableHead className="text-right">标的数</TableHead>
                  <TableHead className="text-right">行数</TableHead>
                  <TableHead>研究状态</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((snap: FeatureSnapshot) => (
                  <TableRow key={snap.snapshot_id}>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {snap.snapshot_id}
                      </span>
                    </TableCell>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {snap.dataset_release_id}
                      </span>
                    </TableCell>
                    <TableCell className="tabular-nums text-right">
                      {snap.factor_names.length}
                    </TableCell>
                    <TableCell className="tabular-nums text-right">
                      {formatNumber(snap.symbol_count, 0)}
                    </TableCell>
                    <TableCell className="tabular-nums text-right">
                      {formatNumber(snap.row_count, 0)}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={snap.research_status} />
                    </TableCell>
                    <TableCell className="tabular-nums text-muted-foreground">
                      {formatDateTime(snap.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title="暂无特征快照"
          description="特征快照由因子引擎批量计算生成，记录每个数据发布下的因子计算结果。"
        />
      )}
    </div>
  );
}

function SignalsTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-signals", { limit: 50 }],
    queryFn: () => factorLabApi.signals({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子信号` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载因子信号"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead>信号 ID</TableHead>
                  <TableHead>因子名</TableHead>
                  <TableHead>研究状态</TableHead>
                  <TableHead>快照 ID</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((sig: FactorSignal) => (
                  <TableRow key={sig.signal_id}>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {sig.signal_id}
                      </span>
                    </TableCell>
                    <TableCell className="font-mono text-sm">
                      {sig.factor_name}
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={sig.research_status} />
                    </TableCell>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {sig.snapshot_id}
                      </span>
                    </TableCell>
                    <TableCell className="tabular-nums text-muted-foreground">
                      {formatDateTime(sig.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Signal className="h-8 w-8" />}
          title="暂无因子信号"
          description="因子信号由因子计算产出，用于驱动策略目标仓位与组合决策。"
        />
      )}
    </div>
  );
}

function ExperimentsTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["factor-experiments", { limit: 50 }],
    queryFn: () => factorLabApi.listFactorExperiments({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个因子实验` : "加载中…"}
        </p>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
      </div>

      {isLoading ? (
        <LoadingState rows={6} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载因子实验"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead>实验 ID</TableHead>
                  <TableHead>假设</TableHead>
                  <TableHead>因子列表</TableHead>
                  <TableHead>数据发布 ID</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>创建时间</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.map((exp: FactorExperiment) => (
                  <TableRow key={exp.factor_experiment_id}>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {exp.factor_experiment_id}
                      </span>
                    </TableCell>
                    <TableCell className="max-w-xs truncate text-muted-foreground">
                      {exp.hypothesis}
                    </TableCell>
                    <TableCell>
                      <div className="flex flex-wrap gap-1">
                        {exp.factor_names.map((f) => (
                          <Badge key={f} variant="secondary" className="font-mono">
                            {f}
                          </Badge>
                        ))}
                      </div>
                    </TableCell>
                    <TableCell>
                      <span className="font-mono text-xs text-muted-foreground">
                        {exp.dataset_release_id}
                      </span>
                    </TableCell>
                    <TableCell>
                      <StatusBadge status={exp.status} />
                    </TableCell>
                    <TableCell className="tabular-nums text-muted-foreground">
                      {formatDateTime(exp.created_at)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<TestTube className="h-8 w-8" />}
          title="暂无因子实验"
          description="因子实验用于验证因子假设的预测力，并与机器验证实验关联。"
        />
      )}
    </div>
  );
}

export default function FactorLab() {
  return (
    <div>
      <PageHeader
        title="因子实验室"
        description="因子目录、特征快照、因子信号与因子实验"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "因子实验室" },
        ]}
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Atom className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">因子目录</p>
              <p className="text-sm font-medium">已注册因子定义</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Layers className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">特征快照</p>
              <p className="text-sm font-medium">批量计算产出</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Signal className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">因子信号</p>
              <p className="text-sm font-medium">驱动目标仓位</p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <TestTube className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">因子实验</p>
              <p className="text-sm font-medium">假设验证与稳健性</p>
            </div>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="catalog">
        <TabsList>
          <TabsTrigger value="catalog">因子目录</TabsTrigger>
          <TabsTrigger value="features">特征快照</TabsTrigger>
          <TabsTrigger value="signals">因子信号</TabsTrigger>
          <TabsTrigger value="experiments">因子实验</TabsTrigger>
        </TabsList>

        <TabsContent value="catalog">
          <CatalogTab />
        </TabsContent>
        <TabsContent value="features">
          <FeaturesTab />
        </TabsContent>
        <TabsContent value="signals">
          <SignalsTab />
        </TabsContent>
        <TabsContent value="experiments">
          <ExperimentsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
