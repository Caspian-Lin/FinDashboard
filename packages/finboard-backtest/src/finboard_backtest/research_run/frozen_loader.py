"""冻结产物 → 组合流水线机械输入加载器(issue #143)。

把 ``ResearchRunManifest`` 冻结的 dataset_releases / factor_snapshots 引用加载成
``PortfolioDecisionInput`` 的**机械字段**(价格 / 执行元数据 / 候选池 / 特征 /
artifact 绑定),供 ``PortfolioPipelineAdapter`` 使用。

issue #187:多数据集联合消费。=== dataset_releases 按 kind 融合 ===
* ``bars`` release —— 行情 / 候选池 / 执行元数据(主发布,仍要求恰好一个);
* ``daily_metrics`` / ``financial_indicators`` release —— 研究数据观测
  (pb / 市值 / 换手 / ROE 等),按 ``available_at <= decision_at`` PIT 门控
  读取并映射为 ``FeatureValue``,与 ``factor_snapshots`` 的观测合并。
manifest 冻结多个 release 时,一个 bars 主发布 + 若干研究数据发布联合消费。

边界:**信号(``NormalizedSignal``)不在本加载器生成**。现有策略实现是事件驱动实盘
``Strategy``(消费 ``MarketDataEvent`` → ``ctx.submit_order``),没有「OHLCV → 批量
信号」的离线函数;每个策略的信号逻辑需要独立设计(属于策略实现工作,非队列迁移)。
因此本加载器产出 :class:`LoadedDecisionContext`(机械字段),由调用方(真实适配器
工厂)补充信号后再构造完整的 ``PortfolioDecisionInput``。本期 worker 端到端测试用
固定样本 :class:`~finboard_backtest.research_run.adapters.DecisionSequenceAdapter`,
不走本加载器。

复用的现成服务(无需新写 Parquet/DB 读取):
* ``FrozenReleaseProvider.fetch_point_in_time_bars`` —— PIT 门控行情读取
* ``FrozenReleaseProvider.fetch_daily_metrics`` / ``fetch_financial_indicators``
  —— 研究数据发布观测读取(issue #187)
* ``FeatureSnapshotRepository.get`` —— 因子快照反序列化(JSON-in-DB)
* ``ResearchDatasetRelease.instruments`` —— 候选元数据 / 执行规则

不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Protocol

import numpy as np
import structlog

from finboard_backtest.portfolio.contracts import AssetLotInfo
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunInterruptedError,
    ResearchRunManifest,
    UniverseCandidate,
)

if TYPE_CHECKING:
    import pyarrow as pa

    from finboard_backtest.factor_lab import PriceFeatureProcessPool
    from finboard_data.factor_lab import FeatureObservation, FeatureSnapshot
    from finboard_data.factors import FactorInputBatch, FactorInputRecord
    from finboard_data.releases import (
        FrozenReleaseProvider,
        PointInTimeBar,
        ReleasedInstrument,
    )
    from finboard_data.research import DailySecurityMetrics
    from finboard_shared.types import Market

logger = structlog.get_logger(__name__)


class ReleaseProviderFactory(Protocol):
    """按 release_id 构造 ``FrozenReleaseProvider`` 的工厂(注入点)。"""

    def __call__(self, release_id: str) -> FrozenReleaseProvider: ...


class FeatureSnapshotProvider(Protocol):
    """按 snapshot_id 读取 ``FeatureSnapshot`` 的回调(注入点)。"""

    async def __call__(self, snapshot_id: str) -> FeatureSnapshot | None: ...


class FactorSeriesRecordLike(Protocol):
    """加载器消费的因子序列视图(issue #360)。

    ``finboard_persistence.FactorSeriesRecord`` 结构性满足本协议(loader
    不直接依赖 persistence);``dates`` 为升序决策日,``values`` 为
    ``{date(ISO): {symbol: float|null}}`` 逐日截面。只读成员用 property
    声明(可变容器型别的协议匹配要求只读访问器)。
    """

    @property
    def series_id(self) -> str: ...

    @property
    def code_artifact(self) -> str: ...

    @property
    def release_id(self) -> str: ...

    @property
    def dates(self) -> tuple[date, ...]: ...

    @property
    def values(self) -> Mapping[str, Mapping[str, float | None]]: ...


class FactorSeriesProvider(Protocol):
    """按 series_id 读取因子序列工件的回调(注入点,issue #360)。"""

    async def __call__(self, series_id: str) -> FactorSeriesRecordLike | None: ...


#: 预计算段进度回调(issue #450):phase 文本 → None。实现方(适配器工厂,
#: 见 ``build_run_phase_reporter``)尽力而为写 job 的 phase 字段,失败不阻断
#: 加载;本模块只在预建循环内按节流调用,done/total 数值列不动(#308 口径)。
PrecomputeProgressReporter = Callable[[str], Awaitable[None]]
#: 预计算段的取消探针(#450 追续):每次进度打点时轮询 run status /
#: job cancel_requested,被取消即抛 ResearchRunInterruptedError(实现方
#: :func:`build_run_cancel_probe`,毫秒级只读查询)。
PrecomputeCancelProbe = Callable[[], Awaitable[None]]

#: 预计算进度帧节流(issue #450):逐标的完成计数每达到该步长上报一帧
#: (首帧与末帧强制)。全市场 5534 标的 x close/daily 两段 ≈ 每段 ~22 帧,
#: DB 写入量可忽略;不节流的逐标的写入既无必要也不可读。
_PRECOMPUTE_PROGRESS_STEP = 256


def _make_precompute_ticker(
    progress: PrecomputeProgressReporter | None,
    label: str,
    total: int,
    cancel_probe: PrecomputeCancelProbe | None = None,
) -> Callable[[], Awaitable[None]]:
    """构造逐标的完成打点器(issue #450):按节流上报预计算进度帧。

    每个标的构建完成后调用返回的打点器一次;``done`` 为 1、达到步长或等于
    ``total`` 时上报 ``research_run:decision_load precompute <label>
    <done>/<total>``(其余静默返回)。``progress`` 为 None 且无
    ``cancel_probe`` 或 total 为 0 时返回 no-op,零开销。上报异常一律吞掉
    —— 进度是纯可观测性,不改变预建结果与失败语义。

    issue #450 追续:``cancel_probe`` 非空时在每次上报点(步长节流,全市场
    ≈ 每 200ms 一次)先轮询取消/run 状态,被取消即抛
    :class:`ResearchRunInterruptedError` 中止预建——预计算段此前是取消
    检查的空白区(整段 3-5 分钟只报进度不查取消),取消信号要等首个分块
    边界才被看见。探针异常**不吞**(与进度上报相反)。
    """
    if (progress is None and cancel_probe is None) or total <= 0:

        async def _noop() -> None:
            return None

        return _noop

    done = 0

    async def _tick() -> None:
        nonlocal done
        done += 1
        if done != 1 and done != total and done % _PRECOMPUTE_PROGRESS_STEP != 0:
            return
        if cancel_probe is not None:
            await cancel_probe()
        if progress is not None:
            with contextlib.suppress(Exception):
                await progress(
                    f"research_run:decision_load precompute {label} {done}/{total}"
                )

    return _tick


#: 逐标的并发加载的信号量上限(实际磁盘读仍受 provider 内 ParquetCache
#: 信号量约束;这里限制的是同时在途的任务数与结果占用的峰值内存)。
_LOAD_CONCURRENCY = 8


def _declared_domain_instruments(
    manifest: ResearchRunManifest,
    instruments: Sequence[ReleasedInstrument],
) -> list[ReleasedInstrument]:
    """声明 ``explicit_symbols`` 时把评估域收窄为 explicit ∩ 发布标的(#254/#299)。

    close 矩阵预建与逐期机械字段加载共用本收窄:声明域之外的价格 /
    执行元数据 / 研究观测不会被任何下游消费(universe 过滤与信号求值都
    在声明域内,#254)。声明但发布中缺失的标的由 ``_apply_universe_filter``
    发具名 warning,此处不重复告警。
    """
    from finboard_backtest.strategy_spec.universe_precheck import explicit_symbol_domain

    narrowed, _missing = explicit_symbol_domain(
        manifest.strategy_spec.universe, instruments
    )
    if len(narrowed) != len(instruments):
        logger.debug(
            "frozen_loader.close_matrix_domain_narrowed",
            declared=len(manifest.strategy_spec.universe.explicit_symbols),
            domain_size=len(narrowed),
            release_size=len(instruments),
            message="close 矩阵与机械字段加载按 explicit_symbols 声明域收窄",
        )
    return narrowed

#: close 矩阵全区间读取使用的 decision_at 上界(发布区间内的 bar 全部可见;
#: available_at 由各市场收盘规则派生,不会超过发布末日)。
_PIT_UNBOUNDED = datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC)


def _wants_execution_open(manifest: ResearchRunManifest) -> bool:
    """执行价基是否为 open(issue #336):timing=next_open 时 True。

    next_open 语义 = 下一交易日开盘成交;next_close = 下一交易日收盘。
    """
    from finboard_backtest.strategy_spec.contracts import ExecutionTiming

    return (
        manifest.strategy_spec.execution_model.timing is ExecutionTiming.NEXT_OPEN
    )


def _end_of_day(at: datetime) -> datetime:
    """执行日的日终时点(同一时区):执行日 bar 在此刻必然可见(#336)。

    D1 bar 的 available_at 为本市场收盘规则时点(A 股 15:30),恒早于当日
    23:59;执行价读取用日终门控即可看到执行日 bar,同时不越过执行日
    (执行日次日及其后的 bar 仍不可见,不引入执行日之后的任何数据)。
    """
    return datetime.combine(at.date(), time(23, 59), tzinfo=at.tzinfo)


def _datetime_epoch_micros(value: datetime) -> int:
    """aware/naive datetime → epoch 微秒(整数精确,不经 float timestamp)。

    aware 即 UTC 瞬时(naive/aware 互比由查询方按构建期记忆的时区属性
    前置拒绝,保持旧「直接比较」的 TypeError 语义,#439)。
    """
    epoch = datetime(1970, 1, 1, tzinfo=UTC) if value.tzinfo else datetime(1970, 1, 1)
    delta = value - epoch
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


@dataclass(frozen=True, slots=True)
class SymbolCloseHistory:
    """单标的冻结全区间 close 历史(close 矩阵切片底座,issue #287)。

    三个序列都按 bar 时间升序且构建时校验非递减。内部表示为原生数组
    (#439,替代 Python 对象元组:每 (天, 标的) 的 datetime/date/装箱 float
    ≈150B 降到 int64/float64 ≈24B,全市场 5534 标的 x ~2800 天从 ~2.5GB
    降到 ~400MB):``available_at_us`` 是 provider 的 PIT 门控键(epoch 微秒,
    aware 即 UTC 瞬时),``date_days`` 是逐期读取原有的日期上界过滤键
    (epoch 天数)。两个键的「可见集合」都是时间升序前缀,``visible_index``
    对两键各做一次 ``searchsorted(side="right")`` 取较小边界,与「逐期
    PIT 过滤后取末根 / 全序列」逐值等价(bisect_right 语义)。

    公开四方法(:meth:`visible_index` / :meth:`close_at` / :meth:`open_at` /
    :meth:`series_until`)签名与返回类型与元组表示时代逐值一致;datetime
    只在查询入参 / 返回值边界按需转换,不在构建期物化整列 Python 对象。
    """

    #: epoch 微秒 int64(单调不减;与 ``date_days`` 同长)
    available_at_us: np.ndarray
    #: epoch 天数 int64
    date_days: np.ndarray
    #: float64
    closes: np.ndarray
    #: available_at 序列的时区属性(取构建期首元素;查询 as_of 的
    #: naive/aware 与之不匹配时保持旧直接比较的 TypeError)
    available_tz_aware: bool = False
    #: available_at 的时区(构建期首元素原样保留;末根观测时点按需重建时
    #: 保证与列式路径同一 instant + 同一 wall-clock 表示,#450 追续)
    available_tz: tzinfo | None = None
    #: 可选携带的 open 平行序列(issue #336):仅 timing=next_open 的 run 在
    #: 矩阵预建时附带,与 close 严格同长;``None`` 表示未携带(close-only
    #: 矩阵),open 查询由调用方回退逐期对象路径读取。
    opens: np.ndarray | None = None

    @classmethod
    def from_sequences(
        cls,
        *,
        available_at: Sequence[datetime],
        dates: Sequence[date],
        closes: Sequence[float] | np.ndarray,
        opens: Sequence[float] | np.ndarray | None = None,
    ) -> SymbolCloseHistory | None:
        """从 Python 序列构建(一次性转原生数组;int64 上校验非递减)。

        ``available_at`` / ``dates`` 任一序列出现回退(异常数据,非递减被
        破坏)时返回 ``None``:前缀切片不再与逐期过滤等价,调用方对该标的
        回退逐期读取(对象路径与列式路径同一防御语义,#439)。空序列合法
        (无可见 bar 语义)。
        """
        available_us = np.fromiter(
            (_datetime_epoch_micros(value) for value in available_at),
            dtype=np.int64,
            count=len(available_at),
        )
        day_numbers = np.fromiter(
            (day.toordinal() - _EPOCH_ORDINAL for day in dates),
            dtype=np.int64,
            count=len(dates),
        )
        if (np.diff(available_us) < 0).any() or (np.diff(day_numbers) < 0).any():
            return None
        tz_aware = bool(available_at[0].tzinfo) if available_at else False
        return cls(
            available_at_us=available_us,
            date_days=day_numbers,
            closes=np.asarray(closes, dtype=np.float64),
            available_tz_aware=tz_aware,
            available_tz=available_at[0].tzinfo if available_at else None,
            opens=None if opens is None else np.asarray(opens, dtype=np.float64),
        )

    def available_at_at(self, index: int) -> datetime:
        """重建 ``index`` 处 bar 的 available_at(与构建期值逐值相等)。

        epoch 微秒整数精确逆变换(``_datetime_epoch_micros`` 的逆):aware
        序列从 UTC 锚点出发再 astimezone 到保留时区(同一 instant + 同一
        wall-clock 表示);naive 序列按其「即 UTC」锚定语义原样还原。
        供矩阵切片路径构造特征观测的观测时点(#450 追续)。
        """
        micros = int(self.available_at_us[index])
        tz = self.available_tz
        if tz is None:
            return datetime(1970, 1, 1) + timedelta(microseconds=micros)
        return (
            datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=micros)
        ).astimezone(tz)

    def visible_index(self, as_of: datetime) -> int:
        """``as_of`` 时点可见最后一根的索引;-1 表示无可见 bar。"""
        if self.available_at_us.size and (
            (as_of.tzinfo is not None) != self.available_tz_aware
        ):
            raise TypeError("can't compare offset-naive and offset-aware datetimes")
        index = min(
            int(np.searchsorted(self.available_at_us, _datetime_epoch_micros(as_of), side="right")),
            int(
                np.searchsorted(
                    self.date_days,
                    as_of.date().toordinal() - _EPOCH_ORDINAL,
                    side="right",
                )
            ),
        )
        return index - 1

    def close_at(self, as_of: datetime) -> float | None:
        """``as_of`` 时点可见的最新 close;无可见 bar 时为 ``None``。"""
        index = self.visible_index(as_of)
        if index < 0:
            return None
        return float(self.closes[index])

    def open_at(self, as_of: datetime) -> float | None:
        """``as_of`` 时点可见最新 bar 的 open(issue #336 执行价基)。

        矩阵未携带 opens 时返回 ``None``,调用方回退逐期对象路径;无可见
        bar 同样返回 ``None``。
        """
        if self.opens is None:
            return None
        index = self.visible_index(as_of)
        if index < 0:
            return None
        return float(self.opens[index])

    def series_until(self, as_of: datetime) -> list[float]:
        """``as_of`` 时点可见的完整 close 序列(时间升序;可能为空)。"""
        index = self.visible_index(as_of)
        if index < 0:
            return []
        return [float(value) for value in self.closes[: index + 1]]


