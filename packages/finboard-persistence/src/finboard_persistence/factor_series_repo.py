"""内容寻址的因子序列工件 Repository(issue #360;#463 工件存储)。

因子序列是派生工件:``series_key`` 按 (代码 commit, bars 主发布, 研究
发布联合集, params, 窗口) 内容寻址 —— 相同输入必然命中同一条记录
(params dict 顺序漂移免疫,canonical json 排序后哈希),build job 以此
做缓存命中(unchanged)/重建分流。``values`` 为 ``{date: {symbol:
float|null}}`` 逐决策日截面,research_run 加载器按 (因子名, 决策日)
索引消费(#360 双轨优先路径,回退既有快照路径)。

两种存储模式(#463,按 ``artifact_relpath`` 判别):旧行内 JSONB
(checksum = 逐日截面 canonical json sha256,values 即反序列化 dict);
新 canonical parquet 工件(values 为 NULL,content_checksum = 工件文件
sha256,record.values 为 :class:`LazySeriesValues` 惰性映射,首访才读
文件并验 sha256)。写入路径(执行器)一律落工件。

并发 upsert 沿用 #204 ``INSERT ... ON CONFLICT DO NOTHING RETURNING``
复查先例:两个 worker 并发保存同一 series_key 时(互相看不到对方未提交
的行),只有一方真正插入,另一方按 series_key 复查复用已存在行;内容
寻址保证同 key 同内容,复查发现 checksum 不一致即确定性破坏,fail-closed。

纯离线研究域存储,不迁移历史快照,不触实盘表。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.factor_series_store import (
    LazySeriesValues,
    resolve_artifact_root,
)
from finboard_persistence.models import ResearchFactorSeriesModel

#: series_key 的版本前缀(canonical 规则变更时递增,旧键永不复用)
SERIES_KEY_VERSION = "v2"


class FactorSeriesConflictError(RuntimeError):
    """同一 series_key 的内容校验和冲突(确定性破坏,fail-closed)。"""


def canonical_json(value: Any) -> str:
    """series_key / content_checksum 的 canonical JSON(#360 钉死规则)。"""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def compute_series_key(
    *,
    code_commit: str,
    release_id: str,
    dataset_release_ids: Sequence[str],
    params: Mapping[str, Any],
    window_start: date,
    window_end: date,
) -> str:
    """内容寻址键:canonical 规则严格按 issue #360。

    ``sha256("v2|" + code_commit + "|" + release_id + "|" +
    ",".join(sorted(dataset_release_ids)) + "|" + canonical_json(params) +
    "|" + str(window_start) + "|" + str(window_end))`` —— params dict 的
    键序漂移经 ``sort_keys`` 免疫,日期一律 ``str(date)``(ISO)序列化。
    """
    payload = "|".join(
        (
            SERIES_KEY_VERSION,
            code_commit,
            release_id,
            ",".join(sorted(dataset_release_ids)),
            canonical_json(dict(params)),
            str(window_start),
            str(window_end),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_content_checksum(
    dates: Sequence[date],
    values: Mapping[Any, Mapping[str, float | None]],
) -> str:
    """内容校验和:sha256(dates + values 的 canonical json)。

    ``values`` 的键接受 ``date`` 或 ISO 字符串(统一 ``str(day)`` 归一,
    两种传法产出同一 checksum;容器产出 #359 结果契约两态均可)。
    """
    payload = {
        "dates": [item.isoformat() for item in dates],
        "values": {
            str(day): dict(sorted(day_values.items()))
            for day, day_values in values.items()
        },
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def series_id_for(series_key: str) -> str:
    """series_id = ``"FS-" + series_key[:12]``(与 series_key 一一对应)。"""
    return f"FS-{series_key[:12]}"


@dataclass(frozen=True)
class FactorSeriesRecord:
    """``research_factor_series`` 逐列对应;dates/values 已反序列化。

    ``dataset_release_ids`` 排序冻结(与 series_key 的 canonical 规则一致);
    ``dates`` 升序决策日;``values`` 为 ``{date(ISO): {symbol: float|null}}``。

    两种存储模式(issue #463,按 ``artifact_relpath`` 判别):

    * 行内(旧行,``artifact_relpath=None``)—— values 为反序列化 dict,
      ``content_checksum`` = 逐日截面的 canonical json sha256(#360 原语义);
    * 工件(新写入,``artifact_relpath`` 非 None)—— values 为
      :class:`LazySeriesValues` 惰性映射(首次访问才读 parquet 并验
      sha256),``content_checksum`` = 工件文件 sha256。
    """

    series_id: str
    series_key: str
    code_artifact: str
    code_commit: str
    kind: str
    release_id: str
    dataset_release_ids: tuple[str, ...]
    params: dict[str, Any] = field(default_factory=dict)
    window_start: date = date(1970, 1, 1)
    window_end: date = date(1970, 1, 1)
    dates: tuple[date, ...] = ()
    values: Mapping[str, Mapping[str, float | None]] = field(default_factory=dict)
    content_checksum: str = ""
    quality: dict[str, Any] | None = None
    source_run_id: str | None = None
    artifact_relpath: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    # issue #450:content_checksum 校验开关(仅 __post_init__ 行为,不属于
    # 内容 —— compare/repr 均排除,不影响相等性与序列化)。写入路径与用户
    # 直接构造保持默认 True(逐位不变);持久化读取行经 _record_from_row 传
    # False —— 写入时已校验过,读取路径重算需对全量 dates+values 做
    # canonical json dumps(75 期 x 4 序列的 run 每期重复这份数秒级开销),
    # 信任已落库内容。series_key 校验廉价,读取路径保留。
    verify_content_checksum: bool = field(
        default=True, compare=False, repr=False
    )

    def __post_init__(self) -> None:
        expected_key = compute_series_key(
            code_commit=self.code_commit,
            release_id=self.release_id,
            dataset_release_ids=self.dataset_release_ids,
            params=self.params,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        if self.series_key != expected_key:
            raise ValueError(
                "series_key 与内容寻址规则不一致: "
                f"expected {expected_key}, got {self.series_key}"
            )
        expected_id = series_id_for(self.series_key)
        if self.series_id != expected_id:
            raise ValueError(
                f"series_id 必须为 {expected_id}(FS- + series_key[:12]),"
                f"got {self.series_id}"
            )
        if self.artifact_relpath is not None:
            # 工件行:content_checksum = 文件 sha256,不经 __post_init__
            # 重验(重验须读文件,违背惰性;完整性由读取路径 _open_verified
            # 逐次把关)。工件行缺 checksum 视为构造错误。
            if not self.content_checksum:
                raise ValueError("工件行(artifact_relpath 非空)缺少 content_checksum")
            return
        if self.verify_content_checksum and self.content_checksum != (
            compute_content_checksum(self.dates, self.values)
        ):
            raise ValueError("content_checksum 与 dates/values 内容不一致")

    @classmethod
    def build(
        cls,
        *,
        code_artifact: str,
        code_commit: str,
        kind: str,
        release_id: str,
        dataset_release_ids: Sequence[str],
        params: Mapping[str, Any],
        window_start: date,
        window_end: date,
        dates: Sequence[date],
        values: Mapping[Any, Mapping[str, float | None]],
        quality: Mapping[str, Any] | None = None,
        source_run_id: str | None = None,
    ) -> FactorSeriesRecord:
        """按 canonical 规则派生 series_key / content_checksum / series_id。"""
        series_key = compute_series_key(
            code_commit=code_commit,
            release_id=release_id,
            dataset_release_ids=dataset_release_ids,
            params=params,
            window_start=window_start,
            window_end=window_end,
        )
        materialised_values = {
            str(day): dict(day_values) for day, day_values in values.items()
        }
        return cls(
            series_id=series_id_for(series_key),
            series_key=series_key,
            code_artifact=code_artifact,
            code_commit=code_commit,
            kind=kind,
            release_id=release_id,
            dataset_release_ids=tuple(sorted(dataset_release_ids)),
            params=dict(params),
            window_start=window_start,
            window_end=window_end,
            dates=tuple(dates),
            values=materialised_values,
            content_checksum=compute_content_checksum(dates, values),
            quality=None if quality is None else dict(quality),
            source_run_id=source_run_id,
        )

    @classmethod
    def build_artifact(
        cls,
        *,
        code_artifact: str,
        code_commit: str,
        kind: str,
        release_id: str,
        dataset_release_ids: Sequence[str],
        params: Mapping[str, Any],
        window_start: date,
        window_end: date,
        dates: Sequence[date],
        artifact_relpath: str,
        artifact_checksum: str,
        quality: Mapping[str, Any] | None = None,
        source_run_id: str | None = None,
    ) -> FactorSeriesRecord:
        """工件模式构造(issue #463):``content_checksum`` = 工件文件 sha256。

        ``values`` 恒空(内容在 parquet 工件;读取路径经 LazySeriesValues
        惰性提供)。series_key 派生规则与 :meth:`build` 完全一致 —— 同一
        (commit, 发布联合集, params, 窗口) 在两种模式下同 key。
        """
        series_key = compute_series_key(
            code_commit=code_commit,
            release_id=release_id,
            dataset_release_ids=dataset_release_ids,
            params=params,
            window_start=window_start,
            window_end=window_end,
        )
        return cls(
            series_id=series_id_for(series_key),
            series_key=series_key,
            code_artifact=code_artifact,
            code_commit=code_commit,
            kind=kind,
            release_id=release_id,
            dataset_release_ids=tuple(sorted(dataset_release_ids)),
            params=dict(params),
            window_start=window_start,
            window_end=window_end,
            dates=tuple(dates),
            values={},
            content_checksum=artifact_checksum,
            quality=None if quality is None else dict(quality),
            source_run_id=source_run_id,
            artifact_relpath=artifact_relpath,
        )


def series_coverage_missing(
    record: FactorSeriesRecord,
    decision_dates: Sequence[date],
) -> list[date]:
    """序列未覆盖的决策日(升序去重)。

    判定 = 决策日不在 ``record.dates`` 中(升序序列);窗口外的决策日
    自然不在 dates 里,由同一口径覆盖。
    """
    covered = set(record.dates)
    return sorted({item for item in decision_dates if item not in covered})


class FactorSeriesRepository:
    """内容寻址幂等保存 / 回读 / 精确匹配查询。

    ``artifact_root``(#463):工件根目录;缺省经环境变量
    ``FINBOARD_FACTOR_SERIES_ARTIFACT_ROOT`` 回退 ``data_cache/factor_series``。
    工件行的 ``record.values`` 为惰性映射,首访才读文件 —— 不触 values 的
    路径(入队缓存检查 / coverage 检查)零文件 IO。
    """

    def __init__(
        self, session: AsyncSession, *, artifact_root: str | Path | None = None
    ) -> None:
        self._session = session
        self._artifact_root = resolve_artifact_root(artifact_root)

    async def upsert(self, record: FactorSeriesRecord) -> FactorSeriesRecord:
        """按 series_key 幂等保存;同 key 同 checksum 复用原记录(#204 先例)。

        INSERT ... ON CONFLICT DO NOTHING 由唯一索引原子仲裁 series_key:
        并发保存同一序列时只有一方真正插入,另一方复查复用;复查发现
        content_checksum 不一致是确定性破坏(非确定代码产出不同值),
        fail-closed 抛 :class:`FactorSeriesConflictError`,不静默覆盖。
        """
        is_artifact = record.artifact_relpath is not None
        statement = (
            pg_insert(ResearchFactorSeriesModel)
            .values(
                series_id=record.series_id,
                series_key=record.series_key,
                code_artifact=record.code_artifact,
                code_commit=record.code_commit,
                kind=record.kind,
                release_id=record.release_id,
                dataset_release_ids=list(record.dataset_release_ids),
                params=record.params,
                window_start=record.window_start,
                window_end=record.window_end,
                dates=[item.isoformat() for item in record.dates],
                # 工件行:values 落 NULL(内容在 parquet + sha256 锚定)
                values=None if is_artifact else record.values,
                artifact_relpath=record.artifact_relpath,
                content_checksum=record.content_checksum,
                quality=record.quality,
                source_run_id=record.source_run_id,
            )
            .on_conflict_do_nothing(index_elements=[ResearchFactorSeriesModel.series_key])
            .returning(ResearchFactorSeriesModel.id)
        )
        inserted_id = (
            await self._session.execute(statement)
        ).scalar_one_or_none()
        if inserted_id is None:
            existing = await self._row_by_series_key(record.series_key)
            if existing is None:
                # READ COMMITTED 下仲裁通过的冲突行对本事务可见,正常到不了
                # 这里;防御性报错,避免把 None 返回给调用方。
                raise FactorSeriesConflictError(
                    f"series_key 冲突但行不存在: {record.series_key}"
                )
            persisted = self._record_from_row(existing)
            if persisted.content_checksum != record.content_checksum:
                raise FactorSeriesConflictError(
                    "同一 series_key 的 content_checksum 不一致"
                    f"({record.series_key}: 既有 {persisted.content_checksum},"
                    f"本次 {record.content_checksum});因子代码须确定性产出,"
                    "拒绝覆盖"
                )
            return persisted
        await self._session.flush()
        row = await self._row_by_id(inserted_id)
        assert row is not None  # 刚插入的行必然可见
        return self._record_from_row(row)

    async def get(self, series_id: str) -> FactorSeriesRecord | None:
        row = (
            await self._session.execute(
                select(ResearchFactorSeriesModel).where(
                    ResearchFactorSeriesModel.series_id == series_id
                )
            )
        ).scalar_one_or_none()
        return None if row is None else self._record_from_row(row)

    async def find_matching(
        self,
        *,
        code_artifact: str,
        release_id: str,
        dataset_release_ids: Sequence[str],
        params: Mapping[str, Any],
        window_start: date,
        window_end: date,
    ) -> FactorSeriesRecord | None:
        """按 series_key 精确命中(content addressing),不模糊匹配窗口。

        该 artifact 的全部已存序列中,取其 (commit, 本次 release/联合集/
        params/窗口) 组合出的 series_key 与行自身 series_key 一致者 ——
        无论 artifact 当前 active commit 指向何处(内容寻址不依赖可变状态)。
        """
        rows = (
            (
                await self._session.execute(
                    select(ResearchFactorSeriesModel).where(
                        ResearchFactorSeriesModel.code_artifact == code_artifact
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            expected = compute_series_key(
                code_commit=row.code_commit,
                release_id=release_id,
                dataset_release_ids=dataset_release_ids,
                params=params,
                window_start=window_start,
                window_end=window_end,
            )
            if expected == row.series_key:
                return self._record_from_row(row)
        return None

    async def list_for_release(
        self, release_id: str
    ) -> list[FactorSeriesRecord]:
        rows = (
            (
                await self._session.execute(
                    select(ResearchFactorSeriesModel)
                    .where(ResearchFactorSeriesModel.release_id == release_id)
                    .order_by(ResearchFactorSeriesModel.series_id)
                )
            )
            .scalars()
            .all()
        )
        return [self._record_from_row(row) for row in rows]

    async def list_for_artifact(self, code_artifact: str) -> list[FactorSeriesRecord]:
        """按代码产物名列出全部序列(validate 锚定提示 / 重建清单用)。"""
        rows = (
            (
                await self._session.execute(
                    select(ResearchFactorSeriesModel)
                    .where(ResearchFactorSeriesModel.code_artifact == code_artifact)
                    .order_by(ResearchFactorSeriesModel.series_id)
                )
            )
            .scalars()
            .all()
        )
        return [self._record_from_row(row) for row in rows]

    # ---- 内部 -------------------------------------------------------------

    async def _row_by_series_key(
        self, series_key: str
    ) -> ResearchFactorSeriesModel | None:
        return (
            await self._session.execute(
                select(ResearchFactorSeriesModel).where(
                    ResearchFactorSeriesModel.series_key == series_key
                )
            )
        ).scalar_one_or_none()

    async def _row_by_id(self, row_id: int) -> ResearchFactorSeriesModel | None:
        return await self._session.get(ResearchFactorSeriesModel, row_id)

    def _record_from_row(
        self, row: ResearchFactorSeriesModel
    ) -> FactorSeriesRecord:
        artifact_relpath = (
            str(row.artifact_relpath) if row.artifact_relpath else None
        )
        if artifact_relpath is not None:
            # #463 工件行:values 惰性映射(首访才读文件并验 sha256);
            # 行内 JSONB 为 NULL,不解析。
            values: Mapping[str, Mapping[str, float | None]] = LazySeriesValues(
                self._artifact_root, artifact_relpath, str(row.content_checksum)
            )
        else:
            raw_values = cast(
                "dict[str, object]", dict(row.values) if row.values else {}
            )
            parsed: dict[str, dict[str, float | None]] = {}
            for day, day_values_obj in raw_values.items():
                day_values = cast("dict[str, object]", day_values_obj)
                parsed[str(day)] = {
                    str(sym): None if val is None else float(str(val))
                    for sym, val in day_values.items()
                }
            values = parsed
        params = dict(row.params) if row.params is not None else {}
        return FactorSeriesRecord(
            series_id=row.series_id,
            series_key=row.series_key,
            code_artifact=row.code_artifact,
            code_commit=row.code_commit,
            kind=row.kind,
            release_id=row.release_id,
            dataset_release_ids=tuple(str(item) for item in row.dataset_release_ids),
            params=params,
            window_start=row.window_start,
            window_end=row.window_end,
            dates=tuple(date.fromisoformat(str(item)) for item in row.dates),
            values=values,
            content_checksum=row.content_checksum,
            quality=dict(row.quality) if row.quality is not None else None,
            source_run_id=row.source_run_id,
            artifact_relpath=artifact_relpath,
            created_at=row.created_at,
            updated_at=row.updated_at,
            # issue #450:持久化读取信任写入时已校验的 content_checksum,
            # 跳过全量 canonical json 重算(读取路径主导开销)。
            verify_content_checksum=False,
        )


__all__ = [
    "SERIES_KEY_VERSION",
    "FactorSeriesConflictError",
    "FactorSeriesRecord",
    "FactorSeriesRepository",
    "canonical_json",
    "compute_content_checksum",
    "compute_series_key",
    "series_coverage_missing",
    "series_id_for",
]
