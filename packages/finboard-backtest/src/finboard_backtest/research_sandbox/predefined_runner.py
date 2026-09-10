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
    industry_groups: dict[str, str | None] = field(default_factory=dict)

    @property
    def run_id(self) -> None:
        """进程内执行无 RCR(结果契约兼容 ``_record_from_result``)。"""
        return None


class _MountFactorInput(PredefinedFactorInput):
    """自窗口挂载 Arrow 表装配的因子输入(字段级惰性物化)。"""

    def __init__(
        self,
        *,
        definition: PredefinedFactorDefinition,
        bars_table: Any,
        daily_table: Any | None,
        mount_symbols: tuple[str, ...],
        decision_dates: tuple[date, ...],
        value_universe: tuple[str, ...],
        tradable_symbols: tuple[str, ...],
        benchmark_only_symbols: frozenset[str],
        industry_groups: Mapping[str, str | None] | None = None,
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
        self._value_universe = value_universe
        self._industry_groups: Mapping[str, str | None] = industry_groups or {}
        self._series_cache: dict[tuple[str, str], dict[str, SymbolSeries]] = {}

    # ---- 数据面(字段级惰性:因子不触碰的字段零物化,#378 精神) ----

    def bars(self, field: str = "close") -> dict[str, SymbolSeries]:
        return self._series_for("bars", field, self._bars_table)

    def daily_metrics(self, field: str) -> dict[str, SymbolSeries]:
        if self._daily_table is None:
            return {}
        return self._series_for("daily_metrics", field, self._daily_table)

    def industry_groups(self) -> dict[str, str | None]:
        """行业分组(#400:自 bars 主发布 instruments.industry 装配)。

        组标签 = 冻结发布 instruments 的 ``industry`` 字段(#185,#212
        research_industry_memberships 分组的冻结发布近似);benchmark-only
        标的与缺失行业的标的不在映射中(取值 None,因子侧可见、可计数)。
        """
        return dict(self._industry_groups)

    def sample(
        self,
        series_by_symbol: Any,
        per_symbol_values: Any,
    ) -> FactorSeriesFrame:
        """按目录条目决定采样面(cross_section → 可交易域,#380)。"""
        return sample_series_frame(
            series_by_symbol,
            per_symbol_values,
            decision_dates=self.decision_dates,
            value_universe=self._value_universe,
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


def _symbol_series_from_table(table: Any, field: str) -> dict[str, SymbolSeries]:
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
    dates = ordered.column("date").to_pylist()
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
    values = np.asarray(
        ordered.column(field).fill_null(float("nan")).to_pylist(),
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


async def run_predefined_factor_series(
    spec: FactorSeriesRunSpec,
    *,
    settings: Any | None = None,
    release_provider_factory: Any | None = None,
    workspace_root: Path | None = None,
    mount_override: WindowDataMount | None = None,
    benchmark_only_symbols: frozenset[str] | None = None,
    industry_groups: Mapping[str, str | None] | None = None,
) -> PredefinedFactorSeriesOutput:
    """进程内执行一次预置因子区间构建(与容器入口同构)。

    ``spec`` 复用 :class:`FactorSeriesRunSpec`(``code_artifact`` = 因子
    裸名,``code_commit`` = :func:`predefined_factor_commit` 锚 —— 不一致
    即 :data:`PREDEFINED_VERSION_MISMATCH` 秒拒,防过期锚混入内容寻址)。

    ``industry_groups``(#400)为 symbol → 行业标签映射;缺省时自 bars
    主发布 instruments 的 ``industry`` 字段装配(剔除 benchmark-only,
    #185/#212 冻结发布近似口径);``mount_override`` 路径无 provider 可
    装配,与 ``benchmark_only_symbols`` 同样要求显式传入(审计变体从基线
    产物继承,行业分组不随挂载重派生 —— 否则中性化类因子的截断变体会
    因分组缺失产生假阳性分歧)。
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
    industry: dict[str, str | None] | None = (
        dict(industry_groups) if industry_groups is not None else None
    )
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
        if industry is None:
            raise SandboxError(
                "mount_spec_mismatch",
                "mount_override 路径须显式传入 industry_groups"
                "(审计变体从基线产物继承,#400 行业分组不随挂载重派生)",
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
        if benchmark is None or industry is None:
            from finboard_backtest.strategy_spec.universe_precheck import (
                is_benchmark_only_instrument,
            )

            benchmark = frozenset(
                str(item.code)
                for item in providers[0].release.instruments
                if is_benchmark_only_instrument(item)
            )
            # 行业标签自冻结发布 instruments.industry(#185);getattr
            # 探针兼容无该字段的 provider/测试 stub(#304 先例),缺失
            # 视为 None → 因子侧缺组缺测可计数。
            industry = {
                str(item.code): getattr(item, "industry", None)
                for item in providers[0].release.instruments
                if not is_benchmark_only_instrument(item)
            }

    bars_table = _read_mount_table(mount.root / "bars.parquet")
    needs_daily = any(
        dep.startswith("daily_metrics.") for dep in definition.data_dependencies
    )
    daily_path = mount.root / "daily_metrics.parquet"
    daily_table = _read_mount_table(daily_path) if needs_daily and daily_path.exists() else None

    mount_series = _symbol_series_from_table(bars_table, "close")
    mount_symbols = tuple(sorted(mount_series))
    tradable = tuple(
        symbol for symbol in mount_symbols if symbol not in benchmark
    )
    value_universe = tradable if definition.cross_section else mount_symbols
    industry_map = industry or {}
    # 缺组计数只覆盖可交易域(基准标的刻意不在分组映射中,不算缺失)
    industry_missing = sum(1 for symbol in tradable if not industry_map.get(symbol))

    inp = _MountFactorInput(
        definition=definition,
        bars_table=bars_table,
        daily_table=daily_table,
        mount_symbols=mount_symbols,
        decision_dates=tuple(spec.dates),
        value_universe=value_universe,
        tradable_symbols=tradable,
        benchmark_only_symbols=benchmark,
        industry_groups=industry_map,
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
            "industry_groups_mapped": sum(
                1 for symbol in tradable if industry_map.get(symbol)
            ),
            "industry_groups_missing": industry_missing,
            "elapsed_seconds": elapsed,
            "release_checksums": release_checksums,
        },
        mount_manifest_checksum=mount.manifest_checksum,
        value_symbols=tuple(value_universe),
        workspace_dir=run_dir,
        mount=mount,
        benchmark_only_symbols=benchmark,
        industry_groups=industry_map,
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
    industry = getattr(baseline, "industry_groups", None)

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
            industry_groups=industry,
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