@dataclass(frozen=True, slots=True)
class SymbolDailyMetricsHistory:
    """单标的全部决策期「最新可见 daily_metrics 行」预计算矩阵(#438)。

    每标的对冻结发布做**一次**列式读取,在 numpy 里对 run 冻结的全部
    decision_at 各定位「PIT 最新可见行」(选择语义与逐期
    ``fetch_daily_metrics_columns`` + ``sorted()[-1]`` 逐值等值,见
    :func:`finboard_data.releases._daily_metrics_latest_visible_indices`);
    payload 以紧凑数组常驻(float64 + bool 掩码,int 列独立,预算
    全市场 5534 x 75 期 x 16 列 < 200MB)。``available_at`` / ``source``
    保留选中行的原串/原对象(仅 P 期规模),``record`` 在消费期按需重建
    行 dict 并走 :func:`_daily_metrics_from_release_row`——领域对象的
    Decimal 强转语义与旧路径逐值一致,只是从「每期全历史」降到「每期一行」。
    """

    #: float payload 列名(发布列 ∷ ``_DAILY_METRICS_FLOAT_PAYLOAD_FIELDS`` 序)
    columns: tuple[str, ...]
    #: (n_periods, n_cols) float64;无效格填 0,以 ``valid`` 掩码为准
    values: np.ndarray
    #: (n_periods, n_cols) bool;False → 重建行时该列为 None
    valid: np.ndarray
    #: (n_periods,) int64(int payload 单独存储,避免 float str 化变义)
    limit_status: np.ndarray
    #: (n_periods,) bool
    limit_status_valid: np.ndarray
    #: (n_periods,) int32,选中行 trade_date 的 epoch 天数(无选中行占位 0)
    trade_date_days: np.ndarray
    #: 选中行 available_at 原值(string 列即原 ISO 串);None = 该期无可见行
    available_at: tuple[object, ...]
    #: 选中行 source 原值(string 或 None)
    source: tuple[object, ...]

    @property
    def n_periods(self) -> int:
        return len(self.available_at)

    @property
    def nbytes(self) -> int:
        """数组负载字节数(不含 str/None 元组;规模断言与容量规划用)。"""
        return int(
            self.values.nbytes
            + self.valid.nbytes
            + self.limit_status.nbytes
            + self.limit_status_valid.nbytes
            + self.trade_date_days.nbytes
        )

    def latest_row(self, period: int) -> dict[str, object] | None:
        """重建该期选中行的发布行 dict(与旧 ``to_pylist()[0]`` 逐值等价)。

        string/数值/日期逐列还原为旧对象路径行 dict 的同一形态:float 列
        ``float(arr[i, j])``(与 Arrow double ``to_pylist`` 同值)、null 掩码
        → None、trade_date 由 epoch 天数精确重建 ``date``。无可见行返回 None。
        """
        available = self.available_at[period]
        if available is None:
            return None
        row: dict[str, object] = {
            "available_at": available,
            "trade_date": _EPOCH_DATE + timedelta(days=int(self.trade_date_days[period])),
            "source": self.source[period],
            "limit_status": (
                int(self.limit_status[period]) if self.limit_status_valid[period] else None
            ),
        }
        for j, name in enumerate(self.columns):
            row[name] = float(self.values[period, j]) if self.valid[period, j] else None
        return row

    def record(self, period: int, *, symbol: str) -> DailySecurityMetrics | None:
        """该期选中行 → 领域记录(Decimal 语义与旧路径一致;无可见行为 None)。"""
        from finboard_data.releases import _daily_metrics_from_release_row

        row = self.latest_row(period)
        if row is None:
            return None
        return _daily_metrics_from_release_row(row, symbol=symbol)


@dataclass(frozen=True, slots=True)
class DailyMetricsPrecompute:
    """一个 daily_metrics 发布在全部冻结决策期上的预计算(#438)。

    ``histories`` 覆盖 run 候选池全体:标的值 ``None`` = 预建时
    ``ReleaseCapabilityError``(标的不在该研究发布)→ 消费期按 #252 missing
    语义报告,与逐期路径一致。
    """

    release_id: str
    #: decision_at → 期序(冻结决策日全集;消费期按 decision_at 查期)
    period_index: dict[datetime, int]
    #: symbol → 预计算历史(None = 标的不在该发布,#252 missing)
    histories: dict[str, SymbolDailyMetricsHistory | None]


