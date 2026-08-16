"""冻结产物 → 组合流水线机械输入加载器(issue #143)。

把 ``ResearchRunManifest`` 冻结的 dataset_releases / factor_snapshots 引用加载成
``PortfolioDecisionInput`` 的**机械字段**(价格 / 执行元数据 / 候选池 / 特征 /
artifact 绑定),供 ``PortfolioPipelineAdapter`` 使用。

边界:**信号(``NormalizedSignal``)不在本加载器生成**。现有策略实现是事件驱动实盘
``Strategy``(消费 ``MarketDataEvent`` → ``ctx.submit_order``),没有「OHLCV → 批量
信号」的离线函数;每个策略的信号逻辑需要独立设计(属于策略实现工作,非队列迁移)。
因此本加载器产出 :class:`LoadedDecisionContext`(机械字段),由调用方(真实适配器
工厂)补充信号后再构造完整的 ``PortfolioDecisionInput``。本期 worker 端到端测试用
固定样本 :class:`~finboard_backtest.research_run.adapters.DecisionSequenceAdapter`,
不走本加载器。

复用的现成服务(无需新写 Parquet/DB 读取):
* ``FrozenReleaseProvider.fetch_point_in_time_bars`` —— PIT 门控行情读取
* ``FeatureSnapshotRepository.get`` —— 因子快照反序列化(JSON-in-DB)
* ``ResearchDatasetRelease.instruments`` —— 候选元数据 / 执行规则

不连 broker / 不下实盘单 / 不修改持仓。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Protocol

from finboard_backtest.portfolio.contracts import AssetLotInfo
from finboard_backtest.research_run.contracts import (
    FeatureValue,
    FrozenArtifactRef,
    ResearchRunManifest,
    UniverseCandidate,
)

if TYPE_CHECKING:
    from finboard_data.factor_lab import FeatureSnapshot
    from finboard_data.releases import FrozenReleaseProvider, ReleasedInstrument
    from finboard_shared.types import Market


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

        * 取 manifest.dataset_releases 的首个 release 作为行情 / 候选来源
          (多 release 融合留作后续)。
        * 按 ``decision_at`` 做 PIT 门控取决策日 close;``execution_at`` 取成交日
          close(必须是 decision_at 之后的下一交易日,由调用方保证)。
        * 加载 manifest.factor_snapshots 的特征观测并映射为 ``FeatureValue``。
        """
        if not manifest.dataset_releases:
            raise ValueError("manifest 必须冻结至少一个数据发布")
        if execution_at <= decision_at:
            raise ValueError("execution_at 必须晚于 decision_at")
        release_ref = manifest.dataset_releases[0]
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
        )

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
