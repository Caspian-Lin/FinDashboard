import { Fragment, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  Database,
  RefreshCw,
  Package,
  Search,
  ChevronRight,
  Layers,
  BarChart3,
  CalendarDays,
  Activity,
} from "lucide-react";
import { PageHeader } from "@/components/ui/page-header";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { StatusBadge } from "@/components/ui/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { ScrollArea } from "@/components/ui/scroll-area";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
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
import { datasetApi, type LifecycleEvent } from "@/lib/research";
import { cn, formatDateTime, formatNumber, formatPercent } from "@/lib/utils";

function coverageColor(pct: number): string {
  if (pct >= 95) return "bg-success";
  if (pct >= 80) return "bg-primary";
  if (pct >= 60) return "bg-warning";
  return "bg-destructive";
}

function ReleasesTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dataset-releases", { limit: 50 }],
    queryFn: () => datasetApi.releases({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 条发布记录` : "加载中…"}
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
          message={error instanceof Error ? error.message : "无法加载数据发布"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="rounded-lg border border-border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>数据集</TableHead>
                <TableHead>版本</TableHead>
                <TableHead>周期</TableHead>
                <TableHead>复权</TableHead>
                <TableHead className="text-right">标的数</TableHead>
                <TableHead className="text-right">覆盖率</TableHead>
                <TableHead>质量</TableHead>
                <TableHead>发布 ID</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {data.map((rel) => (
                <TableRow key={rel.release_id}>
                  <TableCell className="font-medium">{rel.dataset_name}</TableCell>
                  <TableCell>
                    <Badge variant="secondary">{rel.version}</Badge>
                  </TableCell>
                  <TableCell className="text-muted-foreground">{rel.period}</TableCell>
                  <TableCell className="text-muted-foreground">{rel.adjustment}</TableCell>
                  <TableCell className="tabular-nums text-right">
                    {formatNumber(rel.symbol_count, 0)}
                  </TableCell>
                  <TableCell className="tabular-nums text-right">
                    {formatPercent(rel.coverage_pct / 100, 1)}
                  </TableCell>
                  <TableCell>
                    <StatusBadge status={rel.quality_status} />
                  </TableCell>
                  <TableCell>
                    <span className="font-mono text-xs text-muted-foreground">
                      {rel.release_id}
                    </span>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      ) : (
        <EmptyState
          icon={<Database className="h-8 w-8" />}
          title="暂无数据发布"
          description="研究数据发布后将在此列出，包含版本、覆盖率与质量状态。"
        />
      )}
    </div>
  );
}

function ManifestsTab() {
  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["dataset-manifests", { limit: 50 }],
    queryFn: () => datasetApi.manifests({ limit: 50 }),
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {data ? `共 ${data.length} 个数据集` : "加载中…"}
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
          message={error instanceof Error ? error.message : "无法加载数据集清单"}
          onRetry={() => refetch()}
        />
      ) : data && data.length > 0 ? (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
          {data.map((m) => (
            <Card key={`${m.dataset_name}-${m.version}`}>
              <CardHeader className="pb-3">
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0">
                    <CardTitle className="truncate text-base">
                      {m.dataset_name}
                    </CardTitle>
                    <p className="mt-1 font-mono text-xs text-muted-foreground">
                      {m.checksum.slice(0, 12)}
                    </p>
                  </div>
                  <Badge variant="outline">{m.version}</Badge>
                </div>
              </CardHeader>
              <CardContent className="space-y-3">
                <div className="flex items-center justify-between">
                  <Badge variant="info" className="font-mono">
                    {m.source}
                  </Badge>
                  <StatusBadge status={m.quality_status} />
                </div>

                <div className="grid grid-cols-2 gap-2 text-sm">
                  <div>
                    <p className="text-xs text-muted-foreground">标的数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(m.symbol_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">行数</p>
                    <p className="tabular-nums font-medium">
                      {formatNumber(m.row_count, 0)}
                    </p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">数据缺口</p>
                    <p className="tabular-nums font-medium">{m.gaps}</p>
                  </div>
                  <div>
                    <p className="text-xs text-muted-foreground">覆盖率</p>
                    <p className="tabular-nums font-medium">
                      {formatPercent(m.coverage_pct / 100, 1)}
                    </p>
                  </div>
                </div>

                <div>
                  <Progress
                    value={m.coverage_pct}
                    indicatorClassName={coverageColor(m.coverage_pct)}
                  />
                </div>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={<Package className="h-8 w-8" />}
          title="暂无数据集清单"
          description="数据集清单记录了每个数据集的行数、标的数、覆盖率与质量。"
        />
      )}
    </div>
  );
}

function LifecyclePanel({ symbol }: { symbol: string }) {
  const { data, isLoading, isError } = useQuery({
    queryKey: ["instrument-lifecycle", symbol],
    queryFn: () => datasetApi.lifecycle(symbol, { limit: 50 }),
  });

  if (isLoading) {
    return <LoadingState rows={3} />;
  }
  if (isError) {
    return (
      <p className="px-4 py-3 text-sm text-destructive">生命周期事件加载失败</p>
    );
  }
  if (!data || data.length === 0) {
    return (
      <p className="px-4 py-3 text-sm text-muted-foreground">
        该标的无生命周期事件记录。
      </p>
    );
  }

  return (
    <div className="px-4 pb-3">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>事件类型</TableHead>
            <TableHead>日期</TableHead>
            <TableHead>描述</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {data.map((ev: LifecycleEvent) => (
            <TableRow key={ev.event_id}>
              <TableCell>
                <Badge variant="secondary">{ev.event_type}</Badge>
              </TableCell>
              <TableCell className="tabular-nums text-muted-foreground">
                {formatDateTime(ev.event_date)}
              </TableCell>
              <TableCell className="text-muted-foreground">
                {ev.description}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

function InstrumentsTab() {
  const [search, setSearch] = useState("");
  const [market, setMarket] = useState<string>("all");
  const [expandedRow, setExpandedRow] = useState<string | null>(null);

  const { data, isLoading, isError, error, refetch, isFetching } = useQuery({
    queryKey: ["instruments", { limit: 100 }],
    queryFn: () => datasetApi.instruments({ limit: 100 }),
  });

  const filtered = (data ?? []).filter((inst) => {
    const matchesSearch =
      search.trim() === "" ||
      inst.code.toLowerCase().includes(search.trim().toLowerCase()) ||
      inst.name.toLowerCase().includes(search.trim().toLowerCase());
    const matchesMarket = market === "all" || inst.market === market;
    return matchesSearch && matchesMarket;
  });

  const toggleRow = (code: string) => {
    setExpandedRow((prev) => (prev === code ? null : code));
  };

  return (
    <div>
      <div className="mb-3 flex flex-wrap items-center gap-3">
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="搜索代码或名称…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        <Select value={market} onValueChange={setMarket}>
          <SelectTrigger className="w-[140px]">
            <SelectValue placeholder="市场" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部市场</SelectItem>
            <SelectItem value="A股">A 股</SelectItem>
            <SelectItem value="期货">期货</SelectItem>
            <SelectItem value="基金">基金</SelectItem>
            <SelectItem value="债券">债券</SelectItem>
          </SelectContent>
        </Select>
        <Button
          variant="outline"
          size="sm"
          onClick={() => refetch()}
          disabled={isFetching}
        >
          <RefreshCw className={cn("h-4 w-4", isFetching && "animate-spin")} />
          刷新
        </Button>
        <span className="text-sm text-muted-foreground">
          {data ? `${filtered.length} / ${data.length} 只标的` : ""}
        </span>
      </div>

      {isLoading ? (
        <LoadingState rows={8} />
      ) : isError ? (
        <ErrorState
          message={error instanceof Error ? error.message : "无法加载标的元数据"}
          onRetry={() => refetch()}
        />
      ) : filtered.length > 0 ? (
        <div className="rounded-lg border border-border">
          <ScrollArea className="max-h-[600px]">
            <Table>
              <TableHeader className="sticky top-0 bg-card">
                <TableRow>
                  <TableHead className="w-8" />
                  <TableHead>代码</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>市场</TableHead>
                  <TableHead>类型</TableHead>
                  <TableHead>上市日期</TableHead>
                  <TableHead>标记</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {filtered.map((inst) => {
                  const isOpen = expandedRow === inst.code;
                  return (
                    <Fragment key={inst.code}>
                      <TableRow
                        onClick={() => toggleRow(inst.code)}
                        className={cn("cursor-pointer", isOpen && "bg-muted/50")}
                      >
                        <TableCell>
                          <ChevronRight
                            className={cn(
                              "h-4 w-4 text-muted-foreground transition-transform",
                              isOpen && "rotate-90",
                            )}
                          />
                        </TableCell>
                        <TableCell className="font-mono font-medium">
                          {inst.code}
                        </TableCell>
                        <TableCell>{inst.name}</TableCell>
                        <TableCell className="text-muted-foreground">
                          {inst.market}
                        </TableCell>
                        <TableCell className="text-muted-foreground">
                          {inst.instrument_type}
                        </TableCell>
                        <TableCell className="tabular-nums text-muted-foreground">
                          {inst.listed_date ?? "—"}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-1">
                            {inst.is_st && (
                              <Badge variant="warning">ST</Badge>
                            )}
                            {inst.is_suspended && (
                              <Badge variant="destructive">停牌</Badge>
                            )}
                            {!inst.is_st && !inst.is_suspended && (
                              <span className="text-xs text-muted-foreground">
                                正常
                              </span>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                      {isOpen && (
                        <TableRow key={`${inst.code}-detail`}>
                          <TableCell colSpan={7} className="bg-muted/30 p-0">
                            <LifecyclePanel symbol={inst.code} />
                          </TableCell>
                        </TableRow>
                      )}
                    </Fragment>
                  );
                })}
              </TableBody>
            </Table>
          </ScrollArea>
        </div>
      ) : (
        <EmptyState
          icon={<Layers className="h-8 w-8" />}
          title={search || market !== "all" ? "无匹配标的" : "暂无标的元数据"}
          description={
            search || market !== "all"
              ? "尝试调整搜索关键词或市场筛选条件。"
              : "标的元数据导入后将在此列出。"
          }
        />
      )}
    </div>
  );
}

export default function ResearchData() {
  return (
    <div>
      <PageHeader
        title="数据与标的"
        description="研究数据发布、数据集清单与标的元数据"
        breadcrumbs={[
          { label: "研究", href: "/research" },
          { label: "数据与标的" },
        ]}
      />

      <div className="mb-4 grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Database className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">数据发布</p>
              <p className="flex items-center gap-1 text-sm font-medium">
                <BarChart3 className="h-3.5 w-3.5 text-muted-foreground" />
                版本化发布记录
              </p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <Package className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">数据集清单</p>
              <p className="flex items-center gap-1 text-sm font-medium">
                <Layers className="h-3.5 w-3.5 text-muted-foreground" />
                覆盖率与质量监控
              </p>
            </div>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="flex items-center gap-3 p-4">
            <div className="flex h-10 w-10 items-center justify-center rounded-lg bg-primary/10 text-primary">
              <CalendarDays className="h-5 w-5" />
            </div>
            <div>
              <p className="text-xs text-muted-foreground">标的元数据</p>
              <p className="flex items-center gap-1 text-sm font-medium">
                <Activity className="h-3.5 w-3.5 text-muted-foreground" />
                代码 / 生命周期
              </p>
            </div>
          </CardContent>
        </Card>
      </div>

      <Tabs defaultValue="releases">
        <TabsList>
          <TabsTrigger value="releases">数据发布</TabsTrigger>
          <TabsTrigger value="manifests">数据集清单</TabsTrigger>
          <TabsTrigger value="instruments">标的元数据</TabsTrigger>
        </TabsList>

        <TabsContent value="releases">
          <ReleasesTab />
        </TabsContent>
        <TabsContent value="manifests">
          <ManifestsTab />
        </TabsContent>
        <TabsContent value="instruments">
          <InstrumentsTab />
        </TabsContent>
      </Tabs>
    </div>
  );
}