@dataclass(slots=True)
class LoadedDecisionContext:
    """单个决策时点的机械加载结果(不含信号)。

    调用方需补充 ``signals: tuple[NormalizedSignal, ...]``(信号标的必须是
    ``included_candidates`` 的子集),再构造完整 ``PortfolioDecisionInput``。
    """

    business_date: date
    decision_at: datetime
    execution_at: datetime
    candidates: tuple[UniverseCandidate, ...]
    features: tuple[FeatureValue, ...]
    prices: dict[str, float]
    execution_prices: dict[str, float]
    lot_info: dict[str, AssetLotInfo]
    input_artifact_ids: tuple[str, ...]
    included_symbols: tuple[str, ...] = field(default_factory=tuple)
    # #252:研究数据发布缺标的的容忍语义(对齐 factor_lab #212)——
    # {release_id: 缺失标的代码}(只列本次执行候选中发布不含的标的)。
    # 缺失标的的研究数据因子值为 null(不回退外部数据源),不 fail-closed。
    research_release_missing_symbols: dict[str, tuple[str, ...]] = field(
        default_factory=dict
    )
    # issue #396:决策日 / 执行日全天停牌标的(PIT 门控)。空集 = 无停牌
    # 信息(research_suspensions 未同步或未接线),消费端行为与历史一致。
    decision_suspended: frozenset[str] = field(default_factory=frozenset)
    execution_suspended: frozenset[str] = field(default_factory=frozenset)


class SuspensionView:
    """停复牌记录的进程内查询视图(issue #396)。

    只收录**全天停牌**(``suspend_kind="suspension_day"``)记录:盘中停牌
    当日仍可撮合,不计入不可撮合口径;复牌记录不单独消费。查询按 PIT 门控
    (记录 ``available_at <= visible_at``;PIT=当日,09:30 上海后可见)。
    无停牌数据时传 ``None``(而非空视图),保持「信息缺失行为不变」。
    """

    def __init__(self, records: Sequence[tuple[date, str, datetime]]) -> None:
        by_day: dict[date, dict[str, datetime]] = {}
        for trade_date, symbol, available_at in records:
            by_day.setdefault(trade_date, {})[symbol] = available_at
        self._by_day = by_day

    def suspended(self, day: date, *, visible_at: datetime) -> frozenset[str]:
        """``day`` 全天停牌且在 ``visible_at`` 时点已可见的标的集。"""
        rows = self._by_day.get(day)
        if not rows:
            return frozenset()
        return frozenset(
            symbol for symbol, available_at in rows.items() if available_at <= visible_at
        )


