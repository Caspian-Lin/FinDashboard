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

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
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
    from finboard_data.factor_lab import FeatureSnapshot
    from finboard_data.factors import FactorInputBatch
    from finboard_data.releases import (
        FrozenReleaseProvider,
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
    """

    release_provider_factory: ReleaseProviderFactory
    snapshot_provider: FeatureSnapshotProvider

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
        * 按 ``decision_at`` 做 PIT 门控取决策日 close;``execution_at`` 取成交日
          close(必须是 decision_at 之后的下一交易日,由调用方保证)。
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
            list(release.instruments)
        )
        prices = await _load_close_prices(provider, included_candidates, decision_at)
        execution_prices = await _load_close_prices(
            provider, included_candidates, execution_at
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

    factor_rows: list[FactorInputRecord] = []
    missing: list[str] = []
    for candidate in candidates:
        symbol = Symbol(code=candidate.symbol, market=_market_from_value(candidate.market))
        try:
            records = await provider.fetch_daily_metrics(
                symbol,
                start=provider.release.start_date,
                end=decision_at.date(),
                decision_at=decision_at,
            )
        except ReleaseCapabilityError:
            missing.append(candidate.symbol)
            continue
        if not records:
            continue
        # 取决策时点可见的最新一条(同一 trade_date 理论上一条;排序保最新)。
        latest = sorted(records, key=lambda item: item.available_at)[-1]
        factor_rows.append(
            FactorInputRecord(
                symbol=candidate.symbol,
                profile=None,
                daily=latest,
                financial=None,
                industry=None,
            )
        )
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

    factor_rows: list[FactorInputRecord] = []
    missing: list[str] = []
    for candidate in candidates:
        symbol = Symbol(code=candidate.symbol, market=_market_from_value(candidate.market))
        try:
            records = await provider.fetch_financial_indicators(
                symbol,
                decision_at=decision_at,
            )
        except ReleaseCapabilityError:
            # 标的不在该研究发布中:计入缺失,继续其余标的( bars 主发布缺
            # 标的仍由候选构建 fail-closed,语义见 factor_lab #212 注释)。
            missing.append(candidate.symbol)
            continue
        if not records:
            continue
        # 同一 report_period 保留公告修订(update_flag);跨期取最新公告的一期。
        by_period: dict[date, FinancialIndicator] = {}
        for item in records:
            previous = by_period.get(item.report_period)
            if previous is None or item.available_at > previous.available_at:
                by_period[item.report_period] = item
        latest = max(by_period.values(), key=lambda item: item.available_at)
        factor_rows.append(
            FactorInputRecord(
                symbol=candidate.symbol,
                profile=None,
                daily=None,
                financial=latest,
                industry=None,
            )
        )
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
    """从发布 instruments 构造候选池(全部 included)与执行元数据映射。"""
    candidates: list[UniverseCandidate] = []
    lot_info: dict[str, AssetLotInfo] = {}
    for inst in instruments:
        if not inst.ready:
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


async def _load_close_prices(
    provider: FrozenReleaseProvider,
    candidates: Sequence[UniverseCandidate],
    as_of: datetime,
) -> dict[str, float]:
    """PIT 门控读取各标的在 ``as_of`` 时点可见的最新 close 价。"""
    # 延迟导入避免顶层依赖 finboard_shared.models 的循环引用。
    from finboard_shared.models import Symbol

    prices: dict[str, float] = {}
    for candidate in candidates:
        market = _market_from_value(candidate.market)
        symbol = Symbol(code=candidate.symbol, market=market)
        bars = await provider.fetch_point_in_time_bars(
            symbol,
            provider.release.period,
            provider.release.start_date,
            as_of.date(),
            decision_at=as_of,
            adjust=provider.release.adjustment,
        )
        if not bars:
            continue
        prices[candidate.symbol] = float(bars[-1].bar.close)
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
]
