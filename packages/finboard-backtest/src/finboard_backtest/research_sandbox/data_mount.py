"""沙箱数据面 —— 冻结发布 → 只读挂载目录(issue #216;#359 增窗口挂载 v3)。

在**服务端**把 ``dataset_release_ids`` 指向的冻结发布按 ``decision_at``
物化成一个挂载目录:逐标的走 ``FrozenReleaseProvider`` 的 PIT 门控接口
(``available_at <= decision_at``),行级转 float 后写 parquet,并落
``mount_manifest.json`` 清单。

**PIT 由物理隔离保证**:容器内不存在未来数据文件 —— 挂载内容本身即
decision_at 之前的数据。本模块在 provider 门控之上再做一层防线:任何
数据日期晚于 decision_at 当日的行直接 fail-closed(视为上游/provider
缺陷,拒绝生成挂载),杜绝「门控被绕过 → 未来数据进容器」的整类事故。

**窗口挂载 v3(issue #359,``build_window_data_mount``)**:区间因子执行
(``factor.compute_series``)要求容器可见**整个窗口**的数据(否则算不了
后段日期),挂载 fail-closed 上界从 decision_at 改为 ``window_end`` 日终
—— 最大泄漏不超过窗口末端,不存在看到窗口之外未来的可能;窗口内逐日
PIT 不再由挂载物理保证,由 kit ``bars_view`` / ``dataset_view`` 访问器
契约 + 服务端前缀不变性审计检出。v3 挂载的长表额外携带逐行
``available_at`` 列(访问器过滤的依据),清单 ``version=3`` 并携带
window / 代码与发布溯源字段(canonical ``factor_series.json`` 需要回填)。
v2 单日/策略挂载(``build_data_mount``)原样不动。

输出目录结构(挂到容器 ``/data:ro``)::

    <root>/bars.parquet                    # 全部 bars 类发布合并的长表
    <root>/daily_metrics.parquet           # daily_metrics 类发布合并
    <root>/financial_indicators.parquet    # 公告类发布合并(#402 起含
    <root>/income_statements.parquet       #  三表与 dividend 五个 kind,
    <root>/balance_sheets.parquet          #  行结构同构:公告日轴 +
    <root>/cashflow_statements.parquet     #  逐行 available_at(v3))
    <root>/dividends.parquet
    <root>/mount_manifest.json             # 挂载清单(PIT 审计锚点)

列契约与 :mod:`finboard_research_kit.context` 的 docstring 一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_VERSION = 2
#: 窗口挂载清单版本(issue #359,``build_window_data_mount`` 专用)
WINDOW_MANIFEST_VERSION = 3
_MOUNT_MANIFEST = "mount_manifest.json"


class SandboxMountError(Exception):
    """挂载构建失败(PIT 违规 / 发布不可用;消息面向排查)。"""


@dataclass(frozen=True)
class MountDataset:
    """清单里一个发布对挂载的贡献。"""

    release_id: str
    dataset_kind: str
    file: str
    row_count: int
    max_data_date: date | None


@dataclass(frozen=True)
class DataMount:
    """一次沙箱运行的数据挂载(目录 + 清单 + checksum)。"""

    root: Path
    decision_at: datetime
    symbols: tuple[str, ...]
    datasets: tuple[MountDataset, ...]
    manifest_checksum: str
    # issue #218:策略协议(strategy.decide)的挂载清单 v2 增量 ——
    # 引擎回显的当前组合权重与约束只读视图;factor 协议挂载为空映射。
    current_weights: Mapping[str, float] = field(default_factory=dict)
    strategy_constraints: Mapping[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.root / _MOUNT_MANIFEST

    def manifest_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "decision_at": self.decision_at.isoformat(),
            "symbols": list(self.symbols),
            "datasets": [
                {
                    "release_id": d.release_id,
                    "dataset_kind": d.dataset_kind,
                    "file": d.file,
                    "row_count": d.row_count,
                    "max_data_date": d.max_data_date.isoformat()
                    if d.max_data_date
                    else None,
                }
                for d in self.datasets
            ],
        }
        if self.current_weights:
            payload["current_weights"] = dict(self.current_weights)
        if self.strategy_constraints:
            payload["strategy_constraints"] = dict(self.strategy_constraints)
        return payload


class _MountProvider(Protocol):
    """FrozenReleaseProvider 的最小结构(测试可替换)。"""

    @property
    def release(self) -> Any: ...


async def build_data_mount(
    *,
    providers: Iterable[Any],
    decision_at: datetime,
    out_root: Path,
    symbols: Iterable[str] | None = None,
    current_weights: Mapping[str, float] | None = None,
    strategy_constraints: Mapping[str, Any] | None = None,
) -> DataMount:
    """把若干冻结发布物化为一个只读挂载目录。

    ``providers`` 为按 ``dataset_release_ids`` 构造的
    :class:`~finboard_data.releases.FrozenReleaseProvider`;``symbols``
    缺省 = bars 类发布的全部标的(多发布取并集)。

    issue #218:策略协议增 ``current_weights``(引擎回显的当前组合权重,
    上一决策成交后实际持仓市值占比)/ ``strategy_constraints``(组合约束
    只读视图)—— 落入挂载清单 v2,容器内 decide 可见;两者不影响 PIT
    防线(不是按日期门控的数据行)。
    """
    if decision_at.tzinfo is None:
        raise SandboxMountError("decision_at 必须带时区")
    decision_day = decision_at.date()
    wanted = frozenset(symbols) if symbols is not None else None

    await asyncio.to_thread(out_root.mkdir, parents=True, exist_ok=True)
    # 流式写入器(#371):与窗口挂载同构,bars / daily_metrics 逐标的落盘;
    # 公告类数据集(三表/dividend/#402)公告量小,保留整集累积旧路径。
    bars_writer = _DatasetStreamWriter(out_root / "bars.parquet")
    daily_writer = _DatasetStreamWriter(out_root / "daily_metrics.parquet")
    announced_rows: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ANNOUNCED_DATASETS}
    contributions: list[MountDataset] = []
    universe: set[str] = set()

    for provider in providers:
        release = provider.release
        kind = release.dataset_kind
        release_id = release.release_id
        instruments = [
            item for item in release.instruments
            if wanted is None or item.code in wanted
        ]
        if wanted is not None:
            missing = wanted - {i.code for i in instruments}
            if missing and kind.value == "bars":
                raise SandboxMountError(
                    f"发布 {release_id} 缺少请求的标的: {sorted(missing)[:10]}"
                )
        if kind.value == "bars":
            bars_schema = _bars_schema(include_available_at=False)
            start_rows = bars_writer.rows
            provider_max: date | None = None
            batches = await _fetch_bars_batches(
                provider, instruments, decision_at, include_available_at=False
            )
            for rows in batches:
                if not rows:
                    continue
                universe.update(r["symbol"] for r in rows)
                table = pa.Table.from_pylist(rows, schema=bars_schema)
                _guard_pit_table(table, "date", release_id, decision_day)
                bars_writer.write(table)
                provider_max = _max_date_optional(provider_max, rows, "date")
            contributions.append(
                MountDataset(
                    release_id=release_id,
                    dataset_kind=kind.value,
                    file="bars.parquet",
                    row_count=bars_writer.rows - start_rows,
                    max_data_date=provider_max,
                )
            )
        elif kind.value == "daily_metrics":
            daily_schema: pa.Schema | None = None
            start_rows = daily_writer.rows
            provider_max_daily: date | None = None
            batches = await _fetch_daily_rows_batches(
                provider, instruments, decision_at, include_available_at=False
            )
            for rows in batches:
                if not rows:
                    continue
                if daily_schema is None:
                    daily_schema = _daily_metrics_schema(
                        list(rows[0]), include_available_at=False
                    )
                table = pa.Table.from_pylist(rows, schema=daily_schema)
                _guard_pit_table(table, "trade_date", release_id, decision_day)
                daily_writer.write(table)
                provider_max_daily = _max_date_optional(
                    provider_max_daily, rows, "trade_date"
                )
            contributions.append(
                MountDataset(
                    release_id=release_id,
                    dataset_kind=kind.value,
                    file="daily_metrics.parquet",
                    row_count=daily_writer.rows - start_rows,
                    max_data_date=provider_max_daily,
                )
            )
        elif kind.value in ANNOUNCED_DATASETS:
            rows = await _collect_announced(
                provider,
                instruments,
                decision_at,
                fetch_attr=ANNOUNCED_DATASETS[kind.value],
                include_available_at=False,
            )
            _guard_pit(rows, "announcement_date", release_id, decision_day)
            announced_rows[kind.value].extend(rows)
            contributions.append(
                MountDataset(
                    release_id=release_id,
                    dataset_kind=kind.value,
                    file=f"{kind.value}.parquet",
                    row_count=len(rows),
                    max_data_date=_max_date(rows, "announcement_date"),
                )
            )
        else:
            raise SandboxMountError(
                f"发布 {release_id} 的 dataset_kind={kind.value!r} 不支持挂载"
            )

    if bars_writer.rows == 0:
        raise SandboxMountError(
            "挂载不含任何行情行:dataset_release_ids 须至少包含一个含目标"
            "标的的 bars 类发布"
        )
    bars_writer.finish()
    daily_writer.finish()
    for announced_kind, rows in announced_rows.items():
        await asyncio.to_thread(
            _write_parquet,
            out_root / f"{announced_kind}.parquet",
            rows,
        )

    ordered = tuple(sorted(universe))
    frozen_weights: Mapping[str, float] = dict(current_weights or {})
    frozen_constraints: Mapping[str, Any] = dict(strategy_constraints or {})
    mount = DataMount(
        root=out_root,
        decision_at=decision_at,
        symbols=ordered,
        datasets=tuple(contributions),
        manifest_checksum="",
        current_weights=frozen_weights,
        strategy_constraints=frozen_constraints,
    )
    payload = json.dumps(
        {**mount.manifest_dict(), "generated_at": datetime.now(UTC).isoformat()},
        ensure_ascii=False,
        indent=2,
    )
    await asyncio.to_thread(
        mount.manifest_path.write_text, payload, encoding="utf-8"
    )
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return DataMount(
        root=out_root,
        decision_at=decision_at,
        symbols=ordered,
        datasets=tuple(contributions),
        manifest_checksum=checksum,
        current_weights=frozen_weights,
        strategy_constraints=frozen_constraints,
    )


@dataclass(frozen=True)
class WindowDataMount:
    """一次区间因子执行的窗口挂载(issue #359,清单 v3)。

    与 :class:`DataMount` 的差异:数据面覆盖整个窗口
    (``available_at <= window_end`` 日终),bars 长表额外携带逐行
    ``available_at`` 列;清单携带窗口决策日序列与代码/发布溯源字段
    (容器内 canonical ``factor_series.json`` 回填需要)。窗口内逐日 PIT
    由 kit 访问器契约 + 前缀不变性审计承担,不再由挂载物理保证。
    """

    root: Path
    window_start: date
    window_end: date
    dates: tuple[date, ...]
    symbols: tuple[str, ...]
    datasets: tuple[MountDataset, ...]
    manifest_checksum: str
    code_artifact: str
    code_commit: str
    #: 挂载锚定的 bars 主发布(canonical factor_series.json 的 release_id)
    release_id: str
    dataset_release_ids: tuple[str, ...]

    @property
    def manifest_path(self) -> Path:
        return self.root / _MOUNT_MANIFEST

    def manifest_dict(self) -> dict[str, Any]:
        return {
            "version": WINDOW_MANIFEST_VERSION,
            "window": {
                "window_start": self.window_start.isoformat(),
                "window_end": self.window_end.isoformat(),
                "dates": [d.isoformat() for d in self.dates],
            },
            "symbols": list(self.symbols),
            "code_artifact": self.code_artifact,
            "code_commit": self.code_commit,
            "release_id": self.release_id,
            "dataset_release_ids": list(self.dataset_release_ids),
            "datasets": [
                {
                    "release_id": d.release_id,
                    "dataset_kind": d.dataset_kind,
                    "file": d.file,
                    "row_count": d.row_count,
                    "max_data_date": d.max_data_date.isoformat()
                    if d.max_data_date
                    else None,
                }
                for d in self.datasets
            ],
        }


def _end_of_window(window_end: date) -> datetime:
    """窗口挂载的 PIT 上界:window_end 日终(UTC 23:59:59.999999)。"""
    return datetime.combine(window_end, time(23, 59, 59, 999999), tzinfo=UTC)


#: 数据集 kind → 行级数据日期列(过滤挂载重跑窗口防线用,#371)
_DATASET_DATE_FIELD: dict[str, str] = {
    "bars": "date",
    "daily_metrics": "trade_date",
    "financial_indicators": "announcement_date",
    # issue #402:三表 + dividend 公告类数据集(行日期轴同为公告日)
    "income_statements": "announcement_date",
    "balance_sheets": "announcement_date",
    "cashflow_statements": "announcement_date",
    "dividends": "announcement_date",
}

#: 公告频率研究数据集 kind → (provider 取数方法名, 挂载文件名)(#402)。
#: 与 financial_indicators 同构:公告量小,保留整集累积旧路径;挂载文件
#: 名 = kind + .parquet,预置因子通道按同名读回(``predefined_runner``)。
ANNOUNCED_DATASETS: dict[str, str] = {
    "financial_indicators": "fetch_financial_indicators",
    "income_statements": "fetch_income_statements",
    "balance_sheets": "fetch_balance_sheets",
    "cashflow_statements": "fetch_cashflow_statements",
    "dividends": "fetch_dividends",
}


async def filter_window_data_mount(
    baseline: WindowDataMount,
    *,
    new_window_end: date,
    dates: Sequence[date],
    out_root: Path,
) -> WindowDataMount:
    """从基线窗口挂载派生**更紧上界**的变体挂载(issue #371,审计重放)。

    数学等值性:逐行 ``available_at`` 固定且 PIT 门控单调,「在 cut 现场
    物化」(provider 走读 + available_at <= cut 日终门控)所得行集恒等于
    基线行集 ∩ available_at <= cut 日终 —— 本函数对基线各数据集 parquet
    做 Arrow 过滤(保序)后重跑窗口防线(数据日期 <= new_window_end),
    免掉一次全市场 provider 对象走读。

    清单 version=3:窗口字段换新(``window_end`` / ``dates``),代码与
    发布溯源沿用基线;``symbols`` 为过滤后 bars 行集的标的并集(与现场
    物化的 universe 同口径);checksum 与现场物化同法计算。

    前置(违反即 :class:`SandboxMountError`):基线上界不得收紧到窗口起点
    之前;``dates`` 非空、升序、且全部落在 ``[window_start, new_window_end]``。
    """
    if new_window_end > baseline.window_end or new_window_end < baseline.window_start:
        raise SandboxMountError(
            f"过滤挂载的新 window_end {new_window_end} 须落在基线窗口 "
            f"[{baseline.window_start}, {baseline.window_end}] 内"
        )
    ordered_dates = tuple(dates)
    if not ordered_dates:
        raise SandboxMountError("过滤挂载要求非空 dates(截断后决策日序列)")
    if list(ordered_dates) != sorted(ordered_dates):
        raise SandboxMountError("dates 须按升序排列")
    outside = [
        d for d in ordered_dates if d < baseline.window_start or d > new_window_end
    ]
    if outside:
        raise SandboxMountError(
            f"dates 含新窗口外决策日: {outside[:3]}(window "
            f"[{baseline.window_start}, {new_window_end}])"
        )
    ceiling = _end_of_window(new_window_end)

    import pyarrow.compute as pc

    await asyncio.to_thread(out_root.mkdir, parents=True, exist_ok=True)
    contributions: list[MountDataset] = []
    universe: set[str] = set()
    for dataset in baseline.datasets:
        source = baseline.root / dataset.file
        if not source.exists():
            continue
        table = await asyncio.to_thread(pq.read_table, source)
        if "available_at" not in table.column_names:
            raise SandboxMountError(
                f"基线挂载 {dataset.file} 缺少 available_at 列,无法过滤"
                "(窗口挂载 v3 各数据集恒携带该列)"
            )
        keep = pc.invert(
            pc.fill_null(pc.greater(table.column("available_at"), ceiling), False)
        )
        filtered = table.filter(keep)
        date_field = _DATASET_DATE_FIELD.get(dataset.dataset_kind)
        if date_field is not None and filtered.num_rows:
            _guard_window_pit_table(
                filtered, date_field, dataset.release_id, new_window_end
            )
        if dataset.dataset_kind == "bars" and filtered.num_rows:
            universe.update(filtered.column("symbol").unique().to_pylist())
        max_day: date | None = None
        if date_field is not None and filtered.num_rows:
            max_day = pc.max(filtered.column(date_field)).as_py()
        await asyncio.to_thread(
            _write_table_parquet, out_root / dataset.file, filtered
        )
        contributions.append(
            MountDataset(
                release_id=dataset.release_id,
                dataset_kind=dataset.dataset_kind,
                file=dataset.file,
                row_count=filtered.num_rows,
                max_data_date=max_day,
            )
        )

    mount = WindowDataMount(
        root=out_root,
        window_start=baseline.window_start,
        window_end=new_window_end,
        dates=ordered_dates,
        symbols=tuple(sorted(universe)),
        datasets=tuple(contributions),
        manifest_checksum="",
        code_artifact=baseline.code_artifact,
        code_commit=baseline.code_commit,
        release_id=baseline.release_id,
        dataset_release_ids=baseline.dataset_release_ids,
    )
    payload = json.dumps(
        {**mount.manifest_dict(), "generated_at": datetime.now(UTC).isoformat()},
        ensure_ascii=False,
        indent=2,
    )
    await asyncio.to_thread(
        mount.manifest_path.write_text, payload, encoding="utf-8"
    )
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return replace(mount, manifest_checksum=checksum)


def _write_table_parquet(path: Path, table: pa.Table) -> None:
    """Arrow 表直接落盘(空表 = 确保文件不存在;同步,调用方走 to_thread)。"""
    if table.num_rows == 0:
        path.unlink(missing_ok=True)
        return
    pq.write_table(table, path, compression="zstd")


async def build_window_data_mount(
    *,
    providers: Iterable[Any],
    window_start: date,
    window_end: date,
    dates: Sequence[date],
    out_root: Path,
    code_artifact: str,
    code_commit: str,
    release_id: str,
    dataset_release_ids: Sequence[str],
    symbols: Iterable[str] | None = None,
) -> WindowDataMount:
    """把冻结发布物化为**窗口**挂载(清单 v3,issue #359)。

    与 :func:`build_data_mount` 的差异:

    * PIT 上界 = ``window_end`` 日终(provider 门控与 fail-closed 防线同
      上界):窗口之后的数据进不了容器,窗口之内**全量**可见 —— 逐日
      PIT 由协议契约(``value[t]`` 只许依赖 ``available_at <= t`` 的数据)
      与前缀不变性审计承担;
    * bars 长表携带逐行 ``available_at``(访问器过滤依据);
    * 清单 version=3,携带 window 决策日序列与代码/发布溯源
      (canonical ``factor_series.json`` 回填)。

    fail-closed:任何数据日期晚于 ``window_end`` 当日的行**具名拒绝**
    (``窗口外数据``),杜绝 provider 门控缺陷把窗口之后的未来泄进容器。
    """
    ordered_dates = tuple(dates)
    if not ordered_dates:
        raise SandboxMountError("窗口挂载要求非空 dates(窗口内决策日序列)")
    if window_start > window_end:
        raise SandboxMountError(
            f"window_start {window_start} 晚于 window_end {window_end}"
        )
    if list(ordered_dates) != sorted(ordered_dates):
        raise SandboxMountError("dates 须按升序排列")
    outside = [
        d
        for d in ordered_dates
        if d < window_start or d > window_end
    ]
    if outside:
        raise SandboxMountError(
            f"dates 含窗口外决策日: {outside[:3]}(window "
            f"[{window_start}, {window_end}])"
        )
    ceiling = _end_of_window(window_end)
    wanted = frozenset(symbols) if symbols is not None else None

    await asyncio.to_thread(out_root.mkdir, parents=True, exist_ok=True)
    # 流式写入器(#371):bars / daily_metrics 逐标的批次落盘,内存只持
    # row group 缓冲;公告类数据集(三表/dividend/#402)公告量小,保留
    # 整集累积旧路径。
    bars_writer = _DatasetStreamWriter(out_root / "bars.parquet")
    daily_writer = _DatasetStreamWriter(out_root / "daily_metrics.parquet")
    announced_rows: dict[str, list[dict[str, Any]]] = {kind: [] for kind in ANNOUNCED_DATASETS}
    contributions: list[MountDataset] = []
    universe: set[str] = set()

    for provider in providers:
        release = provider.release
        kind = release.dataset_kind
        rel_id = release.release_id
        instruments = [
            item for item in release.instruments
            if wanted is None or item.code in wanted
        ]
        if wanted is not None:
            missing = wanted - {i.code for i in instruments}
            if missing and kind.value == "bars":
                raise SandboxMountError(
                    f"发布 {rel_id} 缺少请求的标的: {sorted(missing)[:10]}"
                )
        if kind.value == "bars":
            bars_schema = _bars_schema(include_available_at=True)
            start_rows = bars_writer.rows
            provider_max: date | None = None
            batches = await _fetch_bars_batches(
                provider, instruments, ceiling, include_available_at=True
            )
            for rows in batches:
                if not rows:
                    continue
                universe.update(r["symbol"] for r in rows)
                table = pa.Table.from_pylist(rows, schema=bars_schema)
                _guard_window_pit_table(table, "date", rel_id, window_end)
                bars_writer.write(table)
                provider_max = _max_date_optional(provider_max, rows, "date")
            contributions.append(
                MountDataset(
                    release_id=rel_id,
                    dataset_kind=kind.value,
                    file="bars.parquet",
                    row_count=bars_writer.rows - start_rows,
                    max_data_date=provider_max,
                )
            )
        elif kind.value == "daily_metrics":
            daily_schema: pa.Schema | None = None
            start_rows = daily_writer.rows
            provider_max_daily: date | None = None
            batches = await _fetch_daily_mixed_batches(
                provider, instruments, ceiling
            )
            for batch in batches:
                if isinstance(batch, pa.Table):
                    # 列式快路径(#371):provider 支持列式读取时逐标的
                    # Arrow 直通,免整行对象税。
                    if batch.num_rows == 0:
                        continue
                    if daily_schema is None:
                        daily_schema = batch.schema
                    table = batch
                else:
                    # 测试 stub 等无列式方法时回退对象路径(逐值等值)。
                    rows = batch
                    if not rows:
                        continue
                    if daily_schema is None:
                        daily_schema = _daily_metrics_schema(
                            list(rows[0]), include_available_at=True
                        )
                    table = pa.Table.from_pylist(rows, schema=daily_schema)
                _guard_window_pit_table(table, "trade_date", rel_id, window_end)
                daily_writer.write(table)
                provider_max_daily = _max_date_optional_table(
                    provider_max_daily, table, "trade_date"
                )
            contributions.append(
                MountDataset(
                    release_id=rel_id,
                    dataset_kind=kind.value,
                    file="daily_metrics.parquet",
                    row_count=daily_writer.rows - start_rows,
                    max_data_date=provider_max_daily,
                )
            )
        elif kind.value in ANNOUNCED_DATASETS:
            rows = await _collect_announced(
                provider,
                instruments,
                ceiling,
                fetch_attr=ANNOUNCED_DATASETS[kind.value],
                include_available_at=True,
            )
            _guard_window_pit(rows, "announcement_date", rel_id, window_end)
            announced_rows[kind.value].extend(rows)
            contributions.append(
                MountDataset(
                    release_id=rel_id,
                    dataset_kind=kind.value,
                    file=f"{kind.value}.parquet",
                    row_count=len(rows),
                    max_data_date=_max_date(rows, "announcement_date"),
                )
            )
        else:
            raise SandboxMountError(
                f"发布 {rel_id} 的 dataset_kind={kind.value!r} 不支持挂载"
            )

    if bars_writer.rows == 0:
        raise SandboxMountError(
            "窗口挂载不含任何行情行:dataset_release_ids 须至少包含一个含"
            "目标标的的 bars 类发布"
        )
    bars_writer.finish()
    daily_writer.finish()
    for announced_kind, rows in announced_rows.items():
        await asyncio.to_thread(
            _write_parquet,
            out_root / f"{announced_kind}.parquet",
            rows,
        )

    mount = WindowDataMount(
        root=out_root,
        window_start=window_start,
        window_end=window_end,
        dates=ordered_dates,
        symbols=tuple(sorted(universe)),
        datasets=tuple(contributions),
        manifest_checksum="",
        code_artifact=code_artifact,
        code_commit=code_commit,
        release_id=release_id,
        dataset_release_ids=tuple(dataset_release_ids),
    )
    payload = json.dumps(
        {**mount.manifest_dict(), "generated_at": datetime.now(UTC).isoformat()},
        ensure_ascii=False,
        indent=2,
    )
    await asyncio.to_thread(
        mount.manifest_path.write_text, payload, encoding="utf-8"
    )
    checksum = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return replace(mount, manifest_checksum=checksum)


# --------------------------------------------------------------------------- #
# 逐 kind 采集(PIT 门控走 provider 接口,见 frozen release #187)
# --------------------------------------------------------------------------- #

#: 逐标的读取并行度(issue #375):分块 gather —— 块内并发,块间在消费端
#: 按原序喂流式 writer,行序与串行逐字节一致(#287/#288 先例)。pyarrow
#: 读/解压释放 GIL,真实 provider(#371 列式路径走 ``to_thread``)可吃满
#: 多核;块大小即内存上界(块内全部标的的行驻留,约十几 MB)。
_MOUNT_FETCH_CONCURRENCY = 8


async def _fetch_instrument_batches(
    collect: Callable[[Any], Awaitable[Any]],
    instruments: Sequence[Any],
) -> list[Any]:
    """分块并发采集逐标的批次,结果按 ``instruments`` 原序归位。"""
    batches: list[Any] = []
    for start in range(0, len(instruments), _MOUNT_FETCH_CONCURRENCY):
        chunk = instruments[start : start + _MOUNT_FETCH_CONCURRENCY]
        batches.extend(await asyncio.gather(*(collect(item) for item in chunk)))
    return batches


async def _fetch_bars_batches(
    provider: Any,
    instruments: Sequence[Any],
    gate: datetime,
    *,
    include_available_at: bool,
) -> list[Any]:
    """单 provider 的逐标的 bars 批次(#375 分块并发)。"""

    async def collect(item: Any) -> list[dict[str, Any]]:
        return await _collect_bars(
            provider, [item], gate, include_available_at=include_available_at
        )

    return await _fetch_instrument_batches(collect, instruments)


async def _fetch_daily_rows_batches(
    provider: Any,
    instruments: Sequence[Any],
    gate: datetime,
    *,
    include_available_at: bool,
) -> list[Any]:
    """单 provider 的逐标的 daily_metrics 行批次(对象路径)。"""

    async def collect(item: Any) -> list[dict[str, Any]]:
        return await _collect_daily_metrics(
            provider, [item], gate, include_available_at=include_available_at
        )

    return await _fetch_instrument_batches(collect, instruments)


async def _fetch_daily_mixed_batches(
    provider: Any,
    instruments: Sequence[Any],
    gate: datetime,
) -> list[Any]:
    """单 provider 的逐标的 daily_metrics 批次(列式优先,对象回退)。"""
    columnar = hasattr(provider, "fetch_daily_metrics_columns")

    async def collect(item: Any) -> Any:
        if columnar:
            return await _collect_daily_metrics_columns(provider, item, gate)
        return await _collect_daily_metrics(
            provider, [item], gate, include_available_at=True
        )

    return await _fetch_instrument_batches(collect, instruments)


async def _collect_bars(
    provider: Any,
    instruments: list[Any],
    decision_at: datetime,
    *,
    include_available_at: bool = False,
) -> list[dict[str, Any]]:
    from finboard_shared.models import Symbol

    release = provider.release
    rows: list[dict[str, Any]] = []
    for item in instruments:
        symbol = Symbol(code=item.code, market=item.market)
        points = await provider.fetch_point_in_time_bars(
            symbol,
            release.period,
            release.start_date,
            release.end_date,
            decision_at=decision_at,
            adjust=release.adjustment,
        )
        for point in points:
            bar = point.bar
            row = {
                "symbol": bar.symbol.code,
                "date": bar.timestamp.date(),
                "open": _f(bar.open),
                "high": _f(bar.high),
                "low": _f(bar.low),
                "close": _f(bar.close),
                "volume": _f(bar.volume),
                "amount": _f(bar.amount),
            }
            if include_available_at:
                # 窗口挂载 v3:访问器 PIT 过滤的逐行依据(kit bars_view)
                row["available_at"] = point.available_at
            rows.append(row)
    return rows


async def _collect_daily_metrics(
    provider: Any,
    instruments: list[Any],
    decision_at: datetime,
    *,
    include_available_at: bool = False,
) -> list[dict[str, Any]]:
    release = provider.release
    rows: list[dict[str, Any]] = []
    for item in instruments:
        symbol = _symbol(item)
        records = await provider.fetch_daily_metrics(
            symbol,
            start=release.start_date,
            end=release.end_date,
            decision_at=decision_at,
        )
        for record in records:
            if include_available_at:
                # 窗口挂载 v3:available_at 原样(datetime)落列,kit
                # dataset_view 据此做逐日 PIT 过滤;时间戳/来源等非数值
                # 元数据不进 v3 数值白名单。
                row = {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date,
                    **{
                        f.name: _f(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name not in
                        {"symbol", "trade_date", "available_at",
                         "observed_at", "source"}
                    },
                    "available_at": record.available_at,
                }
            else:
                # v2 单日挂载:与 v3 同口径,非数值元数据(source/observed_at/
                # available_at)不进数值白名单——真实 DailySecurityMetrics 的
                # source 是 str、两个时间戳是 datetime,float 化必崩(#366);
                # kit 端无 available_at 列时按 trade_date 回退做 PIT 过滤
                # (D1 available_at 是日期的确定性函数,逐值等值)。
                row = {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date,
                    **{
                        f.name: _f(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name not in
                        {"symbol", "trade_date", "available_at",
                         "observed_at", "source"}
                    },
                }
            rows.append(row)
    return rows


async def _collect_daily_metrics_columns(
    provider: Any,
    item: Any,
    decision_at: datetime,
) -> pa.Table:
    """列式采集单标的 daily_metrics(issue #371 快路径,v3 挂载专用)。

    消费 ``FrozenReleaseProvider.fetch_daily_metrics_columns``(PIT/区间
    门控在 provider 内完成,语义与对象路径逐值一致),把逐标的原始表整形为
    与 :func:`_collect_daily_metrics` 相同的行结构(symbol / trade_date /
    数值字段按 dataclass 序 / available_at 末列):日期与时间戳两小列逐行
    解析(同一 fromisoformat / ``_coerce_date`` 镜像语义),数值列保持
    Arrow、缺列(旧发布字段白名单更窄)补全 null float64。
    """
    import dataclasses

    from finboard_data.research import DailySecurityMetrics
    from finboard_shared.models import Symbol

    symbol = Symbol(code=item.code, market=item.market)
    release = provider.release
    table = await provider.fetch_daily_metrics_columns(
        symbol,
        start=release.start_date,
        end=release.end_date,
        decision_at=decision_at,
    )
    if table.num_rows == 0:
        return table
    meta_fields = {"symbol", "trade_date", "available_at", "observed_at", "source"}
    metric_fields = [
        f.name
        for f in dataclasses.fields(DailySecurityMetrics)
        if f.name not in meta_fields
    ]
    columns: dict[str, pa.Array | pa.ChunkedArray] = {
        "symbol": pa.array([item.code] * table.num_rows, type=pa.string()),
        "trade_date": pa.array(
            [_date_from_value(v) for v in table.column("trade_date").to_pylist()],
            type=pa.date32(),
        ),
        "available_at": pa.array(
            [
                v if isinstance(v, datetime) else datetime.fromisoformat(str(v))
                for v in table.column("available_at").to_pylist()
            ],
            type=pa.timestamp("us", tz="UTC"),
        ),
    }
    for name in metric_fields:
        if name in table.column_names:
            col = table.column(name)
            # 全 None 列在逐标的文件里可能被推断为 null 类型(数值列统一
            # float64,与对象路径 _f() 归一口径一致;null/int → float64 cast 安全)
            columns[name] = (
                col.cast(pa.float64()) if col.type != pa.float64() else col
            )
        else:
            columns[name] = pa.nulls(table.num_rows, type=pa.float64())
    ordered = ["symbol", "trade_date", *metric_fields, "available_at"]
    return pa.table({name: columns[name] for name in ordered})


def _date_from_value(value: Any) -> date | None:
    """镜像 ``finboard_data.releases._coerce_date``(避免跨包私有导入)。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


async def _collect_announced(
    provider: Any,
    instruments: list[Any],
    decision_at: datetime,
    *,
    fetch_attr: str,
    include_available_at: bool = False,
) -> list[dict[str, Any]]:
    """公告类研究数据集(三表/dividend/#402)的逐标的采集。

    ``fetch_attr`` 为 provider 的取数方法名(``ANNOUNCED_DATASETS`` 映射,
    如 ``fetch_income_statements``);领域记录均为 dataclass(真实
    ``FrozenReleaseProvider`` 与测试 stub 同构),行 = 除 symbol /
    available_at / observed_at / source 外的全部字段 + available_at 末列
    (v3 窗口挂载;v2 单日挂载无 available_at 列)。
    """
    rows: list[dict[str, Any]] = []
    fetch = getattr(provider, fetch_attr)
    for item in instruments:
        symbol = _symbol(item)
        records = await fetch(symbol, decision_at=decision_at)
        for record in records:
            if include_available_at:
                row = {
                    "symbol": record.symbol,
                    **{
                        f.name: _plain(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name not in
                        {"symbol", "available_at", "observed_at", "source"}
                    },
                    "available_at": record.available_at,
                }
            else:
                row = {
                    "symbol": record.symbol,
                    **{
                        f.name: _plain(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name != "symbol"
                    },
                }
            rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# 流式物化(issue #371):逐标的批次写 parquet,不再整发布堆 list[dict]
# --------------------------------------------------------------------------- #

#: 流式写 row group 的目标行数。逐标的批次(单标的 ~2K 行)直接落盘会产生
#: 上万碎 row group,zstd 压缩比与容器端读取性能都显著劣化;先在内存缓冲、
#: 攒满目标行数再写。内存上界 ≈ 目标行数 x 列数 x 8B(~20MB 量级)。
_ROW_GROUP_TARGET_ROWS = 262_144


class _DatasetStreamWriter:
    """单一数据集 parquet 文件的流式写入器(issue #371)。

    旧实现把全部行堆成 ``list[dict]`` 最后一次性 ``_write_parquet``——
    全市场 daily_metrics 发布(5534 标的 x 全区间 ≈ 800 万行)仅 dict 列表
    即 5-6GB。本写入器逐标的收批次,攒满 :data:`_ROW_GROUP_TARGET_ROWS`
    落一个 row group,内存只持缓冲;文件 schema 取首个非空批次的 schema,
    其后批次逐个 cast(数据集内列集恒定,cast 为 no-op,漂移即 fail-visible)。
    空数据集 ``finish`` 时确保文件不存在(与旧 ``_write_parquet(path, [])``
    的 unlink 语义一致)。
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._schema: pa.Schema | None = None
        self._writer: pq.ParquetWriter | None = None
        self._buffer: list[pa.Table] = []
        self._buffer_rows = 0
        #: 已落盘 + 缓冲中的总行数(= 旧实现贡献计数的 len(rows))
        self.rows = 0

    def write(self, table: pa.Table) -> None:
        if table.num_rows == 0:
            return
        if self._schema is None:
            self._schema = table.schema
            self._writer = pq.ParquetWriter(
                self._path, self._schema, compression="zstd"
            )
        if table.schema != self._schema:
            table = table.cast(self._schema)
        self._buffer.append(table)
        self._buffer_rows += table.num_rows
        self.rows += table.num_rows
        if self._buffer_rows >= _ROW_GROUP_TARGET_ROWS:
            self._flush()

    def _flush(self) -> None:
        assert self._writer is not None  # write() 已建
        if not self._buffer:
            return
        table = (
            pa.concat_tables(self._buffer)
            if len(self._buffer) > 1
            else self._buffer[0]
        )
        self._writer.write_table(table, row_group_size=_ROW_GROUP_TARGET_ROWS)
        self._buffer.clear()
        self._buffer_rows = 0

    def finish(self) -> None:
        if self._writer is not None:
            self._flush()
            self._writer.close()
            self._writer = None
        if self.rows == 0:
            self._path.unlink(missing_ok=True)


def _bars_schema(include_available_at: bool) -> pa.Schema:
    """bars 显式 schema:列序与旧实现首行 dict 的键序逐列一致。"""
    fields = [pa.field("symbol", pa.string()), pa.field("date", pa.date32())]
    fields += [
        pa.field(name, pa.float64())
        for name in ("open", "high", "low", "close", "volume", "amount")
    ]
    if include_available_at:
        fields.append(pa.field("available_at", pa.timestamp("us", tz="UTC")))
    return pa.schema(fields)


def _daily_metrics_schema(
    columns: Sequence[str], *, include_available_at: bool
) -> pa.Schema:
    """daily_metrics 显式 schema:列序随首条记录字段序,类型按域规则。

    采集层 ``_f`` 已把全部数值字段规范化为 ``float | None``,故除标识与
    日期列外恒为 float64(旧实现对全 None 列推断 pa.null(),语义差异仅
    在 pandas 端 object-None 列变 float64-NaN 列,消费端读取更稳)。
    """
    fields = []
    for col in columns:
        if col == "symbol":
            fields.append(pa.field(col, pa.string()))
        elif col == "trade_date":
            fields.append(pa.field(col, pa.date32()))
        elif col == "available_at":
            fields.append(pa.field(col, pa.timestamp("us", tz="UTC")))
        else:
            fields.append(pa.field(col, pa.float64()))
    return pa.schema(fields)


# --------------------------------------------------------------------------- #
# 防线:PIT fail-closed + parquet 落盘
# --------------------------------------------------------------------------- #


def _guard_pit(
    rows: list[dict[str, Any]], date_field: str, release_id: str, decision_day: date
) -> None:
    """任何数据日期晚于 decision_at 当日即拒绝生成挂载(物理隔离防线)。

    仅剩 financial_indicators 遗留路径使用(#371:bars / daily_metrics 已
    改流式批次,走 :func:`_guard_pit_table`)。
    """
    violations = [
        r for r in rows if r.get(date_field) and r[date_field] > decision_day
    ]
    if violations:
        sample = violations[0][date_field].isoformat()
        raise SandboxMountError(
            f"PIT 违规:发布 {release_id} 经 provider 门控后仍含 "
            f"{date_field} > {decision_day} 的行({len(violations)} 行,"
            f"如 {sample});拒绝生成挂载"
        )


def _guard_pit_table(
    table: pa.Table, date_field: str, release_id: str, decision_day: date
) -> None:
    """:func:`_guard_pit` 的流式批次版(issue #371):Arrow 计数,文案一致。

    违规计数为**当前批次**内计数——任一批次违规即刻具名拒绝,不再等剩余
    标的走完;fail-closed 语义不变,失败时点提前。
    """
    import pyarrow.compute as pc

    flags = pc.fill_null(pc.greater(table.column(date_field), decision_day), False)
    count = pc.sum(flags).as_py() or 0
    if count:
        idx = pc.index(flags, True).as_py()
        sample = table.column(date_field)[idx].as_py().isoformat()
        raise SandboxMountError(
            f"PIT 违规:发布 {release_id} 经 provider 门控后仍含 "
            f"{date_field} > {decision_day} 的行({count} 行,"
            f"如 {sample});拒绝生成挂载"
        )


def _guard_window_pit(
    rows: list[dict[str, Any]],
    date_field: str,
    release_id: str,
    window_end: date,
) -> None:
    """窗口挂载防线:数据日期晚于 window_end 当日即具名拒绝(窗口外数据)。

    与 :func:`_guard_pit` 同构但独立成法:窗口模式的泄漏上界是窗口末端
    而非决策日,错误文案指明窗口语义与修复方向(检查 provider PIT 门控 /
    窗口参数),供 factor_series 执行链路 fail-closed 分类。

    仅剩 financial_indicators 遗留路径使用(#371)。
    """
    violations = [
        r for r in rows if r.get(date_field) and r[date_field] > window_end
    ]
    if violations:
        sample = violations[0][date_field].isoformat()
        raise SandboxMountError(
            f"窗口外数据:发布 {release_id} 经 provider 门控(上界 "
            f"window_end={window_end} 日终)后仍含 {date_field} > "
            f"{window_end} 的行({len(violations)} 行,如 {sample});"
            "拒绝生成窗口挂载(factor_series 窗口上界 fail-closed)"
        )


def _guard_window_pit_table(
    table: pa.Table, date_field: str, release_id: str, window_end: date
) -> None:
    """:func:`_guard_window_pit` 的流式批次版(issue #371):文案一致。

    违规计数为**当前批次**内计数,任一批次违规即刻拒绝(fail-closed 不变,
    失败时点提前,不再走完全部标的才发现)。
    """
    import pyarrow.compute as pc

    flags = pc.fill_null(pc.greater(table.column(date_field), window_end), False)
    count = pc.sum(flags).as_py() or 0
    if count:
        idx = pc.index(flags, True).as_py()
        sample = table.column(date_field)[idx].as_py().isoformat()
        raise SandboxMountError(
            f"窗口外数据:发布 {release_id} 经 provider 门控(上界 "
            f"window_end={window_end} 日终)后仍含 {date_field} > "
            f"{window_end} 的行({count} 行,如 {sample});"
            "拒绝生成窗口挂载(factor_series 窗口上界 fail-closed)"
        )


def _write_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    """落盘一个数据集(空数据集 = 确保文件不存在;同步阻塞,调用方走 to_thread)。"""
    if not rows:
        path.unlink(missing_ok=True)
        return
    columns = list(rows[0])
    schema = pa.schema(
        [
            pa.field(
                col,
                _arrow_type(row.get(col) for row in rows),
            )
            for col in columns
        ]
    )
    arrays = [
        pa.array([row.get(col) for row in rows], type=schema.field(col).type)
        for col in columns
    ]
    pq.write_table(
        pa.table(arrays, schema=schema),
        path,
        compression="zstd",
    )


def _arrow_type(values: Any) -> pa.DataType:
    """按首个非 None 值推断列类型(Decimal→float64;date 保留 date32)。"""
    for value in values:
        if value is None:
            continue
        if isinstance(value, bool):
            return pa.bool_()
        if isinstance(value, int):
            return pa.int64()
        if isinstance(value, float | Decimal):
            return pa.float64()
        if isinstance(value, datetime):
            return pa.timestamp("us", tz="UTC")
        if isinstance(value, date):
            return pa.date32()
        return pa.string()
    return pa.null()


def _f(value: Any) -> float | None:
    """Decimal/数值 → float(缺失保持 None)。"""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def _plain(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _max_date(rows: list[dict[str, Any]], field: str) -> date | None:
    values = [r[field] for r in rows if r.get(field) is not None]
    return max(values) if values else None


def _max_date_optional(
    current: date | None, rows: list[dict[str, Any]], field: str
) -> date | None:
    """流式归并单 provider 的最大数据日期(#371:逐标的批次累计)。"""
    batch_max = _max_date(rows, field)
    if current is None:
        return batch_max
    if batch_max is not None and batch_max > current:
        return batch_max
    return current


def _max_date_optional_table(
    current: date | None, table: pa.Table, field: str
) -> date | None:
    """表版 :func:`_max_date_optional`(列式快路径批次归并,#371)。"""
    import pyarrow.compute as pc

    batch_max: date | None = (
        pc.max(table.column(field)).as_py() if table.num_rows else None
    )
    if current is None:
        return batch_max
    if batch_max is not None and batch_max > current:
        return batch_max
    return current


def _symbol(item: Any) -> Any:
    from finboard_shared.models import Symbol

    return Symbol(code=item.code, market=item.market)


def _fields(record: Any) -> list[Any]:
    import dataclasses

    return list(dataclasses.fields(record))


__all__ = [
    "WINDOW_MANIFEST_VERSION",
    "DataMount",
    "MountDataset",
    "SandboxMountError",
    "WindowDataMount",
    "build_data_mount",
    "build_window_data_mount",
    "filter_window_data_mount",
]
