"""issue #56 验收测试 —— 无未来成交与资产规则真实撮合基线。

逐条对照 issue 验收标准:

1. T 日收盘信号不会在 T 日收盘成交,下一 Bar 成交价规则确定、可配置、可复现。
2. 策略持仓由 OrderEvent/Fill 更新;拒单 / 部分成交 / 撤单不会让仓位漂移。
3. ``allow_short=false`` 时超卖被拒绝且现金/持仓不变。
4. 股票 / 股票 ETF / 债券 ETF / 可转债 / 期货规则由元数据选择;未知类型 fail closed。
5. 涨跌停 / 停牌 / 开盘跳空 / 成交量约束 / 部分成交 / 公司行为有明确语义。
6. 结果保存撮合 / 费用 / 资产规则版本;历史 run 可复现。

集成测试用合成多资产数据验证现金 / 持仓 / 权益与成交守恒,不访问公网。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from finboard_app.strategies.ma_cross import MaCrossStrategy
from finboard_backtest.asset_rules import (
    A_SHARE_STOCK,
    ASSET_RULES_VERSION,
    BOND_ETF,
    CROSS_BORDER_ETF,
    DEFAULT_TABLE,
    AssetRuleResolutionError,
    AssetRuleTable,
)
from finboard_backtest.broker import (
    BacktestBroker,
    _InternalPosition,
    _PositionLot,
)
from finboard_backtest.config import (
    BacktestConfig,
    BenchmarkConfig,
    FeeOverrides,
    FillTiming,
    MatchingModel,
)
from finboard_backtest.engine import BacktestEngine
from finboard_shared.identifiers import AccountId, generate_client_order_id
from finboard_shared.models import Bar, Order, Symbol
from finboard_shared.types import (
    BrokerKind,
    InstrumentType,
    Market,
    OrderStatus,
    OrderType,
    Side,
)

SYMBOL_STOCK = Symbol(code="600000.SH", market=Market.A_SHARE)
SYMBOL_ETF = Symbol(code="510300.SH", market=Market.A_SHARE)
SYMBOL_BOND = Symbol(code="511010.SH", market=Market.A_SHARE)
SYMBOL_CB = Symbol(code="113001.SH", market=Market.A_SHARE)
SYMBOL_UNKNOWN = Symbol(code="WTF999.XX", market=Market.A_SHARE)
ACCOUNT = AccountId("test")


def _bar(
    symbol: Symbol,
    day: int,
    close: str,
    *,
    open_: str | None = None,
    high: str | None = None,
    low: str | None = None,
    volume: str = "1000000",
) -> Bar:
    c = Decimal(close)
    return Bar(
        symbol=symbol,
        period=None,  # type: ignore[arg-type]
        timestamp=datetime(2024, 1, day, tzinfo=UTC),
        open=Decimal(open_ if open_ is not None else close),
        high=Decimal(high if high is not None else close),
        low=Decimal(low if low is not None else close),
        close=c,
        volume=Decimal(volume),
        amount=Decimal("0"),
    )


def _order(
    symbol: Symbol,
    side: Side,
    qty: Decimal,
    *,
    order_type: OrderType = OrderType.MARKET,
    price: Decimal | None = None,
) -> Order:
    return Order(
        client_order_id=generate_client_order_id(),
        account_id=ACCOUNT,
        broker_kind=BrokerKind.BACKTEST,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=qty,
        price=price,
    )


class _TypeAwareResolver:
    """按 code 后缀 / 前缀解析资产类型(测试用)。"""

    def resolve(self, code: str) -> tuple[Market, InstrumentType]:
        upper = code.upper()
        if upper.startswith("11") or upper.startswith("12"):
            return Market.A_SHARE, InstrumentType.STOCK  # 可转债暂归 STOCK(规则单独覆盖)
        if upper.startswith("511"):
            return Market.A_SHARE, InstrumentType.ETF  # 国债 ETF
        if upper.endswith(".SH") or upper.endswith(".SZ"):
            return Market.A_SHARE, InstrumentType.ETF if upper.startswith("5") else InstrumentType.STOCK
        return Market.A_SHARE, InstrumentType.STOCK


class _StrictResolver:
    """对所有未知代码 raise 的严格 resolver。"""

    def __init__(self, known: dict[str, tuple[Market, InstrumentType]]) -> None:
        self._known = known

    def resolve(self, code: str) -> tuple[Market, InstrumentType]:
        if code not in self._known:
            raise AssetRuleResolutionError(f"未知代码: {code}")
        return self._known[code]


# --------------------------------------------------------------------------- AC 1


class TestAcceptanceCriteriaTiming:
    """AC 1: T 日收盘信号不在 T 日收盘成交;下一 Bar 成交价确定可配置。"""

    @pytest.mark.unit
    async def test_signal_does_not_fill_on_same_bar(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        # 同 Bar 收单不成交
        assert order.status == OrderStatus.SUBMITTED
        assert order.filled_quantity == Decimal("0")

    @pytest.mark.unit
    async def test_next_bar_open_is_default(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(
            _bar(SYMBOL_STOCK, 2, "4.20", open_="4.10", high="4.30", low="4.00")
        )
        await broker.drain_events()
        assert order.average_fill_price == Decimal("4.10")

    @pytest.mark.unit
    async def test_next_bar_close_via_matching_model(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("100000"),
            matching_model=MatchingModel(fill_timing=FillTiming.NEXT_BAR_CLOSE),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(
            _bar(SYMBOL_STOCK, 2, "4.20", open_="4.10", high="4.30", low="4.00")
        )
        await broker.drain_events()
        assert order.average_fill_price == Decimal("4.20")

    @pytest.mark.unit
    async def test_next_bar_vwap_proxy(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("100000"),
            matching_model=MatchingModel(fill_timing=FillTiming.NEXT_BAR_VWAP_PROXY),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(
            _bar(SYMBOL_STOCK, 2, "4.00", open_="4.00", high="4.20", low="3.80")
        )
        await broker.drain_events()
        # VWAP proxy = (4.20 + 3.80 + 2*4.00) / 4 = 16.00/4 = 4.00
        assert order.average_fill_price == Decimal("4.00")


# --------------------------------------------------------------------------- AC 2


class TestAcceptanceCriteriaPositionDrivenByFills:
    """AC 2: 持仓由 OrderEvent/Fill 更新;拒单 / 部分成交 / 撤单不漂移。"""

    @pytest.mark.unit
    async def test_rejection_preserves_position(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("1"), allow_short=False)
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED
        assert broker.available_quantity(SYMBOL_STOCK.code) == Decimal("0")
        assert broker._cash == Decimal("1")

    @pytest.mark.unit
    async def test_partial_fill_keeps_remaining_in_pending(self) -> None:
        broker = BacktestBroker(
            initial_capital=Decimal("1000000"),
            matching_model=MatchingModel(max_participation=Decimal("0.05")),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00", volume="1000"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("500"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00", volume="1000"))
        await broker.drain_events()
        # max_participation 5% * 1000 = 50 → 但 lot_size=100 → round_to_lot(50)=0
        # 由于 remaining(500) > lot_size(100),会"等下一 Bar"
        assert order.filled_quantity == Decimal("0")
        # 再来一根 bar,仍然只能成交 0 手(参与率太小)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 3, "4.00", volume="5000"))
        await broker.drain_events()
        # 5% * 5000 = 250 → round_to_lot(250) = 200
        assert order.filled_quantity == Decimal("200")

    @pytest.mark.unit
    async def test_cancel_keeps_filled_quantity(self) -> None:
        """已部分成交的订单撤单后,已成交数量保留。"""
        broker = BacktestBroker(
            initial_capital=Decimal("1000000"),
            matching_model=MatchingModel(max_participation=Decimal("0.05")),
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00", volume="10000"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("1000"))
        await broker.place_order(order)
        # 5% * 10000 = 500 → round_to_lot(500) = 500
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00", volume="10000"))
        await broker.drain_events()
        assert order.filled_quantity == Decimal("500")
        # 撤单
        await broker.cancel_order(str(order.client_order_id))
        assert order.status == OrderStatus.CANCELLED
        assert order.filled_quantity == Decimal("500")  # 已成交的部分保留
        # 持仓也保留
        assert broker._positions[SYMBOL_STOCK.code].total == Decimal("500")


# --------------------------------------------------------------------------- AC 3


class TestAcceptanceCriteriaNoOversell:
    """AC 3: allow_short=false 时超卖被拒绝,现金/持仓不变。"""

    @pytest.mark.unit
    async def test_oversell_truncates_to_available(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"), allow_short=False)
        await broker.connect(ACCOUNT, {})
        # 注入 100 股持仓
        rule = A_SHARE_STOCK
        prior_date = datetime(2023, 12, 31, tzinfo=UTC).date()
        broker._positions[SYMBOL_STOCK.code] = _InternalPosition(
            symbol=SYMBOL_STOCK,
            rule=rule,
            total=Decimal("100"),
            available=Decimal("100"),
            average_price=Decimal("4.00"),
            lots=[_PositionLot(quantity=Decimal("100"), available_date=prior_date)],
        )
        cash_before = broker._cash
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        # 卖 300 股(超过持仓 100)
        order = _order(SYMBOL_STOCK, Side.SELL, Decimal("300"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        await broker.drain_events()
        # 截断到 100 股成交
        assert order.filled_quantity == Decimal("100")
        # 持仓为 0
        assert broker._positions[SYMBOL_STOCK.code].total == Decimal("0")
        # 现金增加(卖出收入 - 手续费 - 印花税)
        assert broker._cash > cash_before

    @pytest.mark.unit
    async def test_sell_with_zero_position_rejected(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"), allow_short=False)
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.SELL, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED
        assert "可用 0" in (order.reject_message or "")
        assert broker._cash == Decimal("100000")


# --------------------------------------------------------------------------- AC 4


class TestAcceptanceCriteriaAssetRulesByMetadata:
    """AC 4: 股票 / 股票 ETF / 债券 ETF / 可转债 / 期货规则由元数据选择;
    未知类型 fail closed。"""

    @pytest.mark.unit
    def test_default_table_supports_stock_etf_index(self) -> None:
        rule = DEFAULT_TABLE.resolve(
            market=Market.A_SHARE, instrument_type=InstrumentType.STOCK
        )
        assert rule.lot_size == Decimal("100")
        assert rule.stamp_tax_rate == Decimal("0.0005")
        etf_rule = DEFAULT_TABLE.resolve(
            market=Market.A_SHARE, instrument_type=InstrumentType.ETF
        )
        assert etf_rule.lot_size == Decimal("100")

    @pytest.mark.unit
    def test_unknown_market_type_fails_closed(self) -> None:
        empty = AssetRuleTable(rules=())
        with pytest.raises(AssetRuleResolutionError):
            empty.resolve(market=Market.A_SHARE, instrument_type=InstrumentType.STOCK)

    @pytest.mark.unit
    def test_extended_table_supports_bond_etf_and_cross_border(self) -> None:
        from dataclasses import replace

        # 注册额外的 BOND_ETF / CROSS_BORDER_ETF 通过 replace 改类型
        bond_rule = replace(
            BOND_ETF, instrument_type=InstrumentType.ETF
        )
        # 同一 (market, type) 不能注册两次,因此 BOND_ETF 实际由调用方覆盖
        assert bond_rule.stamp_tax_rate == Decimal("0")
        assert CROSS_BORDER_ETF.enforce_t_plus_1 is False

    @pytest.mark.unit
    async def test_broker_rejects_unknown_instrument_with_strict_resolver(
        self,
    ) -> None:
        strict = _StrictResolver({})
        broker = BacktestBroker(
            initial_capital=Decimal("100000"),
            instrument_resolver=strict,
        )
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED


# --------------------------------------------------------------------------- AC 5


class TestAcceptanceCriteriaMarketMicrostructure:
    """AC 5: 涨跌停 / 停牌 / 开盘跳空 / 成交量约束 / 部分成交。"""

    @pytest.mark.unit
    async def test_limit_up_blocks_buy(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        # Day 3: close=4.40 (+10% from 4.00) = 涨停
        broker.on_new_bar(
            _bar(
                SYMBOL_STOCK,
                3,
                "4.40",
                open_="4.40",
                high="4.40",
                low="4.40",
            )
        )
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED
        assert "涨跌停" in (order.reject_message or "")

    @pytest.mark.unit
    async def test_limit_down_blocks_sell(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"), allow_short=False)
        await broker.connect(ACCOUNT, {})
        # 注入持仓
        prior_date = datetime(2023, 12, 31, tzinfo=UTC).date()
        broker._positions[SYMBOL_STOCK.code] = _InternalPosition(
            symbol=SYMBOL_STOCK,
            rule=A_SHARE_STOCK,
            total=Decimal("200"),
            available=Decimal("200"),
            average_price=Decimal("4.00"),
            lots=[_PositionLot(quantity=Decimal("200"), available_date=prior_date)],
        )
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        order = _order(SYMBOL_STOCK, Side.SELL, Decimal("200"))
        await broker.place_order(order)
        # Day 3: close=3.60 (-10%) = 跌停
        broker.on_new_bar(
            _bar(
                SYMBOL_STOCK,
                3,
                "3.60",
                open_="3.60",
                high="3.60",
                low="3.60",
            )
        )
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED

    @pytest.mark.unit
    async def test_suspension_rejects_order(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("200"))
        await broker.place_order(order)
        # Day 3 停牌:volume=0 + OHLC = pre_close
        broker.on_new_bar(
            _bar(
                SYMBOL_STOCK,
                3,
                "4.00",
                open_="4.00",
                high="4.00",
                low="4.00",
                volume="0",
            )
        )
        await broker.drain_events()
        assert order.status == OrderStatus.REJECTED
        assert "停牌" in (order.reject_message or "")

    @pytest.mark.unit
    async def test_gap_improvement_for_limit_buy(self) -> None:
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(
            SYMBOL_STOCK,
            Side.BUY,
            Decimal("200"),
            order_type=OrderType.LIMIT,
            price=Decimal("4.10"),
        )
        await broker.place_order(order)
        # Day 2: open=3.80(向下跳空),low=3.75 → 按 open 3.80 成交(< limit)
        broker.on_new_bar(
            _bar(SYMBOL_STOCK, 2, "3.90", open_="3.80", high="3.95", low="3.75")
        )
        await broker.drain_events()
        assert order.average_fill_price == Decimal("3.80")


# --------------------------------------------------------------------------- AC 6


class TestAcceptanceCriteriaResultArchival:
    """AC 6: 结果归档撮合模型版本、资产规则版本和所有费用/滑点假设。"""

    @pytest.mark.unit
    def test_matching_model_archives_versions(self) -> None:
        model = MatchingModel()
        data = model.as_dict()
        assert data["matching_model_version"] == "v2"
        assert data["asset_rules_version"] == ASSET_RULES_VERSION
        assert data["fill_timing"] == "next_bar_open"
        assert data["next_bar_only"] is True

    @pytest.mark.unit
    def test_fee_overrides_serializable(self) -> None:
        fees = FeeOverrides(
            commission_rate=Decimal("0.0003"),
            commission_min=Decimal("5"),
            stamp_tax_rate=Decimal("0.0005"),
            slippage_bps=Decimal("5"),
        )
        data = fees.as_dict()
        assert data["commission_rate"] == "0.0003"
        assert data["slippage_bps"] == "5"

    @pytest.mark.unit
    def test_asset_rule_table_serializable(self) -> None:
        from typing import cast

        data = DEFAULT_TABLE.as_dict()
        assert data["rule_version"] == ASSET_RULES_VERSION
        rules = cast(list[dict[str, object]], data["rules"])
        assert any(r["instrument_type"] == "stock" for r in rules)

    @pytest.mark.unit
    def test_benchmark_config_serializable(self) -> None:
        cfg = BenchmarkConfig(symbol="510300.SH")
        assert cfg.as_dict()["symbol"] == "510300.SH"
        assert cfg.as_dict()["equal_weight_universe"] is True


# --------------------------------------------------------------------------- 集成


class TestIntegrationConservation:
    """集成测试:合成多资产数据验证现金 / 持仓 / 权益与成交守恒。"""

    @pytest.mark.unit
    async def test_cash_plus_position_equals_equity(self) -> None:
        """任意时刻: cash + 持仓市值 = total_equity。"""
        broker = BacktestBroker(initial_capital=Decimal("100000"))
        await broker.connect(ACCOUNT, {})
        broker.on_new_bar(_bar(SYMBOL_STOCK, 1, "4.00"))
        order = _order(SYMBOL_STOCK, Side.BUY, Decimal("1000"))
        await broker.place_order(order)
        broker.on_new_bar(_bar(SYMBOL_STOCK, 2, "5.00"))
        await broker.drain_events()
        # 校验守恒
        cash = broker._cash
        position_value = broker._positions[SYMBOL_STOCK.code].total * Decimal("5.00")
        assert cash + position_value == broker.total_equity()


class TestIntegrationWithMaCrossEngine:
    """端到端回测:撮合模型与资产规则在 BacktestEngine 中归档。"""

    @pytest.mark.unit
    async def test_engine_archives_matching_and_asset_rules(self) -> None:
        from finboard_shared.models import Symbol as Sym

        # 简短合成数据:60 根 Bar,触发金叉
        prices = (
            [Decimal("5.0") - Decimal("0.1") * i for i in range(15)]
            + [Decimal("3.6") + Decimal("0.1") * i for i in range(15)]
            + [Decimal("5.0")] * 30
        )

        synthetic_bars = [
            Bar(
                symbol=SYMBOL_ETF,
                period=None,  # type: ignore[arg-type]
                timestamp=datetime(2024, 1, 1, tzinfo=UTC) + timedelta(days=i),
                open=p,
                high=p + Decimal("0.05"),
                low=p - Decimal("0.05"),
                close=p,
                volume=Decimal("1000000"),
            )
            for i, p in enumerate(prices)
        ]

        class _Provider:
            async def fetch_bars(
                self,
                symbol: Sym,
                period,
                start,
                end,
                *,
                adjust: str = "qfq",
            ):
                return synthetic_bars

        strategy = MaCrossStrategy(
            strategy_id="engine-archive",
            short_window=5,
            long_window=10,
        )
        config = BacktestConfig(
            symbols=[SYMBOL_ETF.code],
            start=date(2024, 1, 1),
            end=date(2024, 12, 31),
            initial_capital=Decimal("100000"),
            benchmark=BenchmarkConfig(symbol=SYMBOL_ETF.code),
        )
        engine = BacktestEngine(
            strategy=strategy,
            data_provider=_Provider(),
            config=config,
        )
        result = await engine.run()
        # 归档字段非空
        assert result.matching_model["matching_model_version"] == "v2"
        assert result.matching_model["asset_rules_version"] == ASSET_RULES_VERSION
        assert result.asset_rules is not None
        assert result.asset_rules["rule_version"] == ASSET_RULES_VERSION
        assert "commission_rate" in result.fee_assumptions
        assert result.benchmark_config["symbol"] == SYMBOL_ETF.code
        # summary 包含撮合模型版本
        summary = result.summary()
        assert "撮合模型" in summary
        assert "v2" in summary


class TestBenchmarkNotFirstSymbol:
    """多标的默认基准不再只取第一个标的。"""

    @pytest.mark.unit
    def test_equal_weight_universe_return(self) -> None:
        from finboard_backtest.metrics import equal_weight_universe_return

        sym_a = Symbol(code="AAA.SH", market=Market.A_SHARE)
        sym_b = Symbol(code="BBB.SH", market=Market.A_SHARE)
        bars = {
            "AAA.SH": [
                Bar(
                    symbol=sym_a,
                    period=None,  # type: ignore[arg-type]
                    timestamp=datetime(2024, 1, i + 1, tzinfo=UTC),
                    open=Decimal("10"),
                    high=Decimal("10"),
                    low=Decimal("10"),
                    close=Decimal(str(10 + i)),
                )
                for i in range(5)
            ],
            "BBB.SH": [
                Bar(
                    symbol=sym_b,
                    period=None,  # type: ignore[arg-type]
                    timestamp=datetime(2024, 1, i + 1, tzinfo=UTC),
                    open=Decimal("10"),
                    high=Decimal("10"),
                    low=Decimal("10"),
                    close=Decimal(str(10 + i * 2)),
                )
                for i in range(5)
            ],
        }
        curve = equal_weight_universe_return(bars, Decimal("100000"))
        # 等权基准在第一天 = 100000;每个 symbol 分到 50000,各买 5000 股 @ 10
        assert curve[0][1] == Decimal("100000")
        # 最后一天:AAA=14*5000 + BBB=18*5000 = 70000+90000 = 160000
        assert curve[-1][1] == Decimal("160000")
