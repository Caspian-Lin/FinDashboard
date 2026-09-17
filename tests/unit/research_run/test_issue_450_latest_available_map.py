"""issue #450 追续:``_matrix_to_feature_values`` 的 available_at 预聚合等值。

此前逐 (因子 x 标的) 调 ``_latest_available_at`` 全量扫描 records——矩阵键数
x 记录数 = 全市场发布下每期数亿次比较(加载期主导热点)。修复为一次遍历
预聚合 symbol → max(available_at) 映射后,输出必须与逐标的参照实现逐值一致:
daily/financial 并列取 max、多记录同标的取 max、naive 时戳补 UTC。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from finboard_backtest.research_run.frozen_loader import _matrix_to_feature_values
from finboard_data.factors import FactorInputBatch, FactorInputRecord
from finboard_data.research import DailySecurityMetrics, FinancialIndicator


def _daily(symbol: str, available_at: datetime, pb: float) -> DailySecurityMetrics:
    return DailySecurityMetrics(
        symbol=symbol,
        trade_date=available_at.date(),
        close=Decimal("10"),
        turnover_rate=None,
        turnover_rate_free=None,
        volume_ratio=None,
        pe=None,
        pe_ttm=None,
        pb=Decimal(str(pb)),
        ps=None,
        ps_ttm=None,
        dividend_yield=None,
        dividend_yield_ttm=None,
        total_shares=None,
        float_shares=None,
        free_shares=None,
        total_market_cap=None,
        circulating_market_cap=None,
        limit_status=None,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )


def _financial(
    symbol: str, available_at: datetime, roe: float
) -> FinancialIndicator:
    return FinancialIndicator(
        symbol=symbol,
        announcement_date=available_at.date(),
        report_period=available_at.date(),
        update_flag=None,
        eps=None,
        diluted_eps=None,
        book_value_per_share=None,
        operating_cash_flow_per_share=None,
        return_on_equity=Decimal(str(roe)),
        weighted_return_on_equity=None,
        gross_profit_margin=None,
        net_profit_margin=None,
        debt_to_assets=None,
        revenue_yoy=None,
        net_profit_yoy=None,
        operating_cash_flow_yoy=None,
        source="tushare",
        observed_at=available_at,
        available_at=available_at,
    )


def _reference_latest(batch: FactorInputBatch, symbol: str) -> datetime:
    """修复前的逐调用全量扫描参照实现(与原 _latest_available_at 逐值等价)。"""
    candidates: list[datetime] = []
    for record in batch.records:
        if record.symbol != symbol:
            continue
        for item in (record.daily, record.financial):
            if item is not None:
                value = item.available_at
                if value.tzinfo is None:
                    value = value.replace(tzinfo=UTC)
                candidates.append(value)
    if not candidates:
        raise AssertionError("参照实现只覆盖有观测的标的")
    return max(candidates)


def _records() -> tuple[FactorInputRecord, ...]:
    early = datetime(2022, 3, 1, 15, 30, tzinfo=UTC)
    late = datetime(2022, 6, 1, 15, 30, tzinfo=UTC)
    naive = datetime(2022, 5, 1, 15, 30)
    return (
        FactorInputRecord(
            symbol="000001.SZ",
            profile=None,
            daily=_daily("000001.SZ", early, 3.0),
            financial=None,
            industry=None,
        ),
        # 同标的多记录:financial 晚于 daily 的观测点,取 max。
        FactorInputRecord(
            symbol="000002.SZ",
            profile=None,
            daily=_daily("000002.SZ", late, 4.0),
            financial=_financial("000002.SZ", early, 11.0),
            industry=None,
        ),
        # naive available_at 按 _require_aware 语义补 UTC。
        FactorInputRecord(
            symbol="000006.SZ",
            profile=None,
            daily=_daily("000006.SZ", naive, 5.0),
            financial=_financial("000006.SZ", naive, 12.0),
            industry=None,
        ),
    )


def test_available_at_map_equivalent_to_reference_scan() -> None:
    batch = FactorInputBatch(
        records=_records(), source="tushare", dataset_versions={"r": "frozen"}
    )
    values = _matrix_to_feature_values(batch, release_id="rel-x")
    assert values, "必须产出因子观测"
    by_key = {(item.feature_id, item.symbol): item for item in values}
    assert {key[0] for key in by_key} >= {"pb", "roe"}
    for (feature_id, symbol), item in by_key.items():
        expected = _reference_latest(batch, symbol)
        assert item.available_at == expected, (feature_id, symbol)
        assert item.available_at.tzinfo is not None


def test_empty_batch_returns_empty() -> None:
    batch = FactorInputBatch(records=(), source="tushare", dataset_versions={"r": "frozen"})
    assert _matrix_to_feature_values(batch, release_id="rel-x") == []


def test_map_scales_linearly() -> None:
    """规模冒烟:5000 记录全因子提取应秒级完成(O(n²) 回归哨兵)。"""
    import time

    at = datetime(2022, 6, 1, 15, 30, tzinfo=UTC)
    records = tuple(
        FactorInputRecord(
            symbol=f"{600000 + i}.SH",
            profile=None,
            daily=_daily(f"{600000 + i}.SH", at, 1.0),
            financial=None,
            industry=None,
        )
        for i in range(5000)
    )
    batch = FactorInputBatch(
        records=records, source="tushare", dataset_versions={"r": "frozen"}
    )
    started = time.perf_counter()
    values = _matrix_to_feature_values(batch, release_id="rel-x")
    elapsed = time.perf_counter() - started
    assert len(values) >= 5000
    assert elapsed < 10.0, f"5000 标的耗时 {elapsed:.1f}s——疑似 O(n²) 回归"
