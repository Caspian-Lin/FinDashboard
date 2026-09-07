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
    <root>/financial_indicators.parquet    # financial_indicators 类发布合并
    <root>/mount_manifest.json             # 挂载清单(PIT 审计锚点)

列契约与 :mod:`finboard_research_kit.context` 的 docstring 一致。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
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
    bars_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    fin_rows: list[dict[str, Any]] = []
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
            rows = await _collect_bars(provider, instruments, decision_at)
            _guard_pit(rows, "date", release_id, decision_day)
            bars_rows.extend(rows)
            universe.update(r["symbol"] for r in rows)
            file_name, max_day = "bars.parquet", _max_date(rows, "date")
        elif kind.value == "daily_metrics":
            rows = await _collect_daily_metrics(
                provider, instruments, decision_at
            )
            _guard_pit(rows, "trade_date", release_id, decision_day)
            daily_rows.extend(rows)
            file_name, max_day = "daily_metrics.parquet", _max_date(
                rows, "trade_date"
            )
        elif kind.value == "financial_indicators":
            rows = await _collect_financial(provider, instruments, decision_at)
            _guard_pit(rows, "announcement_date", release_id, decision_day)
            fin_rows.extend(rows)
            file_name, max_day = (
                "financial_indicators.parquet",
                _max_date(rows, "announcement_date"),
            )
        else:
            raise SandboxMountError(
                f"发布 {release_id} 的 dataset_kind={kind.value!r} 不支持挂载"
            )
        contributions.append(
            MountDataset(
                release_id=release_id,
                dataset_kind=kind.value,
                file=file_name,
                row_count=len(rows),
                max_data_date=max_day,
            )
        )

    if not bars_rows:
        raise SandboxMountError(
            "挂载不含任何行情行:dataset_release_ids 须至少包含一个含目标"
            "标的的 bars 类发布"
        )
    await asyncio.to_thread(_write_parquet, out_root / "bars.parquet", bars_rows)
    await asyncio.to_thread(
        _write_parquet, out_root / "daily_metrics.parquet", daily_rows
    )
    await asyncio.to_thread(
        _write_parquet, out_root / "financial_indicators.parquet", fin_rows
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
    bars_rows: list[dict[str, Any]] = []
    daily_rows: list[dict[str, Any]] = []
    fin_rows: list[dict[str, Any]] = []
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
            rows = await _collect_bars(
                provider, instruments, ceiling, include_available_at=True
            )
            _guard_window_pit(rows, "date", rel_id, window_end)
            bars_rows.extend(rows)
            universe.update(r["symbol"] for r in rows)
            file_name, max_day = "bars.parquet", _max_date(rows, "date")
        elif kind.value == "daily_metrics":
            rows = await _collect_daily_metrics(
                provider, instruments, ceiling, include_available_at=True
            )
            _guard_window_pit(rows, "trade_date", rel_id, window_end)
            daily_rows.extend(rows)
            file_name, max_day = "daily_metrics.parquet", _max_date(
                rows, "trade_date"
            )
        elif kind.value == "financial_indicators":
            rows = await _collect_financial(
                provider, instruments, ceiling, include_available_at=True
            )
            _guard_window_pit(rows, "announcement_date", rel_id, window_end)
            fin_rows.extend(rows)
            file_name, max_day = (
                "financial_indicators.parquet",
                _max_date(rows, "announcement_date"),
            )
        else:
            raise SandboxMountError(
                f"发布 {rel_id} 的 dataset_kind={kind.value!r} 不支持挂载"
            )
        contributions.append(
            MountDataset(
                release_id=rel_id,
                dataset_kind=kind.value,
                file=file_name,
                row_count=len(rows),
                max_data_date=max_day,
            )
        )

    if not bars_rows:
        raise SandboxMountError(
            "窗口挂载不含任何行情行:dataset_release_ids 须至少包含一个含"
            "目标标的的 bars 类发布"
        )
    await asyncio.to_thread(_write_parquet, out_root / "bars.parquet", bars_rows)
    await asyncio.to_thread(
        _write_parquet, out_root / "daily_metrics.parquet", daily_rows
    )
    await asyncio.to_thread(
        _write_parquet, out_root / "financial_indicators.parquet", fin_rows
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
                row = {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date,
                    **{
                        f.name: _f(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name not in {"symbol", "trade_date"}
                    },
                }
            rows.append(row)
    return rows


async def _collect_financial(
    provider: Any,
    instruments: list[Any],
    decision_at: datetime,
    *,
    include_available_at: bool = False,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in instruments:
        symbol = _symbol(item)
        records = await provider.fetch_financial_indicators(
            symbol, decision_at=decision_at
        )
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
# 防线:PIT fail-closed + parquet 落盘
# --------------------------------------------------------------------------- #


def _guard_pit(
    rows: list[dict[str, Any]], date_field: str, release_id: str, decision_day: date
) -> None:
    """任何数据日期晚于 decision_at 当日即拒绝生成挂载(物理隔离防线)。"""
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
]
