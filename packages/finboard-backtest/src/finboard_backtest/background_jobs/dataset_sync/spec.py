"""SyncSpec —— 数据集同步规格与注册表(issue #392,数据集驱动框架)。

每个研究数据集注册一份 :class:`SyncSpec`,声明:

* ``shape`` —— 枚举形态,决定框架如何切分进度切片:

  - :attr:`EnumShape.FULL_PAGED`:全量分页 / 全量一次(整市场枚举,单切片);
  - :attr:`EnumShape.DAILY_MARKET`:按日全市场(窗口内每个工作日一切片,
    真实交易日由上游空响应跳过);
  - :attr:`EnumShape.PER_SYMBOL_RANGE`:按标的 x 区间(池内每标的一切片);

* ``row_policy`` —— 行级质量口径(#389 固化),由形态**推导**,Spec 不各自
  手写:全市场枚举(FULL_PAGED / DAILY_MARKET)= 单行跳过 + 具名告警
  ``tushare.dirty_row_skipped``、全脏行整批拒;按 symbol 精确查询
  (PER_SYMBOL_RANGE)= 单行违规整批拒;

* ``fetch`` —— provider 绑定(整批拉取一个切片的原始记录);
* ``persist`` —— 落点绑定(经 ``ResearchDataSyncService`` 走批次发布语义,
  或直接写主数据表如 instrument_names / convertible_metadata);
* ``slice_version`` / ``slice_parameters`` —— dataset_version 幂等键形状与
  批次 parameters(与迁移前的旧路径逐字节一致,见 golden 对照)。

框架(``runner.DatasetSyncExecutor``)统一消费注册表:进度上报、#383 job
timing(worker 通用层,本包零额外计时)、行级口径按 Spec 分发、TushareBudget
共享(provider 进程内单例)、``research_sync_batches`` 记账。新增数据集 =
新增一个 SyncSpec + 注册(接入指南见包 docstring 与 PR 正文)。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_data.research import ResearchDataProvider


@dataclass(frozen=True, slots=True)
class SliceQuery:
    """一个同步切片的完整参数(形态决定哪些字段有值;框架按形态生成)。"""

    dataset: str
    row_policy: str
    #: DAILY_MARKET:本切片的交易日。
    trade_date: date | None = None
    #: PER_SYMBOL_RANGE:本切片的标的。
    symbol: str | None = None
    #: PER_SYMBOL_RANGE:窗口(payload start_date / end_date)。
    start_date: date | None = None
    end_date: date | None = None


@dataclass(frozen=True, slots=True)
class PersistContext:
    """落点绑定可用的执行上下文。"""

    session_maker: async_sessionmaker[AsyncSession]
    source: str
    code_version: str
    dataset_version: str
    query: SliceQuery


@dataclass(frozen=True, slots=True)
class PersistResult:
    """一个切片的落库结果(记账 / 日志用)。

    ``accepted_rows`` 为 ``None`` 表示本数据集无批次语义(主数据写入,
    如 instrument_names / convertible_metadata),不产生
    ``research_sync_batches`` 行。
    """

    accepted_rows: int | None = None
    note: str | None = None


type FetchFn = Callable[[ResearchDataProvider, SliceQuery], Awaitable[list[Any]]]
type PersistFn = Callable[[PersistContext, list[Any]], Awaitable[PersistResult]]


class EnumShape(StrEnum):
    """切片枚举形态(框架按此生成切片并分发行级口径)。"""

    FULL_PAGED = "full_paged"
    DAILY_MARKET = "daily_market"
    PER_SYMBOL_RANGE = "per_symbol_range"


class RowPolicy(StrEnum):
    """行级质量口径(#389 固化;由 :data:`ROW_POLICY_BY_SHAPE` 按形态推导)。"""

    #: 单行契约违规跳过 + 具名告警 tushare.dirty_row_skipped;全部行被跳过
    #: 视为上游 schema 破坏,整批拒。
    SKIP = "skip"
    #: 单行契约违规整批拒(按 symbol 精确查询,坏行不值得静默)。
    REJECT = "reject"


#: 形态 → 行级口径的唯一推导表。全市场枚举(整市场档案 / 按日截面)覆盖含
#: 历史前缀代码的退市老股,单条脏行不值得炸整批;按 symbol 精确查询的坏行
#: 意味着该标的自身数据异常,维持整批拒(宁重试不漂移)。
ROW_POLICY_BY_SHAPE: Mapping[EnumShape, RowPolicy] = {
    EnumShape.FULL_PAGED: RowPolicy.SKIP,
    EnumShape.DAILY_MARKET: RowPolicy.SKIP,
    EnumShape.PER_SYMBOL_RANGE: RowPolicy.REJECT,
}


class UnknownDatasetError(KeyError):
    """payload 引用了未注册的数据集名。"""


@dataclass(frozen=True, slots=True)
class SyncSpec:
    """一个数据集的同步规格(注册表条目,全部字段只读)。"""

    #: 数据集名(payload ``datasets`` 白名单 / 幂等键前缀的一部分)。
    name: str
    shape: EnumShape
    #: 人类可读说明(工具描述 / 日志用)。
    title: str
    fetch: FetchFn
    persist: PersistFn
    #: 切片 → dataset_version(幂等键;同 version 已发布切片重跑被服务层跳过)。
    slice_version: Callable[[SliceQuery], str]
    #: 切片 → 批次 parameters(research_sync_batches.parameters)。
    slice_parameters: Callable[[SliceQuery], Mapping[str, object]] = field(
        default=lambda _query: {}
    )
    #: 是否产生 research_sync_batches 行(False = 主数据写入,无批次语义)。
    writes_batch: bool = True
    #: PER_SYMBOL_RANGE 切片是否必须依赖 symbol 池(恒真,由 shape 决定)。
    #: 空池拒绝语义由框架统一执行,Spec 不重复声明。

    @property
    def row_policy(self) -> RowPolicy:
        """行级质量口径(按形态推导,Spec 不各自手写)。"""

        return ROW_POLICY_BY_SHAPE[self.shape]

    @property
    def is_per_symbol(self) -> bool:
        return self.shape is EnumShape.PER_SYMBOL_RANGE


class SyncSpecRegistry:
    """数据集 → SyncSpec 注册表(声明序即默认执行序)。"""

    def __init__(self) -> None:
        self._specs: dict[str, SyncSpec] = {}

    def register(self, spec: SyncSpec) -> None:
        if not spec.name:
            raise ValueError("dataset 名不能为空")
        if spec.name in self._specs:
            raise ValueError(f"dataset 重复注册: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> SyncSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise UnknownDatasetError(name) from exc

    def has(self, name: str) -> bool:
        return name in self._specs

    @property
    def specs(self) -> tuple[SyncSpec, ...]:
        """全部规格,声明序(= 默认执行序)。"""

        return tuple(self._specs.values())

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._specs)

    def default_names(self) -> tuple[str, ...]:
        """缺省数据集清单(payload 未声明 datasets 时)= 全部,声明序。"""

        return tuple(spec.name for spec in self._specs.values())


#: 进程级唯一注册表(pipeline 单体,无多套语义并存的场景)。
SYNC_SPECS = SyncSpecRegistry()


def spec_by_name(name: str) -> SyncSpec:
    """按名取规格;未知名字抛 :class:`UnknownDatasetError`。"""

    return SYNC_SPECS.get(name)


def workdays(start: date, end: date) -> tuple[date, ...]:
    """窗口内的工作日序列(周一至周五;真实交易日由上游空响应跳过)。

    DAILY_MARKET 形态的切片生成器;与旧路径 ``_workdays`` 逐日等值。
    """

    from datetime import timedelta

    if start > end:
        raise ValueError(f"start({start}) 不能晚于 end({end})")
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return tuple(days)


def slices_for_spec(
    spec: SyncSpec, *, start_date: date, end_date: date, symbol_pool: tuple[str, ...]
) -> tuple[SliceQuery, ...]:
    """按规格的枚举形态生成切片序列(框架统一实现,Spec 不各自切)。"""

    policy = spec.row_policy.value
    if spec.shape is EnumShape.FULL_PAGED:
        return (
            SliceQuery(
                dataset=spec.name,
                row_policy=policy,
                start_date=start_date,
                end_date=end_date,
            ),
        )
    if spec.shape is EnumShape.DAILY_MARKET:
        return tuple(
            SliceQuery(
                dataset=spec.name,
                row_policy=policy,
                trade_date=day,
                start_date=start_date,
                end_date=end_date,
            )
            for day in workdays(start_date, end_date)
        )
    return tuple(
        SliceQuery(
            dataset=spec.name,
            row_policy=policy,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
        )
        for symbol in symbol_pool
    )


__all__ = [
    "ROW_POLICY_BY_SHAPE",
    "SYNC_SPECS",
    "EnumShape",
    "FetchFn",
    "PersistContext",
    "PersistFn",
    "PersistResult",
    "RowPolicy",
    "SliceQuery",
    "SyncSpec",
    "SyncSpecRegistry",
    "UnknownDatasetError",
    "slices_for_spec",
    "spec_by_name",
    "workdays",
]
