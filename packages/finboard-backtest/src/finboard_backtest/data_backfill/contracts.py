"""补数请求契约(issue #511)。

课题发现数据缺口后,把「缺什么」转成一条**可追踪的共享补数请求**:
请求携带 #510 的统一范围(:class:`DataScopeSpec`)+ 每个数据集的目标窗口 /
标的 / 来源 / 复权 / 是否发布,平台据此预检、去重、编排既有同步与发布内核。

本模块只定义**冻结契约**(pydantic frozen + ``extra="forbid"``,与
``data_scope/contracts.py`` 同风格)与稳定 fingerprint:

* :class:`BackfillRequestSpec` —— 提交/预检入参(规范化后算 fingerprint);
* :class:`BackfillPlan` —— 预检结果(缺口 / 覆盖来源 / 步骤 / 估算 / 配额 /
  发布影响 / 阻塞),同时也是提交前 drift 检测的比对基准(``plan_checksum``);
* 状态/动作枚举 —— 请求 ``queued → running → succeeded | partial | failed |
  cancelled``(需求受阻另落 ``blocked`` 行),明细 ``pending → running →
  succeeded | failed | cancelled | reused``。

语义边界(为什么这样切):

* **不混源 / 不混复权**:``source`` 与 ``adjustment`` 是 item 身份维度并进
  fingerprint —— 不同来源、不同复权口径的覆盖永远不算「已覆盖」;
* **缺口是集合差**:``missing_ranges`` 是「请求区间 - (已完成 并集 在途)」的
  区间差,保留独立缺口而不是把整段范围重复跑一遍;
* **发布预告只给身份**:发布是不可变资产,预检只预告将产生的 release_id /
  version,不读取也不修改历史发布。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from finboard_backtest.data_scope.contracts import DataScopeSpec, scope_checksum
from finboard_data.datasets import CatalogDataset

#: 契约版本:字段语义变化时递增(与 #510 的 ``SCOPE_VERSION`` 同精神)。
BACKFILL_VERSION: Literal["v1"] = "v1"

#: ``missing_symbols.preview`` 上限:预检只给有界预览,全量清单在明细接口分页。
SYMBOL_PREVIEW_LIMIT = 50

#: 复权口径(与发布侧 ``adjustment`` 同词表;非价格数据固定 ``none``)。
Adjustment = Literal["qfq", "hqfq", "none"]

#: (start, end) 闭区间,用于区间差 / 覆盖来源。
DateRange = tuple[date, date]


class BackfillRequestStatus(StrEnum):
    """补数请求状态机(落 ``data_backfill_requests.status``)。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    #: 部分 item 成功、部分失败 —— 成功的发布保留可用,失败项可经 retry 续补。
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 预检发现不受支持/无来源/额度不足:登记需求但不建 job(可查询、可审计)。
    BLOCKED = "blocked"


