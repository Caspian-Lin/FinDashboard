"""平台预置因子的进程内 factor_series 构建通道(issue #398)。

与 ``runner.run_factor_series_container`` 同构的区间执行入口,差异只在
执行载体:预置因子是**平台可信代码**(``factors.predefined`` 目录),
直接在 worker 进程内计算,跳过一次性沙箱容器(免容器税;用户代码的
容器隔离语义不变,#359 路径零改动)。

与用户因子通道同构、全部保留的部分:

* **同一窗口挂载** —— ``build_window_data_mount``(清单 v3,PIT 上界 =
  window_end 日终,窗口外数据 fail-closed)与 ``mount_override`` /
  ``filter_window_data_mount`` 审计变体路径;
* **同一前缀不变性审计** —— :func:`default_predefined_prefix_audit`
  以进程内 build_fn 复用 ``research_sandbox.audit`` 引擎(基线复用
  #371,截断变体从基线挂载 Arrow 过滤派生);
* **同一结果契约** —— 产出与 ``FactorSeriesOutput`` 同形的
  ``dates / values / quality``,由 ``factor_series_build`` 执行器的
  ``_record_from_result`` 统一装配为内容寻址 ``FactorSeriesRecord``。

实现要点:

* 逐标的序列自挂载 Arrow 表按 ``(symbol, available_at)`` 稳定排序构建
  (available_at 升序 = 因子输入契约;截断变体是子序列,稳定排序保序,
  前缀不变性因此结构性成立);
* 值域 universe 按目录条目 ``cross_section`` 决定:截面因子采样面收窄
  到可交易域(#380:截面分母不得混入 benchmark-only;基准集自 bars 主
  发布 instruments 经 ``is_benchmark_only_instrument`` 派生);时序因子
  保留全挂载标的(消费端统一剔除基准,#380 兜底 + explicit_symbols
  豁免照常生效);
* 质量门复用 ``check_series_quality``(阈值 ``research_sandbox_*``,
  归档不阻断,与用户因子编排同语义)。

公告类研究数据集(issue #402):``financial_indicators`` 之外,
``income_statements`` / ``balance_sheets`` / ``cashflow_statements`` /
``dividends`` 四个 kind 同构进挂载(文件名 = ``<kind>.parquet``),按目录
条目 ``data_dependencies`` 的 kind 前缀惰性读表;``research_dataset(kind,
field)`` 供给公告步进序列,``dividend_events()`` 供给分红事件史(除权
除息日对齐的精确股息率原料)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from datetime import time as dt_time
from pathlib import Path
from typing import Any

from finboard_backtest.factors.predefined.context import (
    DividendEventHistory,
    FactorSeriesFrame,
    PredefinedFactorInput,
    SymbolSeries,
    sample_series_frame,
)
from finboard_backtest.factors.predefined.registry import (
    PredefinedFactorDefinition,
    get_predefined_factor,
    predefined_factor_commit,
)
from finboard_backtest.research_sandbox.data_mount import (
    WindowDataMount,
    build_window_data_mount,
)
from finboard_backtest.research_sandbox.errors import SandboxError
from finboard_backtest.research_sandbox.factor_publish import (
    SeriesQualityReport,
    check_series_quality,
)
from finboard_backtest.research_sandbox.runner import (
    FactorSeriesRunSpec,
    _series_providers,
    _validate_mount_override,
    _validate_series_spec,
)

#: 预置因子实现版本与 spec 不一致的失败分类
PREDEFINED_VERSION_MISMATCH = "predefined_version_mismatch"

#: 未注册因子名的失败分类
UNKNOWN_PREDEFINED_FACTOR = "unknown_predefined_factor"

#: dict 推断 schema 下全 None 值列的 Arrow null 类型(pyarrow 无公共常量)
_NULL_TYPE: Any = None


def _null_type() -> Any:
    global _NULL_TYPE
    if _NULL_TYPE is None:
        import pyarrow as pa

        _NULL_TYPE = pa.null()
    return _NULL_TYPE


@dataclass(frozen=True)
class PredefinedFactorSeriesOutput:
    """进程内区间执行的产出(与 ``FactorSeriesOutput`` 消费面同形)。

    ``values`` 为 ``{date: {symbol: float | None}}``(非有限值已归一
    None);``quality`` 为逐日截面口径的质量门报告(归档不阻断);
    ``mount`` / ``workspace_dir`` / ``benchmark_only_symbols`` 供审计
    变体派生更紧上界的过滤挂载(#371 同机制,仅内存传递不落库)。
    """

    code_artifact: str
    code_commit: str
    release_id: str
    dataset_release_ids: tuple[str, ...]
    params: dict[str, Any]
    window_start: date
    window_end: date
    dates: tuple[date, ...]
    values: FactorSeriesFrame
    quality: SeriesQualityReport
    metrics: dict[str, Any]
    mount_manifest_checksum: str
    value_symbols: tuple[str, ...]
    workspace_dir: Path
    mount: WindowDataMount | None = None
    benchmark_only_symbols: frozenset[str] = field(default_factory=frozenset)

    @property
    def run_id(self) -> None:
        """进程内执行无 RCR(结果契约兼容 ``_record_from_result``)。"""
        return None


#: 公告类数据集 kind(挂载文件名 = <kind>.parquet,#402 与
#: ``data_mount.ANNOUNCED_DATASETS`` 同表;此处独立常量避免私有跨模块耦合)
_ANNOUNCED_MOUNT_KINDS: tuple[str, ...] = (
    "financial_indicators",
    "income_statements",
    "balance_sheets",
    "cashflow_statements",
    "dividends",
)


class _MountFactorInput(PredefinedFactorInput):
    """自窗口挂载 Arrow 表装配的因子输入(字段级惰性物化)。"""

    def __init__(
        self,
        *,
        definition: PredefinedFactorDefinition,
        bars_table: Any,
        daily_table: Any | None,
        dataset_tables: Mapping[str, Any],
        mount_symbols: tuple[str, ...],
        decision_dates: tuple[date, ...],
        value_universe: tuple[str, ...],
        tradable_symbols: tuple[str, ...],
        benchmark_only_symbols: frozenset[str],
    ) -> None:
        super().__init__(
            factor_name=definition.name,
            decision_dates=decision_dates,
            tradable_symbols=tradable_symbols,
            benchmark_only_symbols=benchmark_only_symbols,
        )
        self._definition = definition
        self._bars_table = bars_table
        self._daily_table = daily_table
        self._dataset_tables = dict(dataset_tables)
        self._value_universe = value_universe
        self._series_cache: dict[tuple[str, str], dict[str, SymbolSeries]] = {}
        self._events_cache: dict[str, Any] | None = None

    # ---- 数据面(字段级惰性:因子不触碰的字段零物化,#378 精神) ----

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return self._series_for("bars", field, self._bars_table)

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        if self._daily_table is None:
            return {}
        return self._series_for("daily_metrics", field, self._daily_table)

    def financial_indicators(self, field: str) -> dict[str, SymbolSeries]:
        return self.research_dataset("financial_indicators", field)

    def research_dataset(self, kind: str, field: str) -> dict[str, SymbolSeries]:
        """公告频率研究数据集的公告序列取数(#401/#402)。

        挂载行无 ``date`` 列(与 bars/daily 不同):行日期轴取
        ``announcement_date``,PIT 门控仍走逐行 ``available_at`` ——
        采样语义 = 「决策日可见的最近一次公告」的步进函数。值列全 None
        (上游该字段整体缺失)按 float64 归一,整列 NaN。未挂载对应
        数据集(联合发布未声明)→ 空映射。
        """
        table = self._dataset_tables.get(kind)
        if table is None:
            return {}
        if kind not in _ANNOUNCED_MOUNT_KINDS:
            raise SandboxError(
                "output_contract_violation",
                f"research_dataset 不支持的数据集 kind: {kind!r}"
                f"(bars/daily_metrics 请用专属取数口;公告类: "
                f"{list(_ANNOUNCED_MOUNT_KINDS)})",
            )
        cache_key = (kind, field)
        cached = self._series_cache.get(cache_key)
        if cached is not None:
            return cached
        if field not in table.column_names:
            raise SandboxError(
                "output_contract_violation",
                f"挂载 {kind} 缺少因子请求的字段: {field}"
                f"(可用: {table.column_names[:20]})",
            )
        series = _announced_series_from_table(table, field)
        self._series_cache[cache_key] = series
        return series

    def dividend_events(self) -> dict[str, DividendEventHistory]:
        """dividends 发布的分红事件史取数(#402;未挂载 → 空映射)。"""
        if self._events_cache is None:
            table = self._dataset_tables.get("dividends")
            self._events_cache = (
                _dividend_events_from_table(table) if table is not None else {}
            )
        return self._events_cache

    def industry_groups(self) -> dict[str, str | None]:
        # v1(#398):行业分组观测自研究发布的装配接线随基本面批次
        # (#401/#402)落地;当前返回空映射 —— cs_neutralize 在全缺组
        # 语义下退化为整体截面去均值(算子层支持显式传入分组序列)。
        return {}

    def sample(
        self,
        series_by_symbol: Any,
        per_symbol_values: Any,
    ) -> FactorSeriesFrame:
        """按目录条目决定采样面(cross_section → 可交易域,#380)。

        值域取目录 universe 与**本次因子实际产出序列**的交集(#401):
        bars/daily 因子对全挂载标的逐标的产出(交集 = 原 universe,零
        变化);财务公告序列只覆盖有公告的标的(无公告 = 结构性缺测,
        与停牌缺行同语义),不因个别标的缺公告拒采样。
        """
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=tuple(
                symbol
                for symbol in self._value_universe
                if symbol in per_symbol_values
            ),
        )

    # ---- 内部 ----------------------------------------------------------

    def _series_for(
        self, dataset: str, field: str, table: Any
    ) -> dict[str, SymbolSeries]:
        cache_key = (dataset, field)
        cached = self._series_cache.get(cache_key)
        if cached is not None:
            return cached
        if field not in table.column_names:
            raise SandboxError(
                "output_contract_violation",
                f"挂载 {dataset} 缺少因子请求的字段: {field}"
                f"(可用: {table.column_names[:20]})",
            )
        series = _symbol_series_from_table(table, field)
        self._series_cache[cache_key] = series
        return series


def _read_mount_table(path: Path) -> Any:
    """挂载 parquet → Arrow 表(mmap 文页支撑,与 kit 同口径)。"""
    import pyarrow.parquet as pq

    return pq.read_table(path, memory_map=True, pre_buffer=False)


def _symbol_series_from_table(
    table: Any, field: str, *, date_column: str = "date"
) -> dict[str, SymbolSeries]:
    """Arrow 长表 → {symbol: SymbolSeries}(按 (symbol, available_at) 稳定排序)。

    available_at 升序是因子输入契约;稳定排序使截断变体(行子序列)
    保序 —— 前缀不变性结构性成立。缺测数值行归一为 NaN。
    """
    import numpy as np
    import pyarrow.compute as pc

    indices = pc.sort_indices(
        table,
        sort_keys=[
            ("symbol", "ascending"),
            ("available_at", "ascending"),
        ],
    )
    ordered = table.take(indices)
    symbols = np.asarray(ordered.column("symbol").to_pylist(), dtype=object)
    dates = ordered.column(date_column).to_pylist()
    if "available_at" in ordered.column_names:
        available_micros = np.asarray(
            ordered.column("available_at").cast("int64").to_pylist(),
            dtype=np.int64,
        )
    else:
        # 无逐行 available_at(理论不可达:窗口挂载 v3 恒带该列)按
        # 业务日期日终回退(与 kit D1 回退语义一致)。
        available_micros = np.asarray(
            [
                int(
                    datetime.combine(
                        day, dt_time(23, 59, 59, 999999), tzinfo=UTC
                    ).timestamp()
                    * 1_000_000
                )
                for day in dates
            ],
            dtype=np.int64,
        )
    field_column = ordered.column(field)
    if field_column.type == _null_type():
        # 全 None 值列(上游该字段整体缺失)在 dict 推断 schema 下为 null
        # 类型,统一 float64 归一(与 #371 全 None 列口径一致)。
        field_column = field_column.cast("float64")
    values = np.asarray(
        field_column.fill_null(float("nan")).to_pylist(),
        dtype=np.float64,
    )
    boundaries = np.unique(symbols, return_index=True)
    series_by_symbol: dict[str, SymbolSeries] = {}
    for position, symbol in enumerate(boundaries[0].tolist()):
        start = int(boundaries[1][position])
        stop = (
            int(boundaries[1][position + 1])
            if position + 1 < len(boundaries[0])
            else symbols.size
        )
        series_by_symbol[str(symbol)] = SymbolSeries(
            dates=tuple(dates[start:stop]),
            values=values[start:stop],
            available_at=tuple(
                datetime.fromtimestamp(micros / 1_000_000, tz=UTC)
                for micros in available_micros[start:stop]
            ),
        )
    return series_by_symbol


def _announced_series_from_table(
    table: Any, field: str
) -> dict[str, SymbolSeries]:
    """公告类挂载表(financial/三表)→ {symbol: 公告序列}(#401/#402)。

    与 bars/daily 同一排序与 PIT 口径,差异只在行日期轴 = ``announcement_date``
    (公告频率步进序列,语义见 :meth:`PredefinedFactorInput.research_dataset`)。
    """
    return _symbol_series_from_table(
        table, field, date_column="announcement_date"
    )


def _dividend_events_from_table(table: Any) -> dict[str, DividendEventHistory]:
    """dividends 挂载表 → {symbol: 分红事件史}(#402)。

    与公告序列同一 ``(symbol, available_at)`` 稳定排序;逐行携带
    ``report_period`` / ``ex_date``(日期列原样,None 保持 None)与
    ``cash_div``(float64,缺测 NaN)。非数值列(div_proc 等)不进
    事件史 —— 因子聚合按「同 report_period 取最新可见行」消解进展
    口径(预案/股东大会/实施),见 registry 精确股息率因子。
    """
    import numpy as np
    import pyarrow.compute as pc

    indices = pc.sort_indices(
        table,
        sort_keys=[
            ("symbol", "ascending"),
            ("available_at", "ascending"),
        ],
    )
    ordered = table.take(indices)
    symbols = np.asarray(ordered.column("symbol").to_pylist(), dtype=object)
    announcement_dates = ordered.column("announcement_date").to_pylist()
    available_micros = np.asarray(
        ordered.column("available_at").cast("int64").to_pylist(),
        dtype=np.int64,
    )
    report_periods = ordered.column("report_period").to_pylist()
    ex_dates = ordered.column("ex_date").to_pylist()
    cash_div = ordered.column("cash_div")
    if cash_div.type == _null_type():
        cash_div = cash_div.cast("float64")
    cash_values = np.asarray(
        cash_div.fill_null(float("nan")).to_pylist(), dtype=np.float64
    )
    available_at = tuple(
        datetime.fromtimestamp(micros / 1_000_000, tz=UTC)
        for micros in available_micros
    )
    boundaries = np.unique(symbols, return_index=True)
    events_by_symbol: dict[str, DividendEventHistory] = {}
    for position, symbol in enumerate(boundaries[0].tolist()):
        start = int(boundaries[1][position])
        stop = (
            int(boundaries[1][position + 1])
            if position + 1 < len(boundaries[0])
            else symbols.size
        )
        events_by_symbol[str(symbol)] = DividendEventHistory(
            announcement_dates=tuple(announcement_dates[start:stop]),
            available_at=available_at[start:stop],
            report_periods=tuple(report_periods[start:stop]),
            ex_dates=tuple(ex_dates[start:stop]),
            cash_div=cash_values[start:stop],
        )
    return events_by_symbol


async def run_predefined_factor_series(
    spec: FactorSeriesRunSpec,
    *,
    settings: Any | None = None,
    release_provider_factory: Any | None = None,
    workspace_root: Path | None = None,
    mount_override: WindowDataMount | None = None,
    benchmark_only_symbols: frozenset[str] | None = None,
) -> PredefinedFactorSeriesOutput:
    """进程内执行一次预置因子区间构建(与容器入口同构)。

    ``spec`` 复用 :class:`FactorSeriesRunSpec`(``code_artifact`` = 因子
    裸名,``code_commit`` = :func:`predefined_factor_commit` 锚 —— 不一致
    即 :data:`PREDEFINED_VERSION_MISMATCH` 秒拒,防过期锚混入内容寻址)。
    """
    _validate_series_spec(spec)
    try:
        definition = get_predefined_factor(spec.code_artifact)
    except KeyError as exc:
        raise SandboxError(UNKNOWN_PREDEFINED_FACTOR, str(exc)) from exc
    expected_commit = predefined_factor_commit(spec.code_artifact)
    if spec.code_commit != expected_commit:
        raise SandboxError(
            PREDEFINED_VERSION_MISMATCH,
            f"预置因子 {spec.code_artifact} 的实现版本锚不一致: "
            f"spec={spec.code_commit!r} vs 目录={expected_commit!r}"
            "(因子实现已随版本演进,请用当前目录锚重新构建)",
        )
    if settings is None:
        settings = _default_series_settings()
    if settings is None:
        raise SandboxError(
            "sandbox_unavailable", "无法加载 settings,预置因子构建不可用"
        )

    started = time.monotonic()
    workspace = workspace_root or Path(settings.research_sandbox_workspace_root)
    run_dir = workspace / f"FSP-{uuid.uuid4().hex[:12]}"
    run_dir.mkdir(parents=True, exist_ok=True)

    benchmark = benchmark_only_symbols
    release_checksums: dict[str, str] = {}
    if mount_override is not None:
        _validate_mount_override(spec, mount_override)
        mount = mount_override
        if benchmark is None:
            raise SandboxError(
                "mount_spec_mismatch",
                "mount_override 路径须显式传入 benchmark_only_symbols"
                "(审计变体从基线产物继承,#380 基准集不随挂载重派生)",
            )
    else:
        providers, release_checksums = await _series_providers(
            spec, release_provider_factory, settings
        )
        mount = await build_window_data_mount(
            providers=providers,
            window_start=spec.window_start,
            window_end=spec.window_end,
            dates=spec.dates,
            out_root=run_dir / "data",
            code_artifact=spec.code_artifact,
            code_commit=spec.code_commit,
            release_id=spec.release_id,
            dataset_release_ids=spec.dataset_release_ids,
        )
        if benchmark is None:
            from finboard_backtest.strategy_spec.universe_precheck import (
                is_benchmark_only_instrument,
            )

            benchmark = frozenset(
                str(item.code)
                for item in providers[0].release.instruments
                if is_benchmark_only_instrument(item)
            )

    bars_table = _read_mount_table(mount.root / "bars.parquet")
    needs_daily = any(
        dep.startswith("daily_metrics.") for dep in definition.data_dependencies
    )
    daily_path = mount.root / "daily_metrics.parquet"
    daily_table = _read_mount_table(daily_path) if needs_daily and daily_path.exists() else None
    # issue #401/#402:声明公告类数据集依赖的因子自对应发布挂载装配公告
    # 序列 / 分红事件史;未挂载(联合发布未声明该 kind)→ 空表缺省,因子
    # 取数口返回空映射(与财务因子「未挂载 → 空映射」同语义)。
    dataset_tables: dict[str, Any] = {}
    needed_kinds = {
        dep.split(".", 1)[0]
        for dep in definition.data_dependencies
        if dep.split(".", 1)[0] in _ANNOUNCED_MOUNT_KINDS
    }
    for dataset_kind in needed_kinds:
        table_path = mount.root / f"{dataset_kind}.parquet"
        if table_path.exists():
            dataset_tables[dataset_kind] = _read_mount_table(table_path)

    mount_series = _symbol_series_from_table(bars_table, "close")
    mount_symbols = tuple(sorted(mount_series))
    tradable = tuple(
        symbol for symbol in mount_symbols if symbol not in benchmark
    )
    value_universe = tradable if definition.cross_section else mount_symbols

    inp = _MountFactorInput(
        definition=definition,
        bars_table=bars_table,
        daily_table=daily_table,
        dataset_tables=dataset_tables,
        mount_symbols=mount_symbols,
        decision_dates=tuple(spec.dates),
        value_universe=value_universe,
        tradable_symbols=tradable,
        benchmark_only_symbols=benchmark,
    )
    frame = definition.compute(inp)
    _validate_frame(frame, spec)

    quality = check_series_quality(
        frame,
        dates=spec.dates,
        universe=list(value_universe),
        max_nan_ratio=settings.research_sandbox_max_nan_ratio,
        min_coverage=settings.research_sandbox_min_coverage,
    )
    elapsed = round(time.monotonic() - started, 3)
    return PredefinedFactorSeriesOutput(
        code_artifact=spec.code_artifact,
        code_commit=spec.code_commit,
        release_id=spec.release_id,
        dataset_release_ids=tuple(spec.dataset_release_ids),
        params=dict(spec.params),
        window_start=spec.window_start,
        window_end=spec.window_end,
        dates=tuple(spec.dates),
        values=frame,
        quality=quality,
        metrics={
            "mode": "predefined_inprocess",
            "factor": spec.code_artifact,
            "implementation_commit": spec.code_commit,
            "cross_section": definition.cross_section,
            "data_dependencies": list(definition.data_dependencies),
            "value_symbols": len(value_universe),
            "mount_symbols": len(mount_symbols),
            "benchmark_only_excluded": definition.cross_section,
            "elapsed_seconds": elapsed,
            "release_checksums": release_checksums,
        },
        mount_manifest_checksum=mount.manifest_checksum,
        value_symbols=tuple(value_universe),
        workspace_dir=run_dir,
        mount=mount,
        benchmark_only_symbols=benchmark,
    )


def _validate_frame(frame: FactorSeriesFrame, spec: FactorSeriesRunSpec) -> None:
    """compute 产出契约校验:决策日逐日齐备(缺测标的允许,缺日拒绝)。"""
    missing = [day for day in spec.dates if day not in frame]
    if missing:
        raise SandboxError(
            "output_contract_violation",
            f"预置因子 {spec.code_artifact} 产出缺少决策日截面: "
            f"{[day.isoformat() for day in missing[:3]]}",
        )


def _default_series_settings() -> Any | None:
    from finboard_backtest.background_jobs.executors._providers import (
        default_settings_factory,
    )

    return default_settings_factory()


async def default_predefined_prefix_audit(
    spec: Any, baseline: Any, *, truncate_at: date
) -> Any:
    """预置因子的前缀不变性审计(#371 同机制,build_fn 进程内)。

    基线 = 主构建产物(:class:`PredefinedFactorSeriesOutput`);截断变体
    从基线挂载经 ``filter_window_data_mount`` Arrow 过滤派生(物理上只
    可见 cut 日终之前的数据),以 ``mount_override`` 进程内重算 —— 检出
    语义与用户因子通道逐字一致(首个分歧日期 + 该日因子值对照)。
    """
    from finboard_backtest.research_sandbox.audit import (
        run_prefix_invariance_audit,
    )
    from finboard_backtest.research_sandbox.data_mount import (
        filter_window_data_mount,
    )

    baseline_mount = getattr(baseline, "mount", None)
    workspace_dir = getattr(baseline, "workspace_dir", None)
    benchmark = getattr(baseline, "benchmark_only_symbols", None)

    async def build_fn(
        dates: Sequence[date], perturb_from: date | None = None
    ) -> Any:
        del perturb_from  # 截断模式忽略第二参(#359 引擎语义)
        window_end = max(dates)
        variant_spec = replace(spec, dates=tuple(dates), window_end=window_end)
        if baseline_mount is None or workspace_dir is None:
            return await run_predefined_factor_series(variant_spec)
        variant_mount = await filter_window_data_mount(
            baseline_mount,
            new_window_end=window_end,
            dates=dates,
            out_root=Path(workspace_dir) / f"data-cut-{window_end.isoformat()}",
        )
        return await run_predefined_factor_series(
            variant_spec,
            mount_override=variant_mount,
            benchmark_only_symbols=benchmark,
            workspace_root=Path(workspace_dir),
        )

    return await run_prefix_invariance_audit(
        build_fn,
        mode="truncation",
        cut_points=[truncate_at],
        dates=list(spec.dates),
        baseline=baseline,
    )


__all__ = [
    "PREDEFINED_VERSION_MISMATCH",
    "UNKNOWN_PREDEFINED_FACTOR",
    "PredefinedFactorSeriesOutput",
    "default_predefined_prefix_audit",
    "run_predefined_factor_series",
]
