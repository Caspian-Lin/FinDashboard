"""因子序列 parquet 工件存储(issue #463,用户拍板「parquet + checksum 工件」)。

``research_factor_series.values`` 此前以 JSONB 行内存储逐决策日截面;
全市场 x 长窗口序列(5263 标的 x 18 个月 ≈ 58MB JSON,全历史 ≈ 440MB/行)
使 DB 行巨型化并撞穿 MCP 64MB 载荷护栏。新写入路径改存 canonical parquet
工件,DB 行只留 ``artifact_relpath`` + 文件 sha256(``content_checksum``):

* 行格式 ``(date date32, symbol string, value float64 可空)`` 的长表 ——
  与 ``{date: {symbol: float|null}}`` 契约逐值等值,且保留「显式 null 值」
  与「标的缺行」的区别(loader 语义:null 产出 value=None 的
  FeatureValue,缺行不产出);
* 日期升序 + 逐日标的排序 → 同一 pyarrow 版本下同内容同字节(重建幂等);
* 落盘原子(tmp + ``os.replace``),读路径先验文件 sha256 再解析(防
  静默损坏;与冻结发布 per-artifact sha256 同口径)。

既有行内 JSONB 行不受影响:``artifact_relpath`` 为空即旧行,checksum
语义不变(逐日截面的 canonical json sha256)。两种模式由 repo 按列判别。
根目录解析:显式参 > 环境变量 ``FINBOARD_FACTOR_SERIES_ARTIFACT_ROOT`` >
默认 ``data_cache/factor_series``(与 research_code_repo_path 同风格,
相对仓库根)。

pyarrow 沿 finboard-data ``cache`` extra 惯例为可选依赖:全部 import 在
函数体内(releases.py 先例),模块导入零 pyarrow;读写在有
pyarrow 的进程(worker / MCP / API)发生。

纯离线研究域存储,不触实盘表。
"""

from __future__ import annotations

import hashlib
import os
import threading
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pyarrow as pa

#: canonical 工件 schema(value 可空 = 显式缺测;缺行 = 标的缺失)
_SCHEMA_SPEC = [
    ("date", "date32"),
    ("symbol", "string"),
    ("value", "float64"),
]

#: 压缩编码(写入确定性:同内容 + 同 pyarrow 版本 → 同字节 → 同 sha256)
_ARTIFACT_COMPRESSION = "zstd"

_SERIES_ARTIFACT_ROOT_ENV = "FINBOARD_FACTOR_SERIES_ARTIFACT_ROOT"
_DEFAULT_ARTIFACT_ROOT = "data_cache/factor_series"

_HASH_CHUNK = 1 << 20


class FactorSeriesArtifactError(RuntimeError):
    """因子序列工件缺失 / 损坏 / 校验和不一致 / 相对路径非法(fail-closed)。"""


@dataclass(frozen=True)
class SeriesArtifactMeta:
    """写入结果:相对路径 + 文件 sha256 + 行数与字节数(归档/护栏用)。"""

    relpath: str
    checksum: str
    row_count: int
    byte_size: int


def resolve_artifact_root(root: str | Path | None = None) -> Path:
    """工件根目录:显式参 > 环境变量 > 默认(相对仓库根)。"""
    return Path(
        root
        or os.getenv(_SERIES_ARTIFACT_ROOT_ENV)
        or _DEFAULT_ARTIFACT_ROOT
    )


def artifact_relpath_for(series_key: str) -> str:
    """内容寻址落盘路径:前 2 位分片防单目录膨胀(releases 逐标的目录同精神)。"""
    return f"{series_key[:2]}/{series_key}.parquet"


def _series_schema() -> pa.Schema:
    import pyarrow as pa

    return pa.schema(
        [
            ("date", pa.date32()),
            ("symbol", pa.string()),
            ("value", pa.float64()),
        ]
    )


