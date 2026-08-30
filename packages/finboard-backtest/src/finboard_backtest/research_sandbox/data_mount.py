"""沙箱数据面 —— 冻结发布 → 只读挂载目录(issue #216)。

在**服务端**把 ``dataset_release_ids`` 指向的冻结发布按 ``decision_at``
物化成一个挂载目录:逐标的走 ``FrozenReleaseProvider`` 的 PIT 门控接口
(``available_at <= decision_at``),行级转 float 后写 parquet,并落
``mount_manifest.json`` 清单。

**PIT 由物理隔离保证**:容器内不存在未来数据文件 —— 挂载内容本身即
decision_at 之前的数据。本模块在 provider 门控之上再做一层防线:任何
数据日期晚于 decision_at 当日的行直接 fail-closed(视为上游/provider
缺陷,拒绝生成挂载),杜绝「门控被绕过 → 未来数据进容器」的整类事故。

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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq

MANIFEST_VERSION = 2
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


# --------------------------------------------------------------------------- #
# 逐 kind 采集(PIT 门控走 provider 接口,见 frozen release #187)
# --------------------------------------------------------------------------- #


async def _collect_bars(
    provider: Any, instruments: list[Any], decision_at: datetime
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
            rows.append(
                {
                    "symbol": bar.symbol.code,
                    "date": bar.timestamp.date(),
                    "open": _f(bar.open),
                    "high": _f(bar.high),
                    "low": _f(bar.low),
                    "close": _f(bar.close),
                    "volume": _f(bar.volume),
                    "amount": _f(bar.amount),
                }
            )
    return rows


async def _collect_daily_metrics(
    provider: Any, instruments: list[Any], decision_at: datetime
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
            rows.append(
                {
                    "symbol": record.symbol,
                    "trade_date": record.trade_date,
                    **{
                        f.name: _f(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name not in {"symbol", "trade_date"}
                    },
                }
            )
    return rows


async def _collect_financial(
    provider: Any, instruments: list[Any], decision_at: datetime
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in instruments:
        symbol = _symbol(item)
        records = await provider.fetch_financial_indicators(
            symbol, decision_at=decision_at
        )
        for record in records:
            rows.append(
                {
                    "symbol": record.symbol,
                    **{
                        f.name: _plain(getattr(record, f.name))
                        for f in _fields(record)
                        if f.name != "symbol"
                    },
                }
            )
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
    "DataMount",
    "MountDataset",
    "SandboxMountError",
    "build_data_mount",
]
