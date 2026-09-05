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
from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from itertools import pairwise
from typing import TYPE_CHECKING, Protocol

import structlog

from finboard_backtest.portfolio.contracts import AssetLotInfo
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    UniverseCandidate,
)

if TYPE_CHECKING:
    from finboard_backtest.factor_lab import PriceFeatureProcessPool
    from finboard_data.factor_lab import FeatureSnapshot
    from finboard_data.factors import FactorInputBatch
    from finboard_data.releases import (
        FrozenReleaseProvider,
        PointInTimeBar,
        ReleasedInstrument,
    )
    from finboard_shared.types import Market

logger = structlog.get_logger(__name__)


class ReleaseProviderFactory(Protocol):
    """按 release_id 构造 ``FrozenReleaseProvider`` 的工厂(注入点)。"""

    def __call__(self, release_id: str) -> FrozenReleaseProvider: ...


class FeatureSnapshotProvider(Protocol):
    """按 snapshot_id 读取 ``FeatureSnapshot`` 的回调(注入点)。"""

    async def __call__(self, snapshot_id: str) -> FeatureSnapshot | None: ...


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


@dataclass(frozen=True, slots=True)
class SymbolCloseHistory:
    """单标的冻结全区间 close 历史(close 矩阵切片底座,issue #287)。

    三个序列都按 bar 时间升序且构建时校验非递减:``available_at`` 是 provider
    的 PIT 门控键(真实 provider 由发布元数据确定性派生),``dates`` 是逐期
    读取原有的日期上界过滤键。两个键的「可见集合」都是时间升序前缀,取二者
    较小边界即与「逐期 PIT 过滤后取末根 / 全序列」逐值等价。
    """

    available_at: tuple[datetime, ...]
    dates: tuple[date, ...]
    closes: tuple[float, ...]
    #: 可选携带的 open 平行序列(issue #336):仅 timing=next_open 的 run 在
    #: 矩阵预建时附带,与 available_at/dates/closes 严格同长;``None`` 表示
    #: 未携带(close-only 矩阵),open 查询由调用方回退逐期对象路径读取。
    opens: tuple[float, ...] | None = None

    def visible_index(self, as_of: datetime) -> int:
        """``as_of`` 时点可见最后一根的索引;-1 表示无可见 bar。"""
        index = min(
            bisect_right(self.available_at, as_of),
            bisect_right(self.dates, as_of.date()),
        )
        return index - 1

    def close_at(self, as_of: datetime) -> float | None:
        """``as_of`` 时点可见的最新 close;无可见 bar 时为 ``None``。"""
        index = self.visible_index(as_of)
        if index < 0:
            return None
        return self.closes[index]

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
        return self.opens[index]

    def series_until(self, as_of: datetime) -> list[float]:
        """``as_of`` 时点可见的完整 close 序列(时间升序;可能为空)。"""
        index = self.visible_index(as_of)
        if index < 0:
            return []
        return list(self.closes[: index + 1])


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
    # symbol → close 历史;``None`` 表示该标的不可安全切片(非单调数据),
    # 逐期回退读取。空映射 = 矩阵未启用(非真实 provider)。
    _close_histories: dict[str, SymbolCloseHistory | None] = field(
        default_factory=dict, init=False, repr=False
    )
    # 矩阵构建只尝试一次(候选集逐期不变);失败不缓存,逐期回退。
    _close_histories_built: bool = field(default=False, init=False, repr=False)

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
        features = await self._load_features(manifest.factor_snapshots, decision_at)
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
        )

    async def ensure_close_histories(
        self,
        manifest: ResearchRunManifest,
        *,
        process_pool: PriceFeatureProcessPool | None = None,
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
                process_pool, provider, included_candidates, include_open=include_open
            )
            if built is not None:
                self._close_histories_built = True
                self._close_histories.update(built)
                return
        await self._ensure_close_histories(
            provider, included_candidates, include_open=include_open
        )

    async def _ensure_close_histories(
        self,
        provider: FrozenReleaseProvider,
        candidates: Sequence[UniverseCandidate],
        *,
        include_open: bool = False,
    ) -> None:
        """构建 close 矩阵(只尝试一次;失败不缓存,逐期回退读取)。"""
        if self._close_histories_built:
            return
        self._close_histories_built = True
        self._close_histories.update(
            await _load_close_histories(
                provider, candidates, include_open=include_open
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
                    provider, candidates, decision_at, release_ref.artifact_id
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
    ) -> tuple[FeatureValue, ...]:
        """加载所有 factor_snapshot 的观测并按 ``available_at <= decision_at`` 过滤。"""
        values: list[FeatureValue] = []
        for snapshot_ref in snapshots:
            snapshot = await self.snapshot_provider(snapshot_ref.artifact_id)
            if snapshot is None:
                continue
            for obs in snapshot.observations:
                if obs.available_at > decision_at:
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


async def _load_daily_metrics_features(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    decision_at: datetime,
    release_id: str,
) -> tuple[list[FeatureValue], tuple[str, ...]]:
    """把 daily_metrics 发布观测映射为因子值(PIT 门控,复用 extract_factor_matrix)。

    #252:缺标的容忍语义同 :func:`_load_financial_features`。
    """
    from finboard_data.factors import FactorInputBatch, FactorInputRecord
    from finboard_data.releases import ReleaseCapabilityError
    from finboard_shared.models import Symbol

    semaphore = asyncio.Semaphore(_LOAD_CONCURRENCY)

    async def _one(
        candidate: UniverseCandidate,
    ) -> tuple[FactorInputRecord | None, bool]:
        async with semaphore:
            symbol = Symbol(code=candidate.symbol, market=_market_from_value(candidate.market))
            try:
                records = await provider.fetch_daily_metrics(
                    symbol,
                    start=provider.release.start_date,
                    end=decision_at.date(),
                    decision_at=decision_at,
                )
            except ReleaseCapabilityError:
                return None, True
        if not records:
            return None, False
        # 取决策时点可见的最新一条(同一 trade_date 理论上一条;排序保最新)。
        latest = sorted(records, key=lambda item: item.available_at)[-1]
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
    if not factor_rows:
        return [], tuple(missing)
    batch = FactorInputBatch(records=tuple(factor_rows), source="tushare", dataset_versions={"research_release": "frozen"})
    return _matrix_to_feature_values(batch, release_id=release_id), tuple(missing)


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
    values: list[FeatureValue] = []
    for factor_name, by_symbol in sorted(matrix.items()):
        for symbol, value in sorted(by_symbol.items()):
            values.append(
                FeatureValue(
                    symbol=symbol,
                    feature_id=factor_name,
                    value=float(value),
                    source_artifact_ids=(release_id,),
                    available_at=_latest_available_at(batch, symbol),
                )
            )
    return values


def _latest_available_at(batch: FactorInputBatch, symbol: str) -> datetime:
    """取某标的在横截面中最新的观测时点(研究记录或特征观测)。"""
    candidates: list[datetime] = []
    for record in batch.records:
        if record.symbol != symbol:
            continue
        for item in (record.daily, record.financial):
            if item is not None:
                candidates.append(_require_aware(item.available_at))
    if not candidates:
        return datetime.now(UTC)
    return max(candidates)


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
    切片不再与逐期过滤等价,调用方对该标的回退逐期读取。
    """
    available = tuple(item.available_at for item in points)
    dates = tuple(item.bar.timestamp.date() for item in points)
    closes = tuple(float(item.bar.close) for item in points)
    if any(later < earlier for earlier, later in pairwise(available)) or any(
        later < earlier for earlier, later in pairwise(dates)
    ):
        return None
    return SymbolCloseHistory(available_at=available, dates=dates, closes=closes)


async def _load_close_histories(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    *,
    include_open: bool = False,
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

    async def _one(candidate: UniverseCandidate) -> SymbolCloseHistory:
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
        return SymbolCloseHistory(
            available_at=columns.available_at,
            dates=columns.dates,
            closes=tuple(columns.closes),
            opens=None if columns.opens is None else tuple(columns.opens),
        )

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


async def _load_close_histories_via_pool(
    pool: PriceFeatureProcessPool,
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    *,
    include_open: bool = False,
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
    try:
        results = await asyncio.gather(
            *(
                loop.run_in_executor(executor, _compute_close_history_process_task, task)
                for task in tasks
            ),
            return_exceptions=True,
        )
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
        built[code] = SymbolCloseHistory(
            available_at=columns.available_at,
            dates=columns.dates,
            closes=tuple(columns.closes),
            opens=None if columns.opens is None else tuple(columns.opens),
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


def _build_artifact_ids(manifest: ResearchRunManifest) -> tuple[str, ...]:
    """收集 manifest 冻结的所有 artifact_id(release + snapshot)。"""
    ids: list[str] = [ref.artifact_id for ref in manifest.dataset_releases]
    ids.extend(ref.artifact_id for ref in manifest.factor_snapshots)
    # 去重保序(PortfolioDecisionInput 要求 input_artifact_ids 唯一)。
    seen: set[str] = set()
    unique: list[str] = []
    for artifact_id in ids:
        if artifact_id not in seen:
            seen.add(artifact_id)
            unique.append(artifact_id)
    return tuple(unique)


__all__ = [
    "FeatureSnapshotProvider",
    "FrozenInputLoader",
    "LoadedDecisionContext",
    "ReleaseProviderFactory",
    "SymbolCloseHistory",
]