@dataclass(slots=True)
class FrozenInputLoader:
    """从冻结产物加载组合流水线机械输入。

    本类只做「读 + 映射」,不生成信号、不下单、不修改持仓。真实策略信号引擎接入
    后,适配器工厂应:``context = await loader.load_context(manifest, decision_at)``
    → 用策略引擎基于 ``context.features`` / ``context.prices`` 生成信号 → 组装
    ``PortfolioDecisionInput``。

    issue #287:loader 实例(= 单 run 装载)内维护 close 矩阵 —— 首个决策对
    全部候选各做一次发布全区间 PIT 读取,其后逐期切片,消除「读全历史取最后
    一个 close」的 2N 次重复全文件读。矩阵只对真实 ``FrozenReleaseProvider``
    启用;其它实现(测试 stub 等)与切不了片的标的回退逐期读取。
    """

    release_provider_factory: ReleaseProviderFactory
    snapshot_provider: FeatureSnapshotProvider
    # issue #360:因子序列工件读取回调;None 或 manifest 未声明 series 时
    # 加载器走纯快照路径(历史行为不变)。
    series_provider: FactorSeriesProvider | None = None
    # issue #396:停复牌查询视图(research_suspensions,PIT 门控);None =
    # 无停牌信息,加载结果不含停牌标注,消费端行为与历史一致。
    suspension_view: SuspensionView | None = None
    # symbol → close 历史;``None`` 表示该标的不可安全切片(非单调数据),
    # 逐期回退读取。空映射 = 矩阵未启用(非真实 provider)。
    _close_histories: dict[str, SymbolCloseHistory | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # 矩阵构建只尝试一次(候选集逐期不变);失败不缓存,逐期回退。
    _close_histories_built: bool = field(default=False, init=False, repr=False)
    # #438:daily_metrics 发布 → run 级预计算(release_id 键;多研究发布并存)。
    # 空映射 = 未预建(未调用 ensure / 无真实 provider),消费走逐期路径。
    _daily_precompute: dict[str, DailyMetricsPrecompute] = field(
        default_factory=dict, init=False, repr=False
    )
    # 预建只尝试一次(决策日全集冻结,候选域逐期不变)。
    _daily_precompute_built: bool = field(default=False, init=False, repr=False)

    @property
    def close_histories(self) -> Mapping[str, SymbolCloseHistory | None]:
        """已构建的 close 矩阵(供价格序列等复用切片;未启用时为空)。"""
        return self._close_histories

    async def load_context(
        self,
        manifest: ResearchRunManifest,
        *,
        decision_at: datetime,
        execution_at: datetime,
    ) -> LoadedDecisionContext:
        """加载单个决策时点的机械字段。

        * 按 kind 融合 manifest.dataset_releases(issue #187):bars 主发布提供
          候选池 / 价格 / 执行元数据;daily_metrics / financial_indicators 发布
          提供研究数据观测(PIT 门控),与 factor_snapshots 观测合并。
        * 按 ``decision_at`` 做 PIT 门控取决策日 close;执行价按执行假设读取
          (issue #336):timing=next_open → 执行日 bar 的 **open**,
          next_close → 执行日 bar 的 close,门控放宽到执行日日终(执行行为
          发生在执行日内,不构成未来函数);``execution_at`` 为成交时间戳
          (next_open = 09:30 / next_close = 15:00,由调用方保证在 decision_at
          之后的下一交易日)。
        * 加载 manifest.factor_snapshots 的特征观测并映射为 ``FeatureValue``。
        """
        if not manifest.dataset_releases:
            raise ValueError("manifest 必须冻结至少一个数据发布")
        if execution_at <= decision_at:
            raise ValueError("execution_at 必须晚于 decision_at")
        release_ref = self._bars_release_ref(manifest)
        provider = self.release_provider_factory(release_ref.artifact_id)
        release = provider.release
        included_candidates, lot_info_by_symbol = _build_candidates_and_lots(
            _declared_domain_instruments(manifest, list(release.instruments))
        )
        prices = await self._load_close_prices(
            provider, included_candidates, decision_at
        )
        # issue #336:执行价按执行假设读取(next_open → 执行日 open;
        # next_close → 执行日 close),PIT 门控放宽到执行日日终——此前按
        # 15:00 门控使执行日 bar(available_at = 15:30)不可见,成交价退化
        # 为决策日收盘,与 timing 声明不符。
        execution_prices = await self._load_execution_prices(
            provider,
            included_candidates,
            execution_at=execution_at,
            want_open=_wants_execution_open(manifest),
        )
        # issue #360 双轨:声明 series 时 u_ 因子观测优先从 series.values 取
        # (按决策日索引),同因子的快照观测让位;未声明走纯快照路径。
        series_features: tuple[FeatureValue, ...] = ()
        series_covered: frozenset[str] = frozenset()
        if manifest.factor_series and self.series_provider is not None:
            series_features, series_covered = await self._load_series_features(
                manifest.factor_series, decision_at
            )
        features = await self._load_features(
            manifest.factor_snapshots, decision_at, exclude_features=series_covered
        )
        features = (*series_features, *features)
        research_features, research_missing = await self._load_research_features(
            manifest, included_candidates, decision_at
        )
        features = (*features, *research_features)
        if research_missing:
            # #252:缺标的具名可见(对齐 factor_lab missing_in_research 语义);
            # bars 主发布缺标的仍由 _load_close_prices / 候选构建 fail-closed。
            for release_id, missing_symbols in research_missing.items():
                logger.warning(
                    "research_release_missing_symbols",
                    release_id=release_id,
                    missing_count=len(missing_symbols),
                    missing_symbols=tuple(missing_symbols[:20]),
                    decision_at=decision_at.isoformat(),
                )
        artifact_ids = _build_artifact_ids(manifest)
        # issue #396:决策日 / 执行日全天停牌标的(PIT 门控;决策日按决策
        # 时点可见,执行日按执行日日终可见——与执行价同一放宽口径)。
        decision_suspended = (
            self.suspension_view.suspended(
                decision_at.date(), visible_at=decision_at
            )
            if self.suspension_view is not None
            else frozenset()
        )
        execution_suspended = (
            self.suspension_view.suspended(
                execution_at.date(), visible_at=execution_at
            )
            if self.suspension_view is not None
            else frozenset()
        )
        return LoadedDecisionContext(
            business_date=decision_at.date(),
            decision_at=decision_at,
            execution_at=execution_at,
            candidates=included_candidates,
            features=features,
            prices=prices,
            execution_prices=execution_prices,
            lot_info=lot_info_by_symbol,
            input_artifact_ids=artifact_ids,
            included_symbols=tuple(item.symbol for item in included_candidates),
            research_release_missing_symbols=research_missing,
            decision_suspended=decision_suspended,
            execution_suspended=execution_suspended,
        )

    async def ensure_close_histories(
        self,
        manifest: ResearchRunManifest,
        *,
        process_pool: PriceFeatureProcessPool | None = None,
        progress: PrecomputeProgressReporter | None = None,
        cancel_probe: PrecomputeCancelProbe | None = None,
    ) -> None:
        """一次性预建 close 矩阵(幂等;issue #288 分块并行加载的前置步骤)。

        矩阵是 loader 实例上的惰性进程内缓存(#287):``build_decision_load_
        contexts`` 改分块并行后,若仍由首个 ``load_context`` 惰性首建,同分块的
        其它期会看到「已开建但未完成」的空矩阵而全部回退逐期读取(结果仍等值,
        但矩阵收益清零且重复读盘)。加载前显式调用本方法把首建收敛到单一顺序
        点。非真实 provider(矩阵不启用)与已建情形零成本返回。

        issue #299:声明 ``explicit_symbols`` 时预建范围收窄到声明域
        (∩ 发布标的),不再为全发布(可能数千只)付一次性全量读取成本;
        未声明时行为不变(全发布预建)。

        issue #301:``process_pool`` 非空(已 start 的常驻池)时预建读取分发到
        进程池——worker 内完成 parquet 解码 + 列式转换,主进程只收列式数据;
        池未启动 / 启动失败 / 任务损坏一律具名降级为进程内线程路径(结果逐值
        一致),不改变池自身状态(特征路径的降级语义由 #288 自行处理)。

        issue #450:``progress`` 非空时逐标的完成按节流上报预计算进度帧
        (只写 phase 文本,数值列不动);None = 无进度上报(逐期惰性路径)。
        """
        if self._close_histories_built:
            return
        release_ref = self._bars_release_ref(manifest)
        provider = self.release_provider_factory(release_ref.artifact_id)
        included_candidates, _ = _build_candidates_and_lots(
            _declared_domain_instruments(manifest, list(provider.release.instruments))
        )
        # issue #336:next_open 执行价基需要 open 平行序列,矩阵预建时一并
        # 读取(next_close 不付这份数据成本)。
        include_open = _wants_execution_open(manifest)
        if process_pool is not None and not process_pool.broken:
            built = await _load_close_histories_via_pool(
                process_pool,
                provider,
                included_candidates,
                include_open=include_open,
                progress=progress,
                cancel_probe=cancel_probe,
            )
            if built is not None:
                self._close_histories_built = True
                self._close_histories.update(built)
                return
        await self._ensure_close_histories(
            provider,
            included_candidates,
            include_open=include_open,
            progress=progress,
            cancel_probe=cancel_probe,
        )

    async def _ensure_close_histories(
        self,
        provider: FrozenReleaseProvider,
        candidates: Sequence[UniverseCandidate],
        *,
        include_open: bool = False,
        progress: PrecomputeProgressReporter | None = None,
        cancel_probe: PrecomputeCancelProbe | None = None,
    ) -> None:
        """构建 close 矩阵(只尝试一次;失败不缓存,逐期回退读取)。"""
        if self._close_histories_built:
            return
        self._close_histories_built = True
        self._close_histories.update(
            await _load_close_histories(
                provider,
                candidates,
                include_open=include_open,
                progress=progress,
                cancel_probe=cancel_probe,
            )
        )

    async def ensure_daily_metrics_histories(
        self,
        manifest: ResearchRunManifest,
        decision_ats: Sequence[datetime],
        *,
        progress: PrecomputeProgressReporter | None = None,
        cancel_probe: PrecomputeCancelProbe | None = None,
    ) -> None:
        """一次性预建全部 daily_metrics 发布的研究观测矩阵(幂等,#438)。

        与 :meth:`ensure_close_histories` 同类:加载前单一顺序点预建。决策日
        全集(``build_decision_load_contexts`` 在分块前已冻结)一次传入,每
        标的**一次**列式读取覆盖全部决策期——此前每 (标的 x 决策期) 独立
        ``pq.read_table`` 整文件,同一标的文件被重复读 P 期(全市场 5534 x
        75 期 ≈ 41.5 万次/run),且门控逐行 ``fromisoformat`` 持 GIL。预建后
        逐期消费查 :class:`SymbolDailyMetricsHistory` 矩阵,零 IO 零逐行解析。

        评估域与 close 矩阵同口径(#299/#380):候选池 = 发布可交易域,
        ``explicit_symbols`` 声明时收窄到声明 ∩ 发布。仅对真实列式 provider
        (``fetch_daily_metrics_columns`` getattr 探测,#371 同构)启用;stub
        与对象路径 provider 不预建,逐期路径行为不变。读取失败(除标的不在
        发布的 ``ReleaseCapabilityError`` → #252 missing 外)直接抛出,与
        逐期路径同 fail-closed。

        issue #450:``progress`` 非空时逐标的完成按节流上报预计算进度帧
        (计数跨发布累积,帧内 done/total 覆盖全部 daily_metrics 发布)。
        ``cancel_probe`` 非空时逐打点轮询取消(#450 追续,预计算段取消
        检查空白区补齐)。
        """
        if self._daily_precompute_built or not decision_ats:
            return
        self._daily_precompute_built = True
        from finboard_data.releases import ReleaseDatasetKind

        bars_ref = self._bars_release_ref(manifest)
        bars_provider = self.release_provider_factory(bars_ref.artifact_id)
        candidates, _ = _build_candidates_and_lots(
            _declared_domain_instruments(manifest, list(bars_provider.release.instruments))
        )
        ordered = sorted(decision_ats)
        daily_releases = [
            release_ref
            for release_ref in manifest.dataset_releases
            if self.release_provider_factory(
                release_ref.artifact_id
            ).release.dataset_kind
            is ReleaseDatasetKind.DAILY_METRICS
        ]
        # 进度计数跨发布累积(#450):帧内 done/total 覆盖本 ensure 全部
        # 标的 x 发布,不随发布切换回退。
        tick = _make_precompute_ticker(
            progress,
            "daily",
            len(candidates) * len(daily_releases),
            cancel_probe,
        )
        for release_ref in daily_releases:
            provider = self.release_provider_factory(release_ref.artifact_id)
            if getattr(provider, "fetch_daily_metrics_columns", None) is None:
                continue
            self._daily_precompute[release_ref.artifact_id] = (
                await _build_daily_metrics_precompute(
                    provider,
                    candidates,
                    ordered,
                    release_ref.artifact_id,
                    tick=tick,
                )
            )

    async def _load_execution_prices(
        self,
        provider: FrozenReleaseProvider,
        candidates: Sequence[UniverseCandidate],
        *,
        execution_at: datetime,
        want_open: bool,
    ) -> dict[str, float]:
        """按执行假设读取各标的的执行价(issue #336)。

        * ``want_open``(timing=next_open)—— 执行日 bar 的 **open**;
        * 否则(timing=next_close)—— 执行日 bar 的 **close**;
        * PIT 门控 as_of 放宽到执行日**日终**:执行行为本身发生在执行日内,
          读取执行日 bar 不构成未来函数(决策输入的 PIT 门控仍在
          ``decision_at``,与本读取无关)。此前实现按 15:00 门控,执行日 bar
          (available_at = 15:30)不可见,成交价退化为**决策日收盘**。

        矩阵未携带 opens / 无可见 bar / 不可切片的标的回退逐期对象路径读取;
        执行日无 bar(停牌 / 数据缺口)的标的沿用「最后可见 bar」价格(与
        close 路径同一降级语义)。
        """
        read_as_of = _end_of_day(execution_at)
        await self._ensure_close_histories(provider, candidates)
        prices: dict[str, float] = {}
        fallback: list[UniverseCandidate] = []
        for candidate in candidates:
            history = self._close_histories.get(candidate.symbol)
            if history is None:
                fallback.append(candidate)
                continue
            value = (
                history.open_at(read_as_of)
                if want_open
                else history.close_at(read_as_of)
            )
            if value is not None:
                prices[candidate.symbol] = value
            else:
                # close-only 矩阵遇到 open 请求 → 该标的回退对象路径读取。
                fallback.append(candidate)
        if fallback:
            prices.update(
                await _load_execution_prices_from_bars(
                    provider, fallback, read_as_of, want_open=want_open
                )
            )
        return prices

    async def _load_close_prices(
        self,
        provider: FrozenReleaseProvider,
        candidates: Sequence[UniverseCandidate],
        as_of: datetime,
    ) -> dict[str, float]:
        """PIT 门控读取各标的在 ``as_of`` 时点可见的最新 close 价(issue #287)。

        close 矩阵可用时直接前缀切片(与逐期 PIT 过滤读取逐值等价,不再读盘);
        矩阵未覆盖 / 不可切片的标的回退 :func:`_load_close_prices` 逐期读取。
        """
        await self._ensure_close_histories(provider, candidates)
        prices: dict[str, float] = {}
        fallback: list[UniverseCandidate] = []
        for candidate in candidates:
            history = self._close_histories.get(candidate.symbol)
            if history is None:
                fallback.append(candidate)
                continue
            value = history.close_at(as_of)
            if value is not None:
                prices[candidate.symbol] = value
        if fallback:
            prices.update(await _load_close_prices(provider, fallback, as_of))
        return prices

    def _bars_release_ref(self, manifest: ResearchRunManifest) -> FrozenArtifactRef:
        """取 bars 主发布引用;缺少或存在多个时 fail-closed。"""
        from finboard_data.releases import ReleaseDatasetKind

        bars_refs = []
        for release_ref in manifest.dataset_releases:
            provider = self.release_provider_factory(release_ref.artifact_id)
            if provider.release.dataset_kind is ReleaseDatasetKind.BARS:
                bars_refs.append(release_ref)
        if len(bars_refs) != 1:
            raise ValueError(
                "联合发布必须恰好包含一个 bars 主发布(行情/候选池来源),"
                "实际: " + ",".join(ref.artifact_id for ref in manifest.dataset_releases)
            )
        return bars_refs[0]

    async def _load_research_features(
        self,
        manifest: ResearchRunManifest,
        candidates: Sequence[UniverseCandidate],
        decision_at: datetime,
    ) -> tuple[tuple[FeatureValue, ...], dict[str, tuple[str, ...]]]:
        """从研究数据发布(daily_metrics / financial_indicators)加载 PIT 观测。

        issue #187:把冻结研究数据映射为因子值(universe 过滤需要的
        ``feature_id`` 与 ``extract_factor_matrix`` 输出一致),与
        factor_snapshots 的观测共同构成决策时点的特征。

        #252:研究发布缺标的按 factor_lab(#212)容忍语义处理——该标的因子
        观测为 null 并计入返回的 missing 映射,不 fail-closed(研究发布只提供
        因子观测,候选池已落在 bars 主发布;一只缺失不应炸整条 run)。
        """
        from finboard_data.releases import ReleaseDatasetKind

        values: list[FeatureValue] = []
        missing_by_release: dict[str, tuple[str, ...]] = {}
        for release_ref in manifest.dataset_releases:
            provider = self.release_provider_factory(release_ref.artifact_id)
            kind = provider.release.dataset_kind
            if kind is ReleaseDatasetKind.DAILY_METRICS:
                metrics, missing = await _load_daily_metrics_features(
                    provider,
                    candidates,
                    decision_at,
                    release_ref.artifact_id,
                    precompute=self._daily_precompute.get(release_ref.artifact_id),
                )
                values.extend(metrics)
            elif kind is ReleaseDatasetKind.FINANCIAL_INDICATORS:
                financials, missing = await _load_financial_features(
                    provider, candidates, decision_at, release_ref.artifact_id
                )
                values.extend(financials)
            else:
                continue
            if missing:
                missing_by_release[release_ref.artifact_id] = missing
        return tuple(values), missing_by_release

    async def _load_features(
        self,
        snapshots: Sequence[FrozenArtifactRef],
        decision_at: datetime,
        *,
        exclude_features: Collection[str] = frozenset(),
    ) -> tuple[FeatureValue, ...]:
        """加载所有 factor_snapshot 的观测并按 ``available_at <= decision_at`` 过滤。

        ``exclude_features``(issue #360):被声明因子序列覆盖的因子名集合 ——
        序列路径优先,同因子的快照观测让位(避免两条路径各供一份观测)。
        """
        values: list[FeatureValue] = []
        for snapshot_ref in snapshots:
            snapshot = await self.snapshot_provider(snapshot_ref.artifact_id)
            if snapshot is None:
                continue
            for obs in snapshot.observations:
                if obs.available_at > decision_at:
                    continue
                if obs.feature_name in exclude_features:
                    continue
                values.append(
                    FeatureValue(
                        symbol=obs.symbol,
                        feature_id=obs.feature_name,
                        value=float(obs.value),
                        source_artifact_ids=(snapshot_ref.artifact_id,),
                        available_at=obs.available_at,
                    )
                )
        return tuple(values)

    async def _load_series_features(
        self,
        series: Sequence[FrozenArtifactRef],
        decision_at: datetime,
    ) -> tuple[tuple[FeatureValue, ...], frozenset[str]]:
        """加载因子序列工件在 ``decision_at`` 决策日的截面(issue #360)。

        返回 ``(观测, 覆盖的因子名集合)``;序列未覆盖该决策日时发具名
        warning(``research_run.factor_series_date_missing``)并跳过 —— 因子
        观测缺失沿既有 missing 语义(null / 下游 warning),不 fail-closed。
        逐序列 ``available_at = decision_at``:序列由前缀不变性审计保证
        「决策日观测只用决策日之前的数据」,该时点可见性是审计结论。

        因子名按序列 ``kind`` 派生(issue #398):``predefined_factor`` →
        ``p_<artifact>``,其余(用户因子)→ ``u_<artifact>``(语义零变化)。
        """
        from finboard_data.factor_lab import series_factor_name

        values: list[FeatureValue] = []
        covered: set[str] = set()
        for series_ref in series:
            assert self.series_provider is not None  # 调用点已判空
            record = await self.series_provider(series_ref.artifact_id)
            if record is None:
                raise ValueError(f"因子序列缺失: {series_ref.artifact_id}")
            factor_name = series_factor_name(
                str(getattr(record, "kind", "factor")), str(record.code_artifact)
            )
            covered.add(factor_name)
            day_values = record.values.get(decision_at.date().isoformat())
            if day_values is None:
                logger.warning(
                    "research_run.factor_series_date_missing",
                    series_id=series_ref.artifact_id,
                    factor=factor_name,
                    decision_at=decision_at.isoformat(),
                    window=f"{record.dates[0]}~{record.dates[-1]}" if record.dates else "empty",
                )
                continue
            values.extend(
                series_feature_values(
                    record, decision_at, factor_name=factor_name
                )
            )
        return tuple(values), frozenset(covered)


_EPOCH_DATE = date(1970, 1, 1)
_EPOCH_ORDINAL = _EPOCH_DATE.toordinal()


def _latest_daily_metrics_row(table: pa.Table) -> dict[str, object] | None:
    """列式表中选 ``available_at`` 最大的行并物化为单个 dict(issue #371 同构)。

    与对象路径 ``sorted(records, key=available_at)[-1]`` 逐值等值:Python 排序
    稳定,available_at 并列时取原读取顺序中最后一条——available_at 整列一次
    cast 成 int64 微秒(#438,aware 即 UTC 瞬时,与旧逐行 ``fromisoformat``
    解析比较同语义)后用 numpy 定位最大值末位下标,slice 1 行物化,零逐行
    Python。后续 ``_daily_metrics_from_release_row`` 的 Decimal 强转成本只付
    选中这一行。
    """
    import pyarrow as pa

    if table.num_rows == 0:
        return None
    from finboard_data.releases import _available_at_timestamp_column

    available = _available_at_timestamp_column(table.column("available_at"))
    values = available.cast(pa.int64()).to_numpy(zero_copy_only=False)
    max_available = int(values.max())
    # sorted()[-1] 的稳定排序等值选择:并列取原读取顺序最后一条。
    index = int(values.size - 1 - np.argmax(values[::-1] == max_available))
    row: dict[str, object] = table.slice(index, 1).to_pylist()[0]
    return row


def _daily_history_from_table(
    table: pa.Table,
    *,
    decision_at_micros: np.ndarray,
    decision_day_ends: np.ndarray,
    range_start_day: int,
    release_id: str,
) -> SymbolDailyMetricsHistory:
    """对单标的已门控表做全部决策期的选行 + 紧凑列抽取(#438)。

    选行语义由 :func:`finboard_data.releases._daily_metrics_latest_visible_indices`
    保证(零逐行 Python);payload 列整列 ``to_numpy`` 后按选中行下标一次性
    聚合成 (n_periods, n_cols) 矩阵——null/无效格以掩码表达,重建行 dict 时
    还原为 None。``available_at``/``source`` 只对选中行 ``take`` 物化(P 期
    规模),原值保留使 ``record`` 的领域对象重建与旧路径逐值一致。
    """
    import pyarrow as pa

    from finboard_data.releases import (
        _DAILY_METRICS_FLOAT_PAYLOAD_FIELDS,
        _DAILY_METRICS_INT_PAYLOAD_FIELDS,
        _daily_metrics_latest_visible_indices,
    )

    selected = _daily_metrics_latest_visible_indices(
        table,
        decision_at_micros=decision_at_micros,
        decision_day_ends=decision_day_ends,
        range_start_day=range_start_day,
        release_id=release_id,
    )
    n_periods = int(decision_at_micros.shape[0])
    # -1(无可见行)占位到行 0,取值后由 valid/available_at 掩码屏蔽。
    rows = np.where(selected >= 0, selected, 0)
    columns = tuple(
        name for name in _DAILY_METRICS_FLOAT_PAYLOAD_FIELDS if name in table.column_names
    )
    values = np.zeros((n_periods, len(columns)), dtype=np.float64)
    valid = np.zeros((n_periods, len(columns)), dtype=bool)
    for j, name in enumerate(columns):
        column = table.column(name)
        null_mask = (
            column.is_null().to_numpy(zero_copy_only=False).astype(dtype=bool, copy=False)
        )
        picked = np.asarray(
            column.to_numpy(zero_copy_only=False), dtype=np.float64
        )[rows]
        keep = (selected >= 0) & ~null_mask[rows]
        values[:, j] = np.where(keep, picked, 0.0)
        valid[:, j] = keep
    limit_status = np.zeros(n_periods, dtype=np.int64)
    limit_status_valid = np.zeros(n_periods, dtype=bool)
    for name in _DAILY_METRICS_INT_PAYLOAD_FIELDS:
        if name not in table.column_names:
            continue
        column = table.column(name)
        null_mask = (
            column.is_null().to_numpy(zero_copy_only=False).astype(dtype=bool, copy=False)
        )
        limit_status = (
            column.fill_null(0).cast(pa.int64()).to_numpy(zero_copy_only=False).astype(
                dtype=np.int64, copy=False
            )[rows]
        )
        limit_status_valid = (selected >= 0) & ~null_mask[rows]
        break
    # 选中行 trade_date 恒非空(可见子集 null 已在选行期具名拒绝)。
    trade_raw = (
        _trade_day_column_for_history(table).to_numpy(zero_copy_only=False).astype(np.float64)
    )
    trade_filled = np.where(np.isnan(trade_raw), 0, trade_raw).astype(np.int32)[rows]
    available_at: list[object] = [None] * n_periods
    source: list[object] = [None] * n_periods
    selected_periods = np.flatnonzero(selected >= 0)
    if selected_periods.size:
        selected_rows = pa.array(selected[selected_periods])
        taken_available = table.column("available_at").take(selected_rows).to_pylist()
        taken_source = (
            table.column("source").take(selected_rows).to_pylist()
            if "source" in table.column_names
            else [None] * selected_periods.size
        )
        for period, value, src in zip(
            selected_periods.tolist(), taken_available, taken_source, strict=True
        ):
            available_at[period] = value
            source[period] = src
    return SymbolDailyMetricsHistory(
        columns=columns,
        values=values,
        valid=valid,
        limit_status=limit_status,
        limit_status_valid=limit_status_valid,
        trade_date_days=trade_filled,
        available_at=tuple(available_at),
        source=tuple(source),
    )


def _trade_day_column_for_history(table: pa.Table) -> pa.ChunkedArray:
    """history 抽取用 trade_date → int32 epoch 天数(null 保持 null)。"""
    import pyarrow as pa

    from finboard_data.releases import _trade_date_day_column

    return _trade_date_day_column(table.column("trade_date")).cast(pa.int32())


async def _load_daily_metrics_features(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    decision_at: datetime,
    release_id: str,
    *,
    precompute: DailyMetricsPrecompute | None = None,
) -> tuple[list[FeatureValue], tuple[str, ...]]:
    """把 daily_metrics 发布观测映射为因子值(PIT 门控,复用 extract_factor_matrix)。

    #252:缺标的容忍语义同 :func:`_load_financial_features`。

    #438:``precompute`` 命中(run 级预建,见
    :meth:`FrozenInputLoader.ensure_daily_metrics_histories`)时直接查矩阵
    装配本期观测——零 IO 零逐行解析;未预建(直接 ``load_context`` 调用 /
    stub provider)走既有逐期路径。

    列式优先(issue #371 同构的 getattr 探测):provider 提供
    ``fetch_daily_metrics_columns``(``FrozenReleaseProvider`` 已实现,PIT/
    区间门控在 Arrow 内完成)时,只把每标的 ``available_at`` 最新的一条可见行
    物化为 dict 并复用 ``_daily_metrics_from_release_row`` 强转——对象路径会把
    「发布起点→决策日」的全部历史行逐行强转成领域对象后仅取最新一条,全市场
    发布 x 多期下 99.9% 强转被扔掉、纯 Python 单线程成为加载瓶颈。测试 stub 等
    无该属性的 provider 回退对象路径,选择语义逐值一致(稳定排序并列取原顺序
    最后一条,PIT 边界 ``available_at == decision_at`` 两路径均可见)。
    """
    from finboard_data.factors import FactorInputRecord
    from finboard_data.releases import (
        ReleaseCapabilityError,
        _daily_metrics_from_release_row,
    )
    from finboard_shared.models import Symbol

    if precompute is not None:
        period = precompute.period_index.get(decision_at)
        if period is not None:
            return _daily_metrics_features_from_precompute(
                precompute, candidates, release_id, period
            )

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)
    # issue #371 同构(getattr 探测,``data_mount`` 挂载写入器 hasattr 同款):
    # 列式优先,无 ``fetch_daily_metrics_columns`` 属性的 provider 回退对象路径。
    fetch_columns = getattr(provider, "fetch_daily_metrics_columns", None)

    async def _one(
        candidate: UniverseCandidate,
    ) -> tuple[FactorInputRecord | None, bool]:
        async with semaphore:
            symbol = Symbol(code=candidate.symbol, market=_market_from_value(candidate.market))
            try:
                if fetch_columns is not None:
                    row = _latest_daily_metrics_row(
                        await fetch_columns(
                            symbol,
                            start=provider.release.start_date,
                            end=decision_at.date(),
                            decision_at=decision_at,
                        )
                    )
                    latest = (
                        _daily_metrics_from_release_row(row, symbol=symbol.code)
                        if row is not None
                        else None
                    )
                else:
                    records = await provider.fetch_daily_metrics(
                        symbol,
                        start=provider.release.start_date,
                        end=decision_at.date(),
                        decision_at=decision_at,
                    )
                    if not records:
                        latest = None
                    else:
                        # 取决策时点可见的最新一条(同一 trade_date 理论上一条;排序保最新)。
                        latest = sorted(records, key=lambda item: item.available_at)[-1]
            except ReleaseCapabilityError:
                return None, True
        if latest is None:
            return None, False
        return (
            FactorInputRecord(
                symbol=candidate.symbol,
                profile=None,
                daily=latest,
                financial=None,
                industry=None,
            ),
            False,
        )

    # issue #287:逐候选并发读取(gather + 信号量);结果与异常都按候选顺序
    # 组装/抛出,与串行实现逐值一致。
    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    factor_rows: list[FactorInputRecord] = []
    missing: list[str] = []
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        record, is_missing = result
        if is_missing:
            missing.append(candidate.symbol)
        elif record is not None:
            factor_rows.append(record)
    return _daily_feature_values(factor_rows, missing, release_id)


