"""issue #402:三表 / dividend 公告类发布经窗口挂载的预置因子通道 E2E 单测。

stub provider(对齐 FrozenReleaseProvider 最小面,**真实领域记录**)→
真实 ``build_window_data_mount``(income/balance/cashflow/dividends 四新
kind 进挂载,文件名 ``<kind>.parquet``)→ ``run_predefined_factor_series``:

* ``val_*`` 因子经 ``research_dataset`` 取数:与独立参考逐值对照 +
  公告 PIT 边界(公告前不可见 / 修订后可见);
* ``val_dps_ttm`` / ``qmj_payout`` 经 ``dividend_events`` 取数(除权除息
  日对齐),并验证 benchmark-only 标的从截面剔除(#380)而时序比值保留;
* 未挂载 kind → 空映射;未知字段 → 具名拒绝;
* ``filter_window_data_mount`` 对新 kind 的审计变体(截断防线 + 行子集)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from finboard_backtest.factors.predefined import (
    get_predefined_factor,
    predefined_factor_commit,
)
from finboard_backtest.research_sandbox.data_mount import (
    build_window_data_mount,
    filter_window_data_mount,
)
from finboard_backtest.research_sandbox.predefined_runner import (
    run_predefined_factor_series,
)
from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec
from finboard_data.research import (
    BalanceSheet,
    CashflowStatement,
    DividendRecord,
    IncomeStatement,
)

SYMBOL = "600000.SH"
INDEX = "000300.SH"
BARS_RELEASE = "DR-bars-402"

_DECISIONS = tuple(date(2024, 5, 8) + timedelta(days=k) for k in range(3))


def _anchor(day: date) -> datetime:
    return datetime.combine(
        day + timedelta(days=1), datetime.min.time(), tzinfo=UTC
    )


def _record(
    cls: Any, symbol: str, announcement: date, period: date, **overrides: Any
) -> Any:
    """真实领域记录构造:未覆盖字段填 None(身份 + PIT 字段必填)。"""
    import dataclasses

    values: dict[str, Any] = {
        f.name: None for f in dataclasses.fields(cls)
    }
    values.update(
        symbol=symbol,
        announcement_date=announcement,
        report_period=period,
        source="tushare",
        observed_at=_anchor(announcement),
        available_at=_anchor(announcement),
    )
    values.update(overrides)
    return cls(**values)


@dataclass
class _Inst:
    code: str
    market: str = "SH"
    instrument_type: str = "stock"


@dataclass
class _Sym:
    code: str


@dataclass
class _Bar:
    symbol: _Sym
    timestamp: datetime
    open: float = 9.0
    high: float = 11.0
    low: float = 8.0
    close: float = 10.0
    volume: float = 100.0
    amount: float = 1000.0


@dataclass
class _PITBar:
    code: str
    day: date
    close: float

    @property
    def available_at(self) -> datetime:
        return datetime(
            self.day.year, self.day.month, self.day.day, 15, 30, tzinfo=UTC
        )

    @property
    def bar(self) -> _Bar:
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=datetime.combine(self.day, datetime.min.time()),
            close=self.close,
        )


@dataclass
class _Kind:
    value: str


@dataclass
class _Release:
    release_id: str
    dataset_kind: Any
    instruments: list[_Inst] = field(default_factory=list)
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2023, 6, 1)
    end_date: date = date(2024, 12, 31)


@dataclass
class _DailyRecord:
    """daily_metrics stub(_collect_daily_metrics 消费面)。"""

    symbol: str
    trade_date: date
    close: float | None = None
    total_market_cap: float | None = None
    circulating_market_cap: float | None = None
    source: str = "tushare"
    observed_at: datetime | None = None
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        anchor = datetime.combine(
            self.trade_date, datetime.min.time(), tzinfo=UTC
        ).replace(hour=16)
        if self.available_at is None:
            self.available_at = anchor
        if self.observed_at is None:
            self.observed_at = anchor


@dataclass
class _StubProvider:
    release: _Release
    bars: list[_PITBar] = field(default_factory=list)
    daily: list[_DailyRecord] = field(default_factory=list)
    income: list[Any] = field(default_factory=list)
    balance: list[Any] = field(default_factory=list)
    cashflow: list[Any] = field(default_factory=list)
    dividends: list[Any] = field(default_factory=list)

    async def fetch_point_in_time_bars(
        self, symbol: Any, period: Any, start: Any, end: Any, *, decision_at: Any, adjust: str = "qfq"
    ) -> list[_PITBar]:
        del period, start, end, adjust
        gate = decision_at.date()
        return [b for b in self.bars if b.code == symbol.code and b.day <= gate]

    async def fetch_daily_metrics(
        self, symbol: Any, *, start: Any, end: Any, decision_at: Any
    ) -> list[_DailyRecord]:
        del start, end
        gate = decision_at
        return [
            r
            for r in self.daily
            if r.symbol == symbol.code and r.available_at <= gate
        ]

    async def fetch_income_statements(
        self, symbol: Any, *, decision_at: Any
    ) -> list[Any]:
        gate = decision_at.date()
        return [
            r for r in self.income
            if r.symbol == symbol.code and r.announcement_date <= gate
        ]

    async def fetch_balance_sheets(
        self, symbol: Any, *, decision_at: Any
    ) -> list[Any]:
        gate = decision_at.date()
        return [
            r for r in self.balance
            if r.symbol == symbol.code and r.announcement_date <= gate
        ]

    async def fetch_cashflow_statements(
        self, symbol: Any, *, decision_at: Any
    ) -> list[Any]:
        gate = decision_at.date()
        return [
            r for r in self.cashflow
            if r.symbol == symbol.code and r.announcement_date <= gate
        ]

    async def fetch_dividends(
        self, symbol: Any, *, decision_at: Any
    ) -> list[Any]:
        gate = decision_at.date()
        return [
            r for r in self.dividends
            if r.symbol == symbol.code and r.announcement_date <= gate
        ]


_EQUITY_Q1 = Decimal("1000")
_EQUITY_H1 = Decimal("1100")
_CAP = 5000.0


def _bars_provider(*codes: str) -> _StubProvider:
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(300)]
    instruments = [
        _Inst(code, instrument_type="index" if code == INDEX else "stock")
        for code in codes
    ]
    return _StubProvider(
        release=_Release(BARS_RELEASE, _Kind("bars"), instruments),
        bars=[
            _PITBar(code=code, day=day, close=20.0)
            for code in codes
            for day in days
        ],
    )


def _daily_provider() -> _StubProvider:
    days = [date(2024, 1, 2) + timedelta(days=i) for i in range(300)]
    return _StubProvider(
        release=_Release(
            "DR-daily-402",
            _Kind("daily_metrics"),
            [_Inst(SYMBOL)],
        ),
        daily=[
            _DailyRecord(
                symbol=SYMBOL,
                trade_date=day,
                close=20.0,
                total_market_cap=_CAP,
                circulating_market_cap=4000.0,
            )
            for day in days
        ],
    )


def _income_provider(*codes: str) -> _StubProvider:
    records = [
        _record(
            IncomeStatement,
            code,
            date(2024, 4, 26),
            date(2024, 3, 31),
            revenue=Decimal("800"),
            n_income_attr_p=Decimal("60"),
        )
        for code in codes
    ]
    return _StubProvider(
        release=_Release(
            "DR-income-402", _Kind("income_statements"), [_Inst(c) for c in codes]
        ),
        income=records,
    )


def _balance_provider(*codes: str) -> _StubProvider:
    records: list[Any] = []
    for code in codes:
        records.append(
            _record(
                BalanceSheet,
                code,
                date(2024, 4, 26),
                date(2024, 3, 31),
                total_hldr_eqy_exc_min_int=_EQUITY_Q1,
            )
        )
        records.append(
            _record(
                BalanceSheet,
                code,
                date(2024, 8, 20),
                date(2024, 6, 30),
                total_hldr_eqy_exc_min_int=_EQUITY_H1,
            )
        )
    return _StubProvider(
        release=_Release(
            "DR-balance-402", _Kind("balance_sheets"), [_Inst(c) for c in codes]
        ),
        balance=records,
    )


def _cashflow_provider(*codes: str) -> _StubProvider:
    records = [
        _record(
            CashflowStatement,
            code,
            date(2024, 4, 26),
            date(2024, 3, 31),
            n_cashflow_act=Decimal("150"),
            free_cashflow=Decimal("120"),
        )
        for code in codes
    ]
    return _StubProvider(
        release=_Release(
            "DR-cashflow-402",
            _Kind("cashflow_statements"),
            [_Inst(c) for c in codes],
        ),
        cashflow=records,
    )


def _dividends_provider(*codes: str) -> _StubProvider:
    records: list[Any] = []
    for code in codes:
        records.append(
            _record(
                DividendRecord,
                code,
                date(2024, 4, 1),
                date(2023, 12, 31),
                div_proc="3",
                cash_div=Decimal("0.5"),
                ex_date=date(2024, 4, 20),
            )
        )
    return _StubProvider(
        release=_Release(
            "DR-dividends-402", _Kind("dividends"), [_Inst(c) for c in codes]
        ),
        dividends=records,
    )


def _provider_factory(rid: str) -> _StubProvider:
    mapping: dict[str, _StubProvider] = {
        BARS_RELEASE: _bars_provider(SYMBOL, INDEX),
        "DR-daily-402": _daily_provider(),
        "DR-income-402": _income_provider(SYMBOL, INDEX),
        "DR-balance-402": _balance_provider(SYMBOL, INDEX),
        "DR-cashflow-402": _cashflow_provider(SYMBOL),
        "DR-dividends-402": _dividends_provider(SYMBOL),
    }
    return mapping[rid]


def _spec(name: str, *, joint: tuple[str, ...]) -> FactorSeriesRunSpec:
    return FactorSeriesRunSpec(
        code_artifact=name,
        code_commit=predefined_factor_commit(name),
        release_id=BARS_RELEASE,
        dataset_release_ids=joint,
        params={},
        window_start=_DECISIONS[0],
        window_end=_DECISIONS[-1],
        dates=_DECISIONS,
    )


def _settings() -> Any:
    class _S:
        research_sandbox_workspace_root = "data_cache/research_sandbox"
        research_sandbox_max_nan_ratio = 1.0
        research_sandbox_min_coverage = 0.0

    return _S()


class TestValueChannelE2E:
    async def test_bm_matches_reference_with_pit_boundary(self, tmp_path) -> None:
        """val_bm 经真实挂载:Q1 公告前 None / 锚点次日可见 / 市值同日口径。"""
        result = await run_predefined_factor_series(
            _spec(
                "val_bm",
                joint=("DR-daily-402", "DR-balance-402"),
            ),
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        expected = 1000.0 / _CAP
        for day in _DECISIONS:
            assert result.values[day][SYMBOL] == pytest.approx(expected)
        # 时序比值不剔除基准:基准标的有三表(分子)→ 入截面;其市值
        # 分母不在 daily 发布 → None(缺测可见,cross_section=False 采样
        # 面 = 全挂载标的;消费端统一剔除基准)
        assert set(result.values[_DECISIONS[0]]) == {SYMBOL, INDEX}
        assert result.values[_DECISIONS[0]][INDEX] is None

    async def test_bm_announcement_day_invisible_next_day_visible(
        self, tmp_path
    ) -> None:
        """公告日当天(锚次日零点)不可见,次日可见(PIT 边界)。"""
        dates = (date(2024, 4, 26), date(2024, 4, 27), date(2024, 8, 21))
        spec = FactorSeriesRunSpec(
            code_artifact="val_bm",
            code_commit=predefined_factor_commit("val_bm"),
            release_id=BARS_RELEASE,
            dataset_release_ids=("DR-daily-402", "DR-balance-402"),
            params={},
            window_start=dates[0],
            window_end=dates[-1],
            dates=dates,
        )
        result = await run_predefined_factor_series(
            spec,
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        assert result.values[dates[0]][SYMBOL] is None
        assert result.values[dates[1]][SYMBOL] == pytest.approx(
            float(_EQUITY_Q1) / _CAP
        )
        # H1 修订(8/20 公告,8/21 可见)覆盖 Q1 值
        assert result.values[dates[2]][SYMBOL] == pytest.approx(
            float(_EQUITY_H1) / _CAP
        )

    async def test_dps_ttm_via_dividend_events(self, tmp_path) -> None:
        """val_dps_ttm 经 dividend_events:除息日 4/20 前后进窗对照。"""
        dates = (date(2024, 4, 19), date(2024, 4, 20), date(2024, 5, 8))
        spec = FactorSeriesRunSpec(
            code_artifact="val_dps_ttm",
            code_commit=predefined_factor_commit("val_dps_ttm"),
            release_id=BARS_RELEASE,
            dataset_release_ids=("DR-dividends-402",),
            params={},
            window_start=dates[0],
            window_end=dates[-1],
            dates=dates,
        )
        result = await run_predefined_factor_series(
            spec,
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        assert result.values[dates[0]][SYMBOL] == pytest.approx(0.0)
        assert result.values[dates[1]][SYMBOL] == pytest.approx(0.5)
        assert result.values[dates[2]][SYMBOL] == pytest.approx(0.5)

    async def test_qmj_payout_excludes_benchmark_cross_section(
        self, tmp_path
    ) -> None:
        """qmj_payout(cross_section=True):benchmark-only 标的不入截面(#380)。"""
        dates = (date(2024, 5, 8),)
        spec = FactorSeriesRunSpec(
            code_artifact="qmj_payout",
            code_commit=predefined_factor_commit("qmj_payout"),
            release_id=BARS_RELEASE,
            dataset_release_ids=(
                "DR-daily-402",
                "DR-dividends-402",
                "DR-income-402",
            ),
            params={},
            window_start=dates[0],
            window_end=dates[-1],
            dates=dates,
        )
        result = await run_predefined_factor_series(
            spec,
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        assert set(result.values[dates[0]]) == {SYMBOL}
        # 单标的截面 rank = 1.0(股息率与分红率两成分均满秩)
        assert result.values[dates[0]][SYMBOL] == pytest.approx(1.0)
        assert result.metrics["benchmark_only_excluded"] is True

    async def test_unmounted_kind_yields_empty(self, tmp_path) -> None:
        """未挂载三表(联合集只有 bars)→ 空映射 → 空截面。"""
        result = await run_predefined_factor_series(
            _spec("val_bm", joint=(BARS_RELEASE,)),
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        for day in _DECISIONS:
            assert result.values[day] == {}

    async def test_unknown_field_rejected(self, tmp_path) -> None:
        """挂载缺字段(请求字段不在挂载列)→ 具名拒绝。"""
        from finboard_backtest.research_sandbox.errors import SandboxError

        def compute(inp: Any) -> Any:
            return inp.sample(
                inp.research_dataset("balance_sheets", "not_a_field"),
                {},
            )

        entry = get_predefined_factor("val_bm")
        bogus = replace(
            entry,
            name="zz_bogus_field_402",
            data_dependencies=("balance_sheets.not_a_field",),
            compute=compute,
        )
        import finboard_backtest.factors.predefined.registry as registry

        registry.PREDEFINED_FACTORS[bogus.name] = bogus
        try:
            with pytest.raises(SandboxError, match="not_a_field"):
                await run_predefined_factor_series(
                    _spec("zz_bogus_field_402", joint=("DR-balance-402",)),
                    settings=_settings(),
                    release_provider_factory=_provider_factory,
                    workspace_root=tmp_path,
                )
        finally:
            registry.PREDEFINED_FACTORS.pop("zz_bogus_field_402", None)


class TestMountFilterForAnnouncedKinds:
    async def test_filter_variant_keeps_rows_and_guards_window(
        self, tmp_path
    ) -> None:
        """filter_window_data_mount 对公告类新 kind:行子集 + 窗口防线。"""
        baseline = await build_window_data_mount(
            providers=[
                _bars_provider(SYMBOL),
                _daily_provider(),
                _balance_provider(SYMBOL),
                _dividends_provider(SYMBOL),
            ],
            window_start=_DECISIONS[0],
            window_end=_DECISIONS[-1],
            dates=_DECISIONS,
            out_root=tmp_path / "base",
            code_artifact="val_bm",
            code_commit="predefined-test",
            release_id=BARS_RELEASE,
            dataset_release_ids=("DR-daily-402", "DR-balance-402", "DR-dividends-402"),
        )
        assert (baseline.root / "balance_sheets.parquet").exists()
        assert (baseline.root / "dividends.parquet").exists()
        cut = _DECISIONS[1]
        variant = await filter_window_data_mount(
            baseline,
            new_window_end=cut,
            dates=_DECISIONS[:2],
            out_root=tmp_path / "cut",
        )
        import pyarrow.parquet as pq

        for name in ("balance_sheets.parquet", "dividends.parquet"):
            base_table = pq.read_table(baseline.root / name)
            cut_table = pq.read_table(variant.root / name)
            assert cut_table.num_rows <= base_table.num_rows
            ceilings = cut_table.column("available_at").to_pylist()
            assert all(
                ceiling.date() <= cut and ceiling is not None
                for ceiling in ceilings
            )
            for row in cut_table.column("announcement_date").to_pylist():
                assert row <= cut