class BackfillItemStatus(StrEnum):
    """补数明细状态(落 ``data_backfill_request_items.status``)。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    #: 已被既有发布/在途请求覆盖,本 item 不重复执行。
    REUSED = "reused"


class BackfillAction(StrEnum):
    """单个 item 的补齐动作(执行器按此组合既有四个执行器)。"""

    #: 已完全覆盖,不产生任何任务。
    NOOP = "noop"
    #: 缺口已被其他在途请求覆盖,只登记复用关系。
    REUSE = "reuse"
    #: 只同步/摄取,不发布(数据集本身不可发布,或调用方显式关闭发布)。
    SYNC = "sync"
    #: 同步 + 冻结新发布(issue 验收:成功后产生可被其他课题选用的新发布)。
    SYNC_PUBLISH = "sync_publish"
    #: 覆盖已存在但质量异常,走缓存修复 + 重新发布(bars 专属)。
    REPAIR = "repair"


#: 在途覆盖判定用到的请求状态(只算还没进终态的)。
INFLIGHT_REQUEST_STATUSES: frozenset[str] = frozenset(
    {BackfillRequestStatus.QUEUED.value, BackfillRequestStatus.RUNNING.value}
)

#: 请求终态(不再产生覆盖语义;失败/取消可 retry 成新请求)。
TERMINAL_REQUEST_STATUSES: frozenset[str] = frozenset(
    {
        BackfillRequestStatus.SUCCEEDED.value,
        BackfillRequestStatus.PARTIAL.value,
        BackfillRequestStatus.FAILED.value,
        BackfillRequestStatus.CANCELLED.value,
        BackfillRequestStatus.BLOCKED.value,
    }
)

#: 明细里「已完成」的状态(参与已完成覆盖;failed/cancelled 可被原子重领)。
DONE_ITEM_STATUSES: frozenset[str] = frozenset(
    {
        BackfillItemStatus.SUCCEEDED.value,
        BackfillItemStatus.REUSED.value,
    }
)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BackfillBlocker(_Frozen):
    """预检阻塞项(逐条具名;有 blocker 的 item 不执行,fail-closed)。"""

    code: str
    message: str
    dataset: CatalogDataset | None = None


class BackfillDatasetSpec(_Frozen):
    """单个数据集的补数目标。

    ``start`` / ``end`` 缺省从 scope 的解析窗口继承(warmup_start ..
    valuation_end);显式声明时必须落在该窗口内 —— 补数不能顺手改范围。
    """

    dataset: CatalogDataset
    #: None = 使用数据集默认来源(tushare / bars 由 provider 解析)。
    source: str | None = None
    #: 缺省 ``qfq``:bars 缓存只写前复权(``bulk_download`` 无 adjust 维度);
    #: 非价格数据集由 planner 覆盖为 ``none``(请求值不参与)。
    adjustment: Adjustment = "qfq"
    start: date | None = None
    end: date | None = None
    #: 标的子集(空 = 沿用 scope 解析出的标的域)。
    symbols: tuple[str, ...] = ()
    #: 是否在补数成功后冻结新发布(研究类数据集默认 True)。
    publish: bool = True

    @model_validator(mode="after")
    def _validate(self) -> BackfillDatasetSpec:
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("datasets.start 不能晚于 end")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("datasets.symbols 不能重复")
        if any(not item.strip() for item in self.symbols):
            raise ValueError("datasets.symbols 不能包含空串")
        return self


class BackfillRequestSpec(_Frozen):
    """一次补数请求(预检与提交共用)。"""

    backfill_version: Literal["v1"] = BACKFILL_VERSION
    scope: DataScopeSpec
    datasets: tuple[BackfillDatasetSpec, ...]
    quality_profile: Literal["standard", "strict"] = "standard"

    @model_validator(mode="after")
    def _validate(self) -> BackfillRequestSpec:
        if not self.datasets:
            raise ValueError("datasets 不能为空")
        keys = [
            (item.dataset.value, item.source or "", item.adjustment)
            for item in self.datasets
        ]
        if len(set(keys)) != len(keys):
            raise ValueError(
                "datasets 不能重复声明同一 (dataset, source, adjustment)组合"
                " —— 不同来源/复权口径请各成一项"
            )
        return self

    def ordered_datasets(self) -> tuple[BackfillDatasetSpec, ...]:
        """按 (dataset, source, adjustment) 定序(fingerprint / 执行序共用)。"""

        return tuple(
            sorted(
                self.datasets,
                key=lambda item: (
                    item.dataset.value,
                    item.source or "",
                    item.adjustment,
                ),
            )
        )

    def canonical_payload(self) -> dict[str, object]:
        """稳定 JSON 负载(fingerprint 用;``None`` 保留为显式 null)。"""

        return {
            "backfill_version": self.backfill_version,
            "scope_checksum": scope_checksum(self.scope),
            "quality_profile": self.quality_profile,
            "datasets": [
                {
                    "dataset": item.dataset.value,
                    "source": item.source,
                    "adjustment": item.adjustment,
                    "start": item.start.isoformat() if item.start is not None else None,
                    "end": item.end.isoformat() if item.end is not None else None,
                    "symbols": sorted(item.symbols),
                    "publish": item.publish,
                }
                for item in self.ordered_datasets()
            ],
        }


def request_fingerprint(spec: BackfillRequestSpec) -> str:
    """规范化 spec 的 sha256(等价重试回同一请求的唯一依据)。

    scope 只取 #510 的 ``scope_checksum`` —— 范围对象自身的字段扩展不应
    让等价请求换 fingerprint。
    """

    payload = json.dumps(
        spec.canonical_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class BackfillGapSymbols(_Frozen):
    """缺标的的计数 + 有界预览(全量清单走明细接口)。"""

    count: int = 0
    preview: tuple[str, ...] = ()


class BackfillCoverage(_Frozen):
    """一段已完成/在途覆盖的来源记录。"""

    #: release / batch / request
    kind: Literal["release", "batch", "request"]
    id: str
    #: release 的 quality_status / request 的 status;batch 用 status。
    status: str | None = None
    ranges: tuple[DateRange, ...] = ()
    symbols: int | None = None


class BackfillPublishPreview(_Frozen):
    """新发布的身份预告(不可变资产,预检只预告不落库)。"""

    release_id: str
    version: str
    release_kind: str
    dataset_name: str


class BackfillItem(_Frozen):
    """一个数据集的补齐计划(item 独立成功/失败,默认 fail-fast)。"""

    dataset: CatalogDataset
    source: str
    adjustment: str
    action: BackfillAction
    start: date
    end: date
    publish: bool
    #: 空 = 全市场(按 scope 解析出的标的域整体补齐);逐标的单元在规模可控时
    #: 冻结完整标的列表,执行器与发布**只用冻结值**,不自行放宽。
    symbols: tuple[str, ...] = ()
    #: 标的域超过冻结上限时的过滤器(market / instrument_type / selection_limit),
    #: 子执行器按同一过滤器从 ``instruments`` 解析,发布面与请求面一致。
    universe_filters: dict[str, object] = Field(default_factory=dict)
    #: 执行器**只补这些区间**(冻结的缺口差集,不重跑整窗)。
    missing_ranges: tuple[DateRange, ...] = ()
    #: 缓存质量修复的标的子集(bars;来自既有发布的异常/重复计数)。
    repair_symbols: tuple[str, ...] = ()
    missing_symbols: BackfillGapSymbols = Field(default_factory=BackfillGapSymbols)
    covered_by: tuple[BackfillCoverage, ...] = ()
    estimated_requests: int = 0
    #: 是否需要先跑缓存质量修复(bars 且覆盖记录存在异常计数)。
    quality_repair: bool = False
    publish_preview: BackfillPublishPreview | None = None
    reused_request_ids: tuple[str, ...] = ()
    blockers: tuple[BackfillBlocker, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def has_gap(self) -> bool:
        return bool(self.missing_ranges)


class BackfillQuota(_Frozen):
    """上游请求配额状态(不足不直接拒绝,标 defer 由执行器退避重试)。"""

    source: str = "tushare"
    #: ok / defer / unknown(读不到预算文件时不阻塞,执行器仍会兜底)。
    status: Literal["ok", "defer", "unknown"] = "unknown"
    remaining_today: int | None = None
    requests_per_minute: int | None = None
    estimated_total: int = 0
    sufficient_today: bool = True
    days_needed: int = 1


class BackfillOverlap(_Frozen):
    """与既有补数请求的重叠(复用范围或全部覆盖)。"""

    request_id: str
    status: str
    #: ranges = 只复用部分区间 / all = 完全覆盖
    reuse: Literal["ranges", "all"]
    datasets: tuple[str, ...] = ()


class BackfillPublishImpact(_Frozen):
    """发布影响:新增几份发布、影响哪些策略、历史发布不变。"""

    new_releases: int = 0
    affected_specs: int = 0
    #: 恒 True —— 补数只产生新发布,既有发布只读(不接受「原地更新」理解)。
    immutable_existing: bool = True
    releases: tuple[BackfillPublishPreview, ...] = ()


class BackfillPlan(_Frozen):
    """预检结果(纯读产出的冻结快照)。"""

    request_fingerprint: str
    scope_checksum: str
    as_of: datetime
    items: tuple[BackfillItem, ...]
    quota: BackfillQuota = Field(default_factory=BackfillQuota)
    overlaps: tuple[BackfillOverlap, ...] = ()
    publish_impact: BackfillPublishImpact = Field(default_factory=BackfillPublishImpact)
    blockers: tuple[BackfillBlocker, ...] = ()
    warnings: tuple[str, ...] = ()
    backfill_version: str = BACKFILL_VERSION

    @property
    def executable_items(self) -> tuple[BackfillItem, ...]:
        """真正需要执行(有缺口且无 blocker)的项。"""

        return tuple(
            item
            for item in self.items
            if item.action
            in (
                BackfillAction.SYNC,
                BackfillAction.SYNC_PUBLISH,
                BackfillAction.REPAIR,
            )
            and not item.blockers
        )

    @property
    def blocked(self) -> bool:
        """请求级不可执行:任一 item 带 blocker 即整体 fail-closed。

        为什么整体阻塞而不是只跳过受阻 item:补数请求是**用户显式提交的一份
        计划**,静默少补一个数据集会让「补完了」变成假象。受阻时落 ``blocked``
        请求(需求登记,不建 job、可审计),按数据集拆请求即可先补可补的部分。
        """

        return bool(self.blockers)

    @property
    def is_empty(self) -> bool:
        """没有任何缺口(可直接复用已完成/在途覆盖)。"""

        return not self.executable_items

    def checksum(self) -> str:
        """计划内容的 sha256 —— 提交前 drift 检测的比对基准。

        只纳入「用户看到的决策事实」:每个 item 的缺口/动作/估算/发布预告、
        复用关系、全局 blocker 与配额状态;``as_of`` 与 warnings 不入账
        (同一分钟的两次预览不应因时间戳漂移判为 drift)。
        """

        payload = json.dumps(
            {
                "request_fingerprint": self.request_fingerprint,
                "scope_checksum": self.scope_checksum,
                "backfill_version": self.backfill_version,
                "quota_status": self.quota.status,
                "items": [
                    {
                        "dataset": item.dataset.value,
                        "source": item.source,
                        "adjustment": item.adjustment,
                        "action": item.action.value,
                        "start": item.start.isoformat(),
                        "end": item.end.isoformat(),
                        "publish": item.publish,
                        "symbols": sorted(item.symbols),
                        "universe_filters": {
                            key: item.universe_filters[key]
                            for key in sorted(item.universe_filters)
                        },
                        "repair_symbols": sorted(item.repair_symbols),
                        "missing_ranges": [
                            [start.isoformat(), end.isoformat()]
                            for start, end in item.missing_ranges
                        ],
                        "missing_symbols": item.missing_symbols.count,
                        "estimated_requests": item.estimated_requests,
                        "quality_repair": item.quality_repair,
                        "covered_by": [
                            {"kind": cov.kind, "id": cov.id} for cov in item.covered_by
                        ],
                        "reused_request_ids": list(item.reused_request_ids),
                        "blockers": [_blocker_key(blocker) for blocker in item.blockers],
                        "publish_preview": (
                            item.publish_preview.release_id
                            if item.publish_preview is not None
                            else None
                        ),
                    }
                    for item in self.items
                ],
                "overlaps": [
                    {"request_id": overlap.request_id, "reuse": overlap.reuse}
                    for overlap in self.overlaps
                ],
                "blockers": [_blocker_key(blocker) for blocker in self.blockers],
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        return hashlib.sha256(payload).hexdigest()


def _blocker_key(blocker: BackfillBlocker) -> dict[str, str | None]:
    """blocker 的稳定比较键(只取 code/dataset,消息文案可变)。"""

    return {
        "code": blocker.code,
        "dataset": blocker.dataset.value if blocker.dataset else None,
    }


class BackfillPlanError(ValueError):
    """预检前置条件不满足(契约层/日历/窗口越界);API 映射 422。"""

    def __init__(self, code: str, message: str, *, dataset: CatalogDataset | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.dataset = dataset


class BackfillPlanDriftError(RuntimeError):
    """提交前复算的 plan 与用户看到的 plan 不一致(API 映射 409)。"""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(
            "补数计划已变化(新发布 / 新在途请求 / 配额变化),请重新预览后再提交"
        )
        self.expected = expected
        self.actual = actual


def utcnow() -> datetime:
    """统一 UTC 时间源(便于测试注入)。"""

    return datetime.now(UTC)


def generate_backfill_request_id() -> str:
    """生成补数请求 ID(``BF-<16hex>``,与 ``BJ-`` / ``RR-`` 前缀互不混淆)。"""

    return f"BF-{uuid.uuid4().hex[:16].upper()}"


__all__ = [
    "BACKFILL_VERSION",
    "DONE_ITEM_STATUSES",
    "INFLIGHT_REQUEST_STATUSES",
    "SYMBOL_PREVIEW_LIMIT",
    "TERMINAL_REQUEST_STATUSES",
    "Adjustment",
    "BackfillAction",
    "BackfillBlocker",
    "BackfillCoverage",
    "BackfillDatasetSpec",
    "BackfillGapSymbols",
    "BackfillItem",
    "BackfillItemStatus",
    "BackfillOverlap",
    "BackfillPlan",
    "BackfillPlanDriftError",
    "BackfillPlanError",
    "BackfillPublishImpact",
    "BackfillPublishPreview",
    "BackfillQuota",
    "BackfillRequestSpec",
    "BackfillRequestStatus",
    "DateRange",
    "generate_backfill_request_id",
    "request_fingerprint",
    "scope_checksum",
    "utcnow",
]