def _daily_metrics_features_from_precompute(
    precompute: DailyMetricsPrecompute,
    candidates: Sequence[UniverseCandidate],
    release_id: str,
    period: int,
) -> tuple[list[FeatureValue], tuple[str, ...]]:
    """查预计算矩阵装配单期 daily_metrics 观测(#438;与逐期路径逐值等值)。

    候选顺序组装(与逐期路径一致):``histories`` 值 ``None`` = 预建期
    ``ReleaseCapabilityError``(标的不在该研究发布)→ #252 missing;矩阵无
    可见行 → 该标的无观测(非 missing);有行 → ``record`` 按需重建领域对象。
    """
    from finboard_data.factors import FactorInputRecord

    factor_rows: list[FactorInputRecord] = []
    missing: list[str] = []
    for candidate in candidates:
        history = precompute.histories.get(candidate.symbol)
        if history is None:
            missing.append(candidate.symbol)
            continue
        latest = history.record(period, symbol=candidate.symbol)
        if latest is not None:
            factor_rows.append(
                FactorInputRecord(
                    symbol=candidate.symbol,
                    profile=None,
                    daily=latest,
                    financial=None,
                    industry=None,
                )
            )
    return _daily_feature_values(factor_rows, missing, release_id)


def _daily_feature_values(
    factor_rows: Sequence[FactorInputRecord],
    missing: Sequence[str],
    release_id: str,
) -> tuple[list[FeatureValue], tuple[str, ...]]:
    """factor 行 + missing 集合 → (FeatureValue 列表, missing 元组) 共用尾部。

    ``FactorInputRecord`` 仅作窄化引用;批构造与矩阵提取与逐期路径共用。
    """
    from finboard_data.factors import FactorInputBatch

    if not factor_rows:
        return [], tuple(missing)
    batch = FactorInputBatch(records=tuple(factor_rows), source="tushare", dataset_versions={"research_release": "frozen"})
    return _matrix_to_feature_values(batch, release_id=release_id), tuple(missing)


