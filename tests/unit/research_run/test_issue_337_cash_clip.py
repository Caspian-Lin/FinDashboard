"""末端买入按可用现金裁剪,优雅降级代替整 run 拒绝(issue #337)。

修复前:sizing 按决策价预算、成交按执行价结算,价差漂移逐笔累积,末端
买入哪怕只超出几元也会触发 `_apply_research_fill` 的「现金为负」fail-closed
检查,整条 run REJECTED(`hard_constraint_rejected`),已落库决策全部陪葬
(RR-26e5640 / r5 / r6 / r7 四次同死)。

锁定三点:
* 执行价高于决策价的漂移下,run 照常 COMPLETED(不再 REJECTED);
* 末端买入被裁剪为部分成交 / 现金不足整单拒单,削减量计入 fill_shortfall;
* 成交后现金恒非负 —— fail-closed 检查保留作账本 bug 兜底。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import numpy as np
import pytest

from finboard_backtest.portfolio import AssetLotInfo, CovarianceEstimate
from finboard_backtest.research_run import (
    DecisionBundle,
    FeatureValue,
    InMemoryResearchRunStore,
    NormalizedSignal,
    ResearchRunCoordinator,
    ResearchRunManifest,
    ResearchRunStatus,
    UniverseCandidate,
)
from finboard_backtest.research_run.portfolio_pipeline import (
    PortfolioDecisionInput,
    PortfolioPipelineAdapter,
    _clip_buy_quantity_to_cash,
)

SYMBOLS = ("A.SH", "B.SH", "C.SH")
LOT = 100


def _clip_helper_checks() -> None:
    """`_clip_buy_quantity_to_cash` 的算术契约(纯函数,同步断言)。"""

    info = AssetLotInfo(code="A.SH", lot_size=LOT)
    # 现金充裕:零削减。
    assert (
        _clip_buy_quantity_to_cash(
            fill_quantity=1000,
            price=Decimal("10"),
            info=info,
            available_cash=Decimal("20000"),
            commission_rate=0.0003,
            commission_min=5.0,
            slippage_bps=5.0,
        )
        == 0
    )
    # 现金 5000:可负担 notional = (5000-5)/1.0008 ≈ 4990 → 400 股(手数取整),
    # 削减 600;成交后成本 = 4000 + max(1.2, 5) + 2 = 4007 <= 5000。
    assert (
        _clip_buy_quantity_to_cash(
            fill_quantity=1000,
            price=Decimal("10"),
            info=info,
            available_cash=Decimal("5000"),
            commission_rate=0.0003,
            commission_min=5.0,
            slippage_bps=5.0,
        )
        == 600
    )
    # 现金不足一手:全额削减(整单拒)。
    assert (
        _clip_buy_quantity_to_cash(
            fill_quantity=1000,
            price=Decimal("10"),
            info=info,
            available_cash=Decimal("50"),
            commission_rate=0.0003,
            commission_min=5.0,
            slippage_bps=5.0,
        )
        == 1000
    )


def _covariance() -> CovarianceEstimate:
    return CovarianceEstimate(
        matrix=np.diag([0.09, 0.01, 0.01]),
        tickers=list(SYMBOLS),
        shrinkage=0.0,
        n_observations=252,
    )


def _manifest(manifest_factory) -> ResearchRunManifest:
    manifest: ResearchRunManifest = manifest_factory(
        run_id="RR-cash-clip-test001",
        idempotency_key="cash-clip-test-0001",
    )
    return replace(
        manifest,
        portfolio_config={
            "overrides": {
                "max_weight_per_asset": 0.50,
                "max_weight_per_sleeve": 1.0,
                "max_risk_contribution": 0.40,
            }
        },
    )


def _input(
    *,
    decision_price: float,
    execution_price: float,
) -> PortfolioDecisionInput:
    """决策价与执行价刻意拉开 25% 漂移:sizing 预算按前者、成交按后者。"""
    day = 2
    decision_at = datetime(2025, 1, day, 15, tzinfo=UTC)
    return PortfolioDecisionInput(
        business_date=date(2025, 1, day),
        decision_at=decision_at,
        execution_at=datetime(2025, 1, day + 1, 9, 30, tzinfo=UTC),
        candidates=tuple(
            UniverseCandidate(
                symbol=symbol,
                included=True,
                reasons=("通过冻结候选池规则",),
                asset_class="equity",
                market="a_share",
            )
            for symbol in SYMBOLS
        ),
        features=tuple(
            FeatureValue(
                symbol=symbol,
                feature_id="close",
                value=decision_price,
                source_artifact_ids=("release-v1",),
                available_at=decision_at,
            )
            for symbol in SYMBOLS
        ),
        signals=tuple(
            NormalizedSignal(
                symbol=symbol,
                score=1.0,
                action="buy",
                rule_id="fixed-research-signal",
                factor_snapshot_id="factor-v1",
                rationale="冻结输入中的标准化多头信号",
            )
            for symbol in SYMBOLS
        ),
        prices=dict.fromkeys(SYMBOLS, decision_price),
        execution_prices=dict.fromkeys(SYMBOLS, execution_price),
        lot_info={symbol: AssetLotInfo(code=symbol, lot_size=LOT) for symbol in SYMBOLS},
        input_artifact_ids=("release-v1", "factor-v1"),
        covariance=_covariance(),
        sleeve_map=dict.fromkeys(SYMBOLS, "equity"),
    )


async def _collect_decisions(
    adapter: PortfolioPipelineAdapter,
    manifest: ResearchRunManifest,
) -> list[DecisionBundle]:
    adapter.validate_manifest(manifest)
    return [decision async for decision in adapter.decisions(manifest)]


@pytest.mark.asyncio
async def test_execution_price_drift_completes_with_clipped_terminal_buy(
    manifest_factory,
) -> None:
    """执行价 +50% 漂移:run 照常完成,末端买入被裁剪且现金恒非负。

    +50% 漂移下成交总额 111k > 初始资金 100k —— 修复前末端买入必然触发
    「现金为负」整 run REJECTED;修复后末端买入被裁剪为部分成交。
    """
    _clip_helper_checks()
    manifest = _manifest(manifest_factory)
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(_input(decision_price=10.0, execution_price=15.0),),
    )
    store = InMemoryResearchRunStore()
    decision = (await _collect_decisions(adapter, manifest))[0]
    record = await ResearchRunCoordinator(store).execute(manifest, adapter)

    assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
    # 现金恒非负(fail-closed 检查仍在,只是不再被正常漂移触发)。
    assert decision.ledger.cash >= 0
    # 至少一笔买入被裁剪:部分成交或现金不足整单拒,削减量计入 shortfall。
    clipped_orders = [
        item
        for item in decision.orders
        if item.status.value == "partially_filled"
        or (
            item.status.value == "rejected"
            and item.reject_reason is not None
            and "现金" in item.reject_reason
        )
    ]
    assert clipped_orders, "预期出现被现金裁剪的末端买入"
    assert decision.ledger.fill_shortfall > 0
    # 裁剪后仍建立了多头头寸(优雅降级,不是放弃整期)。
    assert any(item.quantity > 0 for item in decision.fills)


@pytest.mark.asyncio
async def test_no_drift_keeps_orders_unclipped(manifest_factory) -> None:
    """执行价与决策价一致:零削减,行为与修复前逐值一致。"""
    manifest = _manifest(manifest_factory)
    adapter = PortfolioPipelineAdapter(
        strategy_kind="ma_cross",
        decision_inputs=(_input(decision_price=10.0, execution_price=10.0),),
    )
    decision = (await _collect_decisions(adapter, manifest))[0]
    assert decision.ledger.cash >= 0
    assert all(
        item.reject_reason is None or "现金" not in item.reject_reason for item in decision.orders
    )
