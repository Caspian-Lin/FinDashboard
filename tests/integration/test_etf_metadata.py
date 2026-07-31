"""ETF 元数据 Repository 集成测试(issue #97)。

验证:
* upsert 批量写入(新增 / 更新);
* 人工覆盖记录不被自动同步覆盖(manual_override 保护);
* apply_manual_override 写审计流水;
* batch_confirm 审核流转;
* preview_upsert 统计正确性。

需要 PostgreSQL 运行在 127.0.0.1:5432。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_data.assets.classifier import (
    ETF_CLASSIFIER_VERSION,
    EtfClassification,
)
from finboard_persistence import EtfMetadataAuditModel, EtfMetadataRepository
from finboard_shared.types import (
    AssetClass,
    EtfCategory,
    EtfExecutionProfile,
    EtfStrategyType,
    ReviewStatus,
    UnderlyingMarket,
)


def _make_classification(
    code: str = "159010.SZ",
    *,
    profile: EtfExecutionProfile = EtfExecutionProfile.CROSS_BORDER_ETF,
    category: EtfCategory | None = None,
    confidence: Decimal = Decimal("0.9"),
    review: ReviewStatus = ReviewStatus.AUTO_ADOPTED,
) -> EtfClassification:
    if category is None:
        category = {
            EtfExecutionProfile.DOMESTIC_EQUITY_ETF: EtfCategory.EQUITY,
            EtfExecutionProfile.CROSS_BORDER_ETF: EtfCategory.CROSS_BORDER,
            EtfExecutionProfile.BOND_ETF: EtfCategory.BOND,
            EtfExecutionProfile.MONEY_MARKET_ETF: EtfCategory.MONEY_MARKET,
            EtfExecutionProfile.COMMODITY_ETF: EtfCategory.COMMODITY,
        }[profile]
    return EtfClassification(
        code=code,
        execution_profile=profile,
        underlying_asset_class=AssetClass.EQUITY,
        underlying_market=UnderlyingMarket.HK,
        strategy_type=EtfStrategyType.INDEX,
        category=category,
        confidence=confidence,
        review_status=review,
        rule_version=ETF_CLASSIFIER_VERSION,
        evidence=("基金类型「股票指数」→ 权益", "名称含港股 → 港股"),
        tracked_index="恒生科技指数",
    )


@pytest.mark.asyncio
async def test_upsert_inserts_new_classification(db_session: AsyncSession) -> None:
    repo = EtfMetadataRepository(db_session)
    cls = _make_classification(
        "510300.SH",
        profile=EtfExecutionProfile.DOMESTIC_EQUITY_ETF,
        category=EtfCategory.EQUITY,
    )

    inserted, updated, skipped = await repo.upsert_batch([cls])

    assert inserted == 1
    assert updated == 0
    assert skipped == 0
    row = await repo.get("510300.SH")
    assert row is not None
    assert row.execution_profile == "domestic_equity_etf"
    assert row.category == "equity"
    assert row.review_status == "auto_adopted"


@pytest.mark.asyncio
async def test_upsert_updates_existing(db_session: AsyncSession) -> None:
    repo = EtfMetadataRepository(db_session)
    await repo.upsert_batch([_make_classification("159010.SZ")])

    updated_cls = _make_classification("159010.SZ", confidence=Decimal("0.95"))
    inserted, updated, skipped = await repo.upsert_batch([updated_cls])

    assert inserted == 0
    assert updated == 1
    assert skipped == 0
    row = await repo.get("159010.SZ")
    assert row is not None
    assert row.confidence == Decimal("0.95")


@pytest.mark.asyncio
async def test_upsert_skips_manual_override(db_session: AsyncSession) -> None:
    """人工覆盖的记录不被自动同步覆盖(issue #97 核心验收)。"""
    repo = EtfMetadataRepository(db_session)
    await repo.upsert_batch([_make_classification("510300.SH", profile=EtfExecutionProfile.DOMESTIC_EQUITY_ETF)])

    await repo.apply_manual_override(
        "510300.SH",
        execution_profile=EtfExecutionProfile.BOND_ETF,
        reason="人工修正为债券 ETF",
    )

    auto_cls = _make_classification("510300.SH", profile=EtfExecutionProfile.DOMESTIC_EQUITY_ETF)
    _inserted, _updated, skipped = await repo.upsert_batch([auto_cls])

    assert skipped == 1
    row = await repo.get("510300.SH")
    assert row is not None
    assert row.execution_profile == "bond_etf"
    assert row.manual_override is True
    assert row.review_status == "manually_overridden"


@pytest.mark.asyncio
async def test_apply_manual_override_writes_audit(db_session: AsyncSession) -> None:
    repo = EtfMetadataRepository(db_session)
    await repo.upsert_batch([_make_classification("159010.SZ")])

    await repo.apply_manual_override(
        "159010.SZ",
        execution_profile=EtfExecutionProfile.COMMODITY_ETF,
        underlying_market="overseas",
        changed_by="analyst-alice",
        reason="跟踪标的变更为海外商品",
    )

    audits = (
        await db_session.execute(
            select(EtfMetadataAuditModel)
            .where(EtfMetadataAuditModel.code == "159010.SZ")
            .order_by(EtfMetadataAuditModel.changed_at)
        )
    ).scalars().all()
    fields = {a.field_name for a in audits}
    assert "execution_profile" in fields
    assert "underlying_asset_class" in fields
    assert "underlying_market" in fields
    assert all(a.changed_by == "analyst-alice" for a in audits)


@pytest.mark.asyncio
async def test_batch_confirm(db_session: AsyncSession) -> None:
    repo = EtfMetadataRepository(db_session)
    await repo.upsert_batch([
        _make_classification("159001.SZ", review=ReviewStatus.NEEDS_REVIEW),
        _make_classification("159002.SZ", review=ReviewStatus.NEEDS_REVIEW),
        _make_classification("159003.SZ", review=ReviewStatus.AUTO_ADOPTED),
    ])

    confirmed = await repo.batch_confirm(["159001.SZ", "159002.SZ", "159003.SZ"])

    assert confirmed == 2
    for code in ("159001.SZ", "159002.SZ"):
        row = await repo.get(code)
        assert row is not None
        assert row.review_status == "manually_confirmed"
    row3 = await repo.get("159003.SZ")
    assert row3 is not None
    assert row3.review_status == "auto_adopted"


@pytest.mark.asyncio
async def test_preview_upsert(db_session: AsyncSession) -> None:
    repo = EtfMetadataRepository(db_session)
    await repo.upsert_batch([_make_classification("510300.SH")])
    await repo.apply_manual_override(
        "510300.SH", execution_profile=EtfExecutionProfile.BOND_ETF
    )

    preview = await repo.preview_upsert([
        _make_classification("510300.SH"),
        _make_classification("159915.SZ", profile=EtfExecutionProfile.DOMESTIC_EQUITY_ETF),
    ])

    assert preview.total == 2
    assert preview.to_insert == 1
    assert preview.to_update == 0
    assert preview.skipped_override == 1


@pytest.mark.asyncio
async def test_needs_review_blocks_release(db_session: AsyncSession) -> None:
    """needs_review 的 ETF 不能通过发布质量门(fail-closed)。"""
    from finboard_data.releases import ReleaseCapabilityError
    from finboard_persistence import EtfMetadataModel
    from finboard_persistence.dataset_release_repo import _resolve_etf_classification

    db_session.add(EtfMetadataModel(
        code="159999.SZ",
        fund_code="159999",
        category="equity",
        execution_profile="domestic_equity_etf",
        review_status="needs_review",
    ))
    await db_session.flush()

    with pytest.raises(ReleaseCapabilityError, match="待复核"):
        _resolve_etf_classification(
            "159999.SZ",
            EtfMetadataModel(
                code="159999.SZ",
                fund_code="159999",
                category="equity",
                execution_profile="domestic_equity_etf",
                review_status="needs_review",
            ),
        )