async def _build_daily_metrics_precompute(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    decision_ats: Sequence[datetime],
    release_id: str,
    *,
    tick: Callable[[], Awaitable[None]] | None = None,
) -> DailyMetricsPrecompute:
    """并发预建一个 daily_metrics 发布的全部决策期矩阵(#438)。

    每标的以 ``decision_at=_PIT_UNBOUNDED`` 做一次列式读取(PIT/区间门控在
    Arrow 内完成,全区间可见),选行与列抽取 numpy 化后置 ``to_thread``;
    逐候选 ``gather`` + 信号量并发,异常按候选顺序抛出(与 close 矩阵构建
    同语义)。标的不在发布(``ReleaseCapabilityError``)→ ``None``(#252
    missing),其余异常原样传播(fail-closed)。``tick`` 非空时逐标的构建
    完成后调用一次(issue #450 预计算进度打点,节流见
    :func:`_make_precompute_ticker`)。
    """
    from finboard_data.releases import ReleaseCapabilityError, _epoch_micros
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)
    decision_at_micros = np.array(
        [_epoch_micros(at) for at in decision_ats], dtype=np.int64
    )
    decision_day_ends = np.array(
        [at.date().toordinal() - _EPOCH_ORDINAL for at in decision_ats],
        dtype=np.int32,
    )
    range_start_day = provider.release.start_date.toordinal() - _EPOCH_ORDINAL

    async def _one(candidate: UniverseCandidate) -> SymbolDailyMetricsHistory | None:
        async with semaphore:
            symbol = Symbol(
                code=candidate.symbol, market=_market_from_value(candidate.market)
            )
            try:
                table = await provider.fetch_daily_metrics_columns(
                    symbol,
                    start=provider.release.start_date,
                    end=provider.release.end_date,
                    decision_at=_PIT_UNBOUNDED,
                )
            except ReleaseCapabilityError:
                return None
        history = await asyncio.to_thread(
            _daily_history_from_table,
            table,
            decision_at_micros=decision_at_micros,
            decision_day_ends=decision_day_ends,
            range_start_day=range_start_day,
            release_id=release_id,
        )
        if tick is not None:
            await tick()
        return history

    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    histories: dict[str, SymbolDailyMetricsHistory | None] = {}
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        histories[candidate.symbol] = result
    logger.debug(
        "frozen_loader.daily_metrics_history_built",
        release_id=release_id,
        candidates=len(candidates),
        periods=len(decision_ats),
        symbols_precomputed=sum(1 for item in histories.values() if item is not None),
        precompute_bytes=sum(item.nbytes for item in histories.values() if item is not None),
    )
    return DailyMetricsPrecompute(
        release_id=release_id,
        period_index={at: index for index, at in enumerate(decision_ats)},
        histories=histories,
    )


async def _load_financial_features(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    decision_at: datetime,
    release_id: str,
) -> tuple[list[FeatureValue], tuple[str, ...]]:
    """把 financial_indicators 发布观测映射为因子值(PIT 门控)。

    #252:标的不在发布中时容忍(factor_lab #212 同语义)——该标的因子观测
    缺失并计入返回的 missing 元组,不回退外部数据源、不 fail-closed。
    """
    from finboard_data.factors import FactorInputBatch, FactorInputRecord
    from finboard_data.releases import ReleaseCapabilityError
    from finboard_data.research import FinancialIndicator
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)

    async def _one(
        candidate: UniverseCandidate,
    ) -> tuple[FactorInputRecord | None, bool]:
        async with semaphore:
            symbol = Symbol(code=candidate.symbol, market=_market_from_value(candidate.market))
            try:
                records = await provider.fetch_financial_indicators(
                    symbol,
                    decision_at=decision_at,
                )
            except ReleaseCapabilityError:
                # 标的不在该研究发布中:计入缺失,继续其余标的( bars 主发布缺
                # 标的仍由候选构建 fail-closed,语义见 factor_lab #212 注释)。
                return None, True
        if not records:
            return None, False
        # 同一 report_period 保留公告修订(update_flag);跨期取最新公告的一期。
        by_period: dict[date, FinancialIndicator] = {}
        for item in records:
            previous = by_period.get(item.report_period)
            if previous is None or item.available_at > previous.available_at:
                by_period[item.report_period] = item
        latest = max(by_period.values(), key=lambda item: item.available_at)
        return (
            FactorInputRecord(
                symbol=candidate.symbol,
                profile=None,
                daily=None,
                financial=latest,
                industry=None,
            ),
            False,
        )

    # issue #287:逐候选并发读取(gather + 信号量);结果与异常都按候选顺序
    # 组装/抛出,与串行实现逐值一致。
    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    factor_rows: list[FactorInputRecord] = []
    missing: list[str] = []
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        record, is_missing = result
        if is_missing:
            missing.append(candidate.symbol)
        elif record is not None:
            factor_rows.append(record)
    if not factor_rows:
        return [], tuple(missing)
    batch = FactorInputBatch(records=tuple(factor_rows), source="tushare", dataset_versions={"research_release": "frozen"})
    return _matrix_to_feature_values(batch, release_id=release_id), tuple(missing)