def _resolve_artifact_path(root: str | Path, relpath: str) -> Path:
    root = resolve_artifact_root(root)
    candidate = (root / relpath).resolve()
    if not candidate.is_relative_to(root.resolve()):
        raise FactorSeriesArtifactError(f"工件相对路径非法: {relpath!r}")
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _open_verified(root: str | Path, relpath: str, expected_checksum: str) -> Path:
    """解析路径 + 校验文件 sha256;缺失/篡改一律具名失败。"""
    if not expected_checksum:
        raise FactorSeriesArtifactError(
            f"工件行缺少 content_checksum,拒绝读取: {relpath!r}"
        )
    path = _resolve_artifact_path(root, relpath)
    if not path.is_file():
        raise FactorSeriesArtifactError(f"因子序列工件缺失: {relpath}")
    digest = _sha256_file(path)
    if digest != expected_checksum:
        raise FactorSeriesArtifactError(
            f"因子序列工件 sha256 校验失败({relpath}):"
            f"期望 {expected_checksum},实际 {digest};文件可能被篡改或损坏"
        )
    return path


def write_series_artifact(
    root: str | Path,
    series_key: str,
    values: Mapping[Any, Mapping[str, float | None]],
) -> SeriesArtifactMeta:
    """逐日截面 → canonical parquet 工件(原子落盘,返回路径与 sha256)。

    键接受 ``date`` 或 ISO 字符串(统一归一);逐日标的排序、日期升序,
    同内容同字节。value=None 落为 null 行(显式缺测),值域外键不入表
    (标的那日缺行)。
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    normalised: dict[date, dict[str, float | None]] = {}
    for day, day_values in values.items():
        key = day if isinstance(day, date) else date.fromisoformat(str(day))
        normalised[key] = dict(day_values)

    rows_date: list[date] = []
    rows_symbol: list[str] = []
    rows_value: list[float | None] = []
    for day in sorted(normalised):
        for symbol in sorted(normalised[day]):
            value = normalised[day][symbol]
            rows_date.append(day)
            rows_symbol.append(symbol)
            rows_value.append(None if value is None else float(value))

    table = pa.table(
        {
            "date": pa.array(rows_date, type=pa.date32()),
            "symbol": pa.array(rows_symbol, type=pa.string()),
            "value": pa.array(rows_value, type=pa.float64()),
        },
        schema=_series_schema(),
    )

    root_path = resolve_artifact_root(root)
    relpath = artifact_relpath_for(series_key)
    target = _resolve_artifact_path(root_path, relpath)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f"{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex[:8]}")
    try:
        pq.write_table(table, tmp, compression=_ARTIFACT_COMPRESSION)
        checksum = _sha256_file(tmp)
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            tmp.unlink()
    return SeriesArtifactMeta(
        relpath=relpath,
        checksum=checksum,
        row_count=table.num_rows,
        byte_size=target.stat().st_size,
    )


def _read_table(path: Path, *, columns: list[str] | None = None) -> pa.Table:
    import pyarrow.parquet as pq

    return pq.read_table(path, schema=_series_schema(), columns=columns)


def _frame_from_table(table: pa.Table) -> dict[str, dict[str, float | None]]:
    """长表 → ``{ISO 日: {symbol: float|null}}``(单次 to_pylist 物化)。"""
    frame: dict[str, dict[str, float | None]] = {}
    if table.num_rows == 0:
        return frame
    days = table.column("date").to_pylist()
    symbols = table.column("symbol").to_pylist()
    values = table.column("value").to_pylist()
    for day, symbol, value in zip(days, symbols, values, strict=True):
        frame.setdefault(day.isoformat(), {})[symbol] = value
    return frame


def read_series_values(
    root: str | Path,
    relpath: str,
    expected_checksum: str,
) -> dict[str, dict[str, float | None]]:
    """校验 sha256 后整表物化为逐日截面(MCP detail 等全量消费口)。"""
    path = _open_verified(root, relpath, expected_checksum)
    return _frame_from_table(_read_table(path))


def read_series_symbols(
    root: str | Path,
    relpath: str,
    expected_checksum: str,
) -> list[str]:
    """只读 symbol 列(字典编码投影)取标的集;summary 不物化全量 values。"""
    import pyarrow.compute as pc

    path = _open_verified(root, relpath, expected_checksum)
    table = _read_table(path, columns=["symbol"])
    encoded = pc.dictionary_encode(table.column("symbol").combine_chunks())
    return sorted(set(encoded.dictionary.to_pylist()))


def artifact_file_size(root: str | Path, relpath: str) -> int:
    """工件文件字节数(summary 归档展示;缺失视为 0,不抛)。"""
    path = _resolve_artifact_path(root, relpath)
    try:
        return path.stat().st_size
    except OSError:
        return 0


class LazySeriesValues(Mapping[str, dict[str, float | None]]):
    """按日惰性取数的 ``record.values`` 后端(工件行;loader/MCP 零改动)。

    首次访问才读文件并校验 sha256(构造零 IO,入队缓存检查等不触 values
    的路径零开销);单日 ``get`` 走日期列上的 numpy 二分定位行区间后切片
    (O(log n) + 单日 to_pylist,毫秒级),全量迭代(``items``/``values``)
    一次性物化并缓存。构造后不可变。

    issue #464 实测教训:首版首访构建 ``_dates`` 用「整列 to_pylist +
    逐日 isoformat」——全历史 1470 万行 = 分钟级纯对象开销,且 loader 在
    事件循环线程同步调用会阻塞整个 worker(心跳/并发 job 全停)。现首访
    只做「验签 + 读表 + date32 列转 int32 numpy(零对象)」,全量迭代才
    物化;loader 侧取值经 ``asyncio.to_thread``(#464 配套改动)。

    2026-09-14 内存归因(#470 后半场):首访后 arrow 表整表常驻
    (date/symbol/value 三列缓冲,全历史序列 ~0.3GB+ 且对 gc 不可见,
    run 级多序列叠加是决策段驻留大头)。现首访一次性把三列抽成紧凑
    numpy(date int32 + symbol 字典索引 int32 + value float64/null 掩码)
    后**丢弃 arrow 表**,常驻 ≈ 17B/行且逐列可回收;``value`` 的 null 语义
    经布尔掩码保留(显式缺测 None 不与数值 NaN 混同,与 ``to_pylist``
    逐值一致)。
    """

    def __init__(self, root: str | Path, relpath: str, expected_checksum: str) -> None:
        self._root = root
        self._relpath = relpath
        self._expected_checksum = expected_checksum
        self._loaded = False
        # ``values`` is consumed from ``asyncio.to_thread`` by concurrent
        # decision loaders.  Guard the check-then-load transition so a shared
        # record verifies/decompresses its parquet artifact exactly once.
        self._load_lock = threading.RLock()
        #: date32 → int32(自 1970-01-01 的天数;文件即升序)
        self._dates: Any = None
        #: symbol 字典索引 int32(行序)与字典表
        self._symbol_codes: Any = None
        self._symbol_dict: list[str] = []
        #: value float64(null 位填 NaN,以 ``_value_valid`` 掩码为准)
        self._value_values: Any = None
        self._value_valid: Any = None
        self._materialised: dict[str, dict[str, float | None]] | None = None

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        with self._load_lock:
            if self._loaded:
                return
            import numpy as np
            import pyarrow as pa
            import pyarrow.compute as pc
            import pyarrow.parquet as pq

            path = _open_verified(self._root, self._relpath, self._expected_checksum)
            # 逐批流式装载(<3GB 收官):整表 read_table 的解压瞬态(单表 ~1GB,
            # 多条全历史序列首访部分重叠)是 run 期 RSS 尖峰主源(2026-09-14
            # 实测:决策段平台 2.44GB + 首访装载尖峰 2.17GB = 峰值 4.61GB)。
            # 改按行批流式解压、预分配紧凑数组直填,瞬态降到 ~MB 级;产出数组
            # 与整表路径逐值一致(日期 int32 升序、symbol 全局首现字典、
            # null→NaN+掩码),等值由 #470 紧凑化测试锁定。
            parquet_file = pq.ParquetFile(path)
            total_rows = parquet_file.metadata.num_rows
            dates = np.empty(total_rows, dtype=np.int32)
            symbol_codes = np.empty(total_rows, dtype=np.int32)
            value_values = np.empty(total_rows, dtype=np.float64)
            value_valid = np.empty(total_rows, dtype=bool)
            # symbol 列字典编码(read_series_symbols 同构):字典表 list[str]
            # + 行级 int32 索引,字符串本体只在字典表存在一份;批内字典重映射
            # 到全局首现序,与整表 dictionary_encode 的字典序一致。
            symbol_dict: dict[str, int] = {}
            offset = 0
            for batch in parquet_file.iter_batches(batch_size=65536):
                rows = batch.num_rows
                dates[offset : offset + rows] = np.asarray(
                    batch.column("date").cast(pa.int32()).to_numpy(zero_copy_only=False),
                    dtype=np.int32,
                )
                encoded = pc.dictionary_encode(batch.column("symbol"))
                batch_dict = encoded.dictionary.to_pylist()
                remap = np.asarray(
                    [symbol_dict.setdefault(name, len(symbol_dict)) for name in batch_dict],
                    dtype=np.int32,
                )
                symbol_codes[offset : offset + rows] = remap[
                    np.asarray(
                        encoded.indices.to_numpy(zero_copy_only=False), dtype=np.int32
                    )
                ]
                value_col = batch.column("value")
                # value 列:null(显式缺测)→ NaN 占位 + 布尔掩码,None 语义不丢。
                value_values[offset : offset + rows] = np.asarray(
                    value_col.fill_null(float("nan")).to_numpy(zero_copy_only=False),
                    dtype=np.float64,
                )
                value_valid[offset : offset + rows] = np.asarray(
                    pc.is_valid(value_col).to_numpy(zero_copy_only=False), dtype=bool
                )
                offset += rows
            if offset != total_rows:
                raise FactorSeriesArtifactError(
                    f"series 工件行数与 parquet 元数据不符: 实读 {offset} / {total_rows}"
                )
            self._dates = dates
            self._symbol_dict = list(symbol_dict)
            self._symbol_codes = symbol_codes
            self._value_values = value_values
            self._value_valid = value_valid
            self._loaded = True

    def _day_bounds(self, day: date) -> tuple[int, int]:
        """单日行区间 [lo, hi)(date 列升序;numpy 二分,零对象)。"""
        import numpy as np

        self._ensure_loaded()
        key = day.toordinal() - date(1970, 1, 1).toordinal()
        lo = int(np.searchsorted(self._dates, key, side="left"))
        hi = int(np.searchsorted(self._dates, key, side="right"))
        return lo, hi

    def __getitem__(self, key: str) -> dict[str, float | None]:
        if self._materialised is not None:
            return self._materialised[key]
        day = date.fromisoformat(str(key))
        lo, hi = self._day_bounds(day)
        if hi <= lo:
            raise KeyError(key)
        self._ensure_loaded()
        symbols = self._symbol_dict
        out: dict[str, float | None] = {}
        codes = self._symbol_codes[lo:hi].tolist()
        values = self._value_values[lo:hi].tolist()
        valid = self._value_valid[lo:hi].tolist()
        for i, code in enumerate(codes):
            out[symbols[code]] = values[i] if valid[i] else None
        return out

    def __iter__(self) -> Iterator[str]:
        return iter(self._materialise())

    def __len__(self) -> int:
        if self._materialised is not None:
            return len(self._materialised)
        import numpy as np

        self._ensure_loaded()
        return int(np.unique(self._dates).size)

    def __contains__(self, key: object) -> bool:
        if self._materialised is not None:
            return key in self._materialised
        try:
            day = date.fromisoformat(str(key))
        except ValueError:
            return False
        lo, hi = self._day_bounds(day)
        return hi > lo

    def items(self) -> Any:
        materialised = self._materialise()
        return materialised.items()

    def values(self) -> Any:
        materialised = self._materialise()
        return materialised.values()

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def _materialise(self) -> dict[str, dict[str, float | None]]:
        if self._materialised is None:
            with self._load_lock:
                if self._materialised is None:
                    self._ensure_loaded()
                    frame: dict[str, dict[str, float | None]] = {}
                    symbols = self._symbol_dict
                    codes = self._symbol_codes.tolist()
                    values = self._value_values.tolist()
                    valid = self._value_valid.tolist()
                    epoch_day = date(1970, 1, 1).toordinal()
                    for i, day_int in enumerate(self._dates.tolist()):
                        row = frame.setdefault(
                            date.fromordinal(epoch_day + day_int).isoformat(), {}
                        )
                        row[symbols[codes[i]]] = values[i] if valid[i] else None
                    self._materialised = frame
        return self._materialised

    def __repr__(self) -> str:  # 不触发读盘(防 str()/repr() 意外物化)
        return (
            f"<LazySeriesValues relpath={self._relpath!r} "
            f"loaded={self._loaded}>"
        )


__all__ = [
    "FactorSeriesArtifactError",
    "LazySeriesValues",
    "SeriesArtifactMeta",
    "artifact_file_size",
    "artifact_relpath_for",
    "read_series_symbols",
    "read_series_values",
    "resolve_artifact_root",
    "write_series_artifact",
]