def _matrix_to_feature_values(
    batch: FactorInputBatch,
    *,
    release_id: str,
) -> list[FeatureValue]:
    """复用因子提取矩阵,把研究数据横截面映射为 ``FeatureValue``。

    只提取 universe 过滤依赖的日频/财务因子(pb / 市值 / 换手 / ROE /
    毛利率 / 负债率 / 营收增速),与 ``extract_factor_matrix`` 输出一致,
    避免因子映射逻辑在加载器与快照构建之间漂移。
    """
    from finboard_backtest.factors.extract import extract_factor_matrix

    matrix = extract_factor_matrix(batch)
    # symbol → 最新观测时点一次遍历预聚合(此前逐 (因子 x 标的) 全量扫描
    # records,矩阵 75k 键 x 5000 记录 = 每期数亿次比较,是加载期主导热点;
    # 映射后 O(records + 矩阵键数))。
    latest_by_symbol: dict[str, datetime] = {}
    for record in batch.records:
        candidates = [
            _require_aware(item.available_at)
            for item in (record.daily, record.financial)
            if item is not None
        ]
        if not candidates:
            continue
        latest = max(candidates)
        current = latest_by_symbol.get(record.symbol)
        if current is None or latest > current:
            latest_by_symbol[record.symbol] = latest
    values: list[FeatureValue] = []
    for factor_name, by_symbol in sorted(matrix.items()):
        for symbol, value in sorted(by_symbol.items()):
            available_at = latest_by_symbol.get(symbol)
            if available_at is None:
                available_at = datetime.now(UTC)
            values.append(
                FeatureValue(
                    symbol=symbol,
                    feature_id=factor_name,
                    value=float(value),
                    source_artifact_ids=(release_id,),
                    available_at=available_at,
                )
            )
    return values


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _build_candidates_and_lots(
    instruments: Sequence[ReleasedInstrument],
) -> tuple[tuple[UniverseCandidate, ...], dict[str, AssetLotInfo]]:
    """从发布 instruments 构造候选池(全部 included)与执行元数据映射。

    issue #256:指数基准资产(``instrument_type=index``)只做基准数据
    (``benchmark_config.symbol`` 经 ``_load_benchmark_curve`` 单独读取),
    不可撮合,不进候选池与执行元数据——与静态预检
    (``static_universe_candidates``)共用 :func:`is_benchmark_only_instrument`,
    两边口径一致。
    """
    from finboard_backtest.strategy_spec.universe_precheck import (
        is_benchmark_only_instrument,
    )

    candidates: list[UniverseCandidate] = []
    lot_info: dict[str, AssetLotInfo] = {}
    skipped_benchmark: list[str] = []
    for inst in instruments:
        if not inst.ready:
            continue
        if is_benchmark_only_instrument(inst):
            skipped_benchmark.append(inst.code)
            continue
        candidates.append(
            UniverseCandidate(
                symbol=inst.code,
                included=True,
                reasons=("冻结发布标的且 ready=True",),
                asset_class=inst.asset_class.value,
                market=inst.market.value,
            )
        )
        lot_info[inst.code] = _execution_to_lot_info(inst)
    if skipped_benchmark:
        logger.debug(
            "frozen_loader.benchmark_only_skipped",
            count=len(skipped_benchmark),
            symbols=skipped_benchmark[:20],
            message="指数基准资产不进候选池(只做 benchmark 行情)",
        )
    return tuple(candidates), lot_info


def _execution_to_lot_info(instrument: ReleasedInstrument) -> AssetLotInfo:
    """``ExecutionMetadata`` → ``AssetLotInfo`` 字段映射。"""
    exec_meta = instrument.execution
    return AssetLotInfo(
        code=instrument.code,
        lot_size=int(exec_meta.lot_size),
        multiplier=float(exec_meta.multiplier),
        margin_rate=float(exec_meta.margin_rate) if exec_meta.margin_rate is not None else None,
        commission_rate=float(exec_meta.commission_rate),
        commission_min=float(exec_meta.commission_min),
        stamp_tax_rate=float(exec_meta.stamp_tax_rate),
    )


def _history_from_points(
    points: Sequence[PointInTimeBar],
) -> SymbolCloseHistory | None:
    """把发布全区间 PIT bars 转成可切片的 close 历史(#287,对象路径)。

    issue #300 后矩阵预建已走列式直出(:func:`_load_close_histories`),本函数
    保留给需要从对象序列构建历史的调用方(等值测试对照)。``available_at`` /
    bar 日期任一序列出现回退(异常数据,非递减被破坏)时返回 ``None``:前缀
    切片不再与逐期过滤等价,调用方对该标的回退逐期读取(校验在
    :meth:`SymbolCloseHistory.from_sequences` 的 int64 数组上进行,#439)。
    """
    return SymbolCloseHistory.from_sequences(
        available_at=[item.available_at for item in points],
        dates=[item.bar.timestamp.date() for item in points],
        closes=[float(item.bar.close) for item in points],
    )


async def _load_close_histories(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    *,
    include_open: bool = False,
    progress: PrecomputeProgressReporter | None = None,
    cancel_probe: PrecomputeCancelProbe | None = None,
) -> dict[str, SymbolCloseHistory | None]:
    """并发读取各标的冻结全区间 close 历史,构建 close 矩阵(issue #287)。

    只对真实 ``FrozenReleaseProvider`` 启用:其 ``available_at`` 由发布元数据
    确定性派生(日线 = 各市场收盘规则时点),全区间一次读取 + 逐期前缀切片
    与逐期 PIT 过滤读取逐值等价。其它实现(测试 stub 等)返回空映射,调用方
    全部走逐期回退读取,行为与优化前一致。逐标的读取经 ``asyncio.gather`` +
    信号量并发化;异常按候选顺序抛出,与串行语义一致。

    issue #300:读取走列式直出(``fetch_close_history``,timestamp/close 两列
    float64 直出),不再经 Bar / Decimal 逐行对象构造;列式序列按发布 bar 顺序
    平行排列且 available_at 是业务日期的确定性函数,矩阵切片的「时间升序前缀」
    前提由构造保证(此前依赖逐点单调性检查,异常数据回退逐期读取——列式路径
    排序后该前提恒成立,回退不再有触发面,取值语义不变)。
    """
    from finboard_data.releases import FrozenReleaseProvider
    from finboard_shared.models import Symbol

    if not candidates or not isinstance(provider, FrozenReleaseProvider):
        return {}
    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)
    tick = _make_precompute_ticker(progress, "close", len(candidates), cancel_probe)

    async def _one(candidate: UniverseCandidate) -> SymbolCloseHistory | None:
        async with semaphore:
            columns = await provider.fetch_close_history(
                Symbol(
                    code=candidate.symbol,
                    market=_market_from_value(candidate.market),
                ),
                provider.release.period,
                provider.release.start_date,
                provider.release.end_date,
                decision_at=_PIT_UNBOUNDED,
                adjust=provider.release.adjustment,
                include_open=include_open,
            )
        history = SymbolCloseHistory.from_sequences(
            available_at=columns.available_at,
            dates=columns.dates,
            closes=columns.closes,
            opens=columns.opens,
        )
        await tick()
        return history

    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    built: dict[str, SymbolCloseHistory | None] = {}
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        built[candidate.symbol] = result
    logger.debug(
        "frozen_loader.close_history_built",
        release_id=provider.release.release_id,
        candidates=len(candidates),
        sliceable=sum(1 for item in built.values() if item is not None),
    )
    return built


async def build_price_feature_snapshot_from_close_matrix(
    *,
    histories: Mapping[str, SymbolCloseHistory | None],
    provider: FrozenReleaseProvider,
    decision_at: datetime,
    code_version: str,
    symbols: Sequence[str],
    momentum_lookback: int | None = None,
    volatility_windows: tuple[int, ...] | None = None,
) -> FeatureSnapshot:
    """从 close 矩阵切片直接构建逐期价格特征快照(#450 追续)。

    逐期 ``build_price_feature_snapshot`` 在进程池对每标的整文件重读 close
    历史(全市场 x 多期 = 数十万次重复读盘),而 close 矩阵(#287/#439)已
    持有同一发布的全区间 close——特征数学(:func:`price_observations_from_
    closes`)只依赖序列尾部连续元素,尾切片与全序列计算逐位等值,直接切片
    供数即可。切片/重建在单次 ``to_thread`` 内完成(纯 numpy + 少量
    datetime,秒级);矩阵未覆盖的标的(``None`` / 缺键)回退 provider
    逐标的读取(与原路径同 PIT/边界语义);FeatureSnapshot 装配与列式路径
    共用 :func:`_assemble_price_snapshot_from_observations`(单一样本源)。
    仅支持 D1 发布(矩阵只对日线构建);``decision_at`` 需带时区。
    """
    from finboard_backtest.factor_lab import (
        DEFAULT_MOMENTUM_LOOKBACK,
        FactorAnalysisError,
        _assemble_price_snapshot_from_observations,
        _build_price_observations,
        price_observations_from_closes,
    )
    from finboard_shared.models import Symbol

    if decision_at.tzinfo is None:
        raise ValueError("decision_at 必须带时区")
    lookback = momentum_lookback if momentum_lookback is not None else DEFAULT_MOMENTUM_LOOKBACK
    windows = volatility_windows if volatility_windows is not None else (20, 60, 120)
    release = provider.release
    by_code = {item.code: item for item in release.instruments}
    scoped = [code for code in by_code if code in set(symbols)]
    tail_n = max(lookback + 1, max(windows) + 1, 61)

    async def _fallback(code: str) -> list[FeatureObservation]:
        columns = await provider.fetch_close_history(
            Symbol(code, by_code[code].market),
            release.period,
            release.start_date,
            min(decision_at.date(), release.end_date),
            decision_at=decision_at,
            adjust=release.adjustment,
        )
        return _build_price_observations(
            source=release.source,
            source_version=release.version,
            symbol=code,
            market=by_code[code].market,
            asset_class=by_code[code].asset_class,
            columns=columns,
            momentum_lookback=lookback,
            volatility_windows=windows,
        )

    def _slice_one(code: str) -> list[FeatureObservation]:
        history = histories[code]
        assert history is not None  # 调用方已按矩阵覆盖过滤
        item = by_code[code]
        index = history.visible_index(decision_at)
        if index < 0:
            return []
        lo = max(0, index + 1 - tail_n)
        closes = history.closes[lo : index + 1]
        last_date = date.fromordinal(int(history.date_days[index]) + _EPOCH_ORDINAL)
        return price_observations_from_closes(
            source=release.source,
            source_version=release.version,
            symbol=code,
            market=item.market,
            asset_class=item.asset_class,
            closes=closes,
            last_timestamp=datetime.combine(last_date, time(0, 0), tzinfo=UTC),
            last_available_at=history.available_at_at(index),
            momentum_lookback=lookback,
            volatility_windows=windows,
        )

    results: dict[str, list[FeatureObservation]] = {}
    matrix_codes = [code for code in scoped if histories.get(code) is not None]
    fallback_codes = [code for code in scoped if histories.get(code) is None]
    if matrix_codes:
        def _slice_batch() -> dict[str, list[FeatureObservation]]:
            return {code: _slice_one(code) for code in matrix_codes}

        results.update(await asyncio.to_thread(_slice_batch))
    if fallback_codes:
        semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)

        async def _one(code: str) -> tuple[str, list[FeatureObservation]]:
            async with semaphore:
                return code, await _fallback(code)

        for code, obs in await asyncio.gather(
            *(_one(code) for code in fallback_codes)
        ):
            results[code] = obs
    observations = [item for code in scoped for item in results.get(code, [])]
    if not observations:
        raise FactorAnalysisError("冻结发布在决策时点没有足够数据计算价格特征")
    return _assemble_price_snapshot_from_observations(
        release=release,
        decision_at=decision_at,
        code_version=code_version,
        observations=observations,
        momentum_lookback=lookback,
        volatility_windows=windows,
    )


async def _load_close_histories_via_pool(
    pool: PriceFeatureProcessPool,
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    *,
    include_open: bool = False,
    progress: PrecomputeProgressReporter | None = None,
    cancel_probe: PrecomputeCancelProbe | None = None,
) -> dict[str, SymbolCloseHistory | None] | None:
    """经常驻进程池预建 close 矩阵(issue #301);不可用时返回 ``None``。

    worker 内完成 parquet 解码 + 列式转换(PIT 门控含内),主进程只收列式
    数据并组装 ``SymbolCloseHistory``,绕开反序列化段的 GIL;磁盘并发由
    ``worker_count`` 个串行 worker 进程自然钳制(与 io_semaphore 同语义)。
    池未启动 / 任务损坏(BrokenProcessPool 等)/ 逐任务业务异常时记具名
    warning 并返回 ``None``,调用方降级为进程内线程路径(结果逐值一致,
    :func:`_load_close_histories`);不修改池自身状态(#288 特征路径的降级
    语义自行处理)。结果按候选顺序组装,与进程内路径逐值一致。
    """
    from finboard_backtest.factor_lab import (
        _CloseHistoryProcessTask,
        _compute_close_history_process_task,
    )
    from finboard_data.releases import CloseHistoryColumns

    if not candidates or pool.broken:
        return None
    try:
        executor = pool.executor
    except RuntimeError:
        return None
    loop = asyncio.get_running_loop()
    tasks = [
        _CloseHistoryProcessTask(
            code=candidate.symbol,
            start=provider.release.start_date,
            end=provider.release.end_date,
            decision_at=_PIT_UNBOUNDED,
            include_open=include_open,
        )
        for candidate in candidates
    ]
    tick = _make_precompute_ticker(progress, "close", len(candidates), cancel_probe)

    async def _run_one(
        task: _CloseHistoryProcessTask,
    ) -> tuple[str, CloseHistoryColumns]:
        result = await loop.run_in_executor(
            executor, _compute_close_history_process_task, task
        )
        await tick()
        return result

    try:
        results = await asyncio.gather(
            *(_run_one(task) for task in tasks),
            return_exceptions=True,
        )
    except ResearchRunInterruptedError:
        # 取消/打断探针异常不是池故障:原样上抛交给上层收口(#450 追续),
        # 降级成进程内路径会无视取消继续整段预建。
        raise
    except Exception as exc:
        logger.warning(
            "frozen_loader.close_matrix_pool_failed",
            stage="decision_load",
            release_id=provider.release.release_id,
            candidates=len(candidates),
            error=str(exc),
            message="close 矩阵池分发失败,降级进程内路径",
        )
        return None
    built: dict[str, SymbolCloseHistory | None] = {}
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            if isinstance(result, ResearchRunInterruptedError):
                raise result
            logger.warning(
                "frozen_loader.close_matrix_pool_failed",
                stage="decision_load",
                release_id=provider.release.release_id,
                candidates=len(candidates),
                symbol=candidate.symbol,
                error=str(result),
                message="close 矩阵池任务异常,降级进程内路径",
            )
            return None
        code, columns = result
        built[code] = SymbolCloseHistory.from_sequences(
            available_at=columns.available_at,
            dates=columns.dates,
            closes=columns.closes,
            opens=columns.opens,
        )
    logger.debug(
        "frozen_loader.close_history_built_via_pool",
        release_id=provider.release.release_id,
        candidates=len(candidates),
        worker_count=pool.worker_count,
    )
    return built


async def _load_close_prices(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    as_of: datetime,
) -> dict[str, float]:
    """PIT 门控逐期读取各标的在 ``as_of`` 时点可见的最新 close 价(回退路径)。

    issue #287:逐 symbol 串行改 ``asyncio.gather`` + 信号量;结果按候选顺序
    组装、异常按候选顺序抛出,与串行实现逐值一致。
    """
    # 延迟导入避免顶层依赖 finboard_shared.models 的循环引用。
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)

    async def _one(candidate: UniverseCandidate) -> float | None:
        async with semaphore:
            bars = await provider.fetch_point_in_time_bars(
                Symbol(
                    code=candidate.symbol,
                    market=_market_from_value(candidate.market),
                ),
                provider.release.period,
                provider.release.start_date,
                as_of.date(),
                decision_at=as_of,
                adjust=provider.release.adjustment,
            )
        if not bars:
            return None
        return float(bars[-1].bar.close)

    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    prices: dict[str, float] = {}
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            prices[candidate.symbol] = result
    return prices


async def _load_execution_prices_from_bars(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    read_as_of: datetime,
    *,
    want_open: bool,
) -> dict[str, float]:
    """逐期对象路径读取执行价(issue #336;矩阵未覆盖 / 未携带 opens 的回退)。

    与 :func:`_load_close_prices` 同一 PIT 门控与并发语义,取最后可见 bar 的
    open(next_open)或 close(next_close);执行日无 bar 的标的沿用最后
    可见 bar 的价格(与矩阵路径同一降级语义)。
    """
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)

    async def _one(candidate: UniverseCandidate) -> float | None:
        async with semaphore:
            bars = await provider.fetch_point_in_time_bars(
                Symbol(
                    code=candidate.symbol,
                    market=_market_from_value(candidate.market),
                ),
                provider.release.period,
                provider.release.start_date,
                read_as_of.date(),
                decision_at=read_as_of,
                adjust=provider.release.adjustment,
            )
        if not bars:
            return None
        last = bars[-1].bar
        return float(last.open if want_open else last.close)

    results = await asyncio.gather(
        *(_one(candidate) for candidate in candidates), return_exceptions=True
    )
    prices: dict[str, float] = {}
    for candidate, result in zip(candidates, results, strict=True):
        if isinstance(result, BaseException):
            raise result
        if result is not None:
            prices[candidate.symbol] = result
    return prices


def _market_from_value(value: str) -> Market:
    """容忍 ReleasedInstrument.market.value 与 Market 枚举值的差异。"""
    from finboard_shared.types import Market

    try:
        return Market(value)
    except ValueError:
        # 兜底:A 股代码默认 A_SHARE,其它按 value 小写启发式映射。
        lowered = value.lower()
        if "a_share" in lowered or lowered.endswith((".sh", ".sz", ".bj")):
            return Market.A_SHARE
        if "future" in lowered:
            return Market.FUTURE
        return Market.A_SHARE


def series_feature_values(
    record: FactorSeriesRecordLike,
    decision_at: datetime,
    *,
    factor_name: str,
) -> tuple[FeatureValue, ...]:
    """把序列在 ``decision_at`` 决策日的截面映射为 ``FeatureValue``(#360)。

    与快照路径(``_load_features``)喂同一 ``extract_factor_matrix`` 消费的
    ``FeatureValue`` 形态;``available_at = decision_at`` 由前缀不变性审计
    背书(序列构建即审计,决策日观测只用决策日之前的数据)。调用方负责
    判定决策日已被序列覆盖(缺失日发具名 warning 后跳过)。
    """
    day_values = record.values.get(decision_at.date().isoformat())
    if day_values is None:
        return ()
    return tuple(
        FeatureValue(
            symbol=symbol,
            feature_id=factor_name,
            value=None if value is None else float(value),
            source_artifact_ids=(record.series_id,),
            available_at=decision_at,
        )
        for symbol, value in sorted(day_values.items())
    )


def _build_artifact_ids(manifest: ResearchRunManifest) -> tuple[str, ...]:
    """收集 manifest 冻结的所有 artifact_id(release + snapshot + series)。"""
    ids: list[str] = [ref.artifact_id for ref in manifest.dataset_releases]
    ids.extend(ref.artifact_id for ref in manifest.factor_snapshots)
    ids.extend(ref.artifact_id for ref in manifest.factor_series)
    # 去重保序(PortfolioDecisionInput 要求 input_artifact_ids 唯一)。
    seen: set[str] = set()
    unique: list[str] = []
    for artifact_id in ids:
        if artifact_id not in seen:
            seen.add(artifact_id)
            unique.append(artifact_id)
    return tuple(unique)


__all__ = [
    "DailyMetricsPrecompute",
    "FactorSeriesProvider",
    "FeatureSnapshotProvider",
    "FrozenInputLoader",
    "LoadedDecisionContext",
    "ReleaseProviderFactory",
    "SuspensionView",
    "SymbolCloseHistory",
    "SymbolDailyMetricsHistory",
    "series_feature_values",
]
