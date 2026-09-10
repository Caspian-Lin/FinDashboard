"""issue #401:预置因子进程内通道的财务公告序列装配端到端单测。

stub provider(对齐 FrozenReleaseProvider 最小面)→ 真实
``build_window_data_mount``(financial_indicators 发布进挂载)→
``run_predefined_factor_series``:

* ``fin_*`` 因子数据依赖 ``financial_indicators.<field>`` 触发财务表
  读取,产出与**独立公告步进参考**逐值对照(决策日采样 =
  available_at 门控,公告间持有最近值);
* 值列全 None(上游字段整体缺失,null 类型列)→ 缺测语义不炸;
* 未挂载财务发布的因子取到空映射(质量门归档不阻断)。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from finboard_backtest.factors.predefined import get_predefined_factor, predefined_factor_commit
from finboard_backtest.research_sandbox.predefined_runner import (
    run_predefined_factor_series,
)
from finboard_backtest.research_sandbox.runner import FactorSeriesRunSpec

SYMBOL = "600000.SH"
BARS_RELEASE = "DR-bars-401"
FIN_RELEASE = "DR-fin-401"

_ANN_A = date(2023, 4, 26)
_ANN_B = date(2023, 8, 22)
_ROE_A, _ROE_B = 0.051, 0.048
_ANNOUNCE_AVAILABLE = lambda day: datetime.combine(  # noqa: E731
    day + timedelta(days=1), datetime.min.time(), tzinfo=UTC
)


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
        return datetime(self.day.year, self.day.month, self.day.day, 15, 30, tzinfo=UTC)

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
    start_date: date = date(2023, 1, 2)
    end_date: date = date(2024, 12, 31)


@dataclass
class _FinRecord:
    """财务公告 stub(领域 FinancialIndicator 的最小投影,_collect_financial 消费面)。"""

    symbol: str
    announcement_date: date
    report_period: date
    update_flag: str = "1"
    return_on_equity: Decimal | None = None
    revenue_yoy: Decimal | None = None
    net_profit_yoy: Decimal | None = None
    source: str = "tushare"
    observed_at: datetime | None = None
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        # PIT 锚 = 公告日次日零点(与领域 _parse_financial 同口径)。
        anchor = datetime.combine(
            self.announcement_date + timedelta(days=1), datetime.min.time(), tzinfo=UTC
        )
        if self.available_at is None:
            self.available_at = anchor
        if self.observed_at is None:
            self.observed_at = anchor


@dataclass
class _StubProvider:
    release: _Release
    bars: list[_PITBar] = field(default_factory=list)
    financial: list[_FinRecord] = field(default_factory=list)

    async def fetch_point_in_time_bars(
        self, symbol: Any, period: Any, start: Any, end: Any, *, decision_at: Any, adjust: str = "qfq"
    ) -> list[_PITBar]:
        del period, start, end, adjust
        gate = decision_at.date()
        return [b for b in self.bars if b.code == symbol.code and b.day <= gate]

    async def fetch_financial_indicators(
        self, symbol: Any, *, decision_at: Any
    ) -> list[_FinRecord]:
        gate = decision_at.date()
        return [
            record
            for record in self.financial
            if record.symbol == symbol.code
            and record.announcement_date <= gate
        ]


def _bars_provider() -> _StubProvider:
    days = [date(2023, 1, 3) + timedelta(days=i) for i in range(300)]
    return _StubProvider(
        release=_Release(BARS_RELEASE, _Kind("bars"), [_Inst(SYMBOL)]),
        bars=[
            _PITBar(code=SYMBOL, day=day, close=10.0 + i * 0.01)
            for i, day in enumerate(days)
        ],
    )


def _fin_provider(
    *, roe_values: list[float | None] | None = None
) -> _StubProvider:
    announcements = [
        (_ANN_A, date(2023, 3, 31)),
        (_ANN_B, date(2023, 6, 30)),
    ]
    values = roe_values if roe_values is not None else [_ROE_A, _ROE_B]
    roe_decimals: list[Decimal | None] = [
        None if v is None else Decimal(str(v)).quantize(Decimal("0.0001"))
        for v in values
    ]
    return _StubProvider(
        release=_Release(FIN_RELEASE, _Kind("financial_indicators"), [_Inst(SYMBOL)]),
        financial=[
            _FinRecord(
                symbol=SYMBOL,
                announcement_date=ann,
                report_period=period,
                return_on_equity=roe,
                revenue_yoy=None,
            )
            for (ann, period), roe in zip(announcements, roe_decimals, strict=True)
        ],
    )


def _provider_factory(rid: str) -> _StubProvider:
    if rid == FIN_RELEASE:
        return _fin_provider()
    return _bars_provider()


def _spec(name: str, *, with_fin: bool = True) -> FactorSeriesRunSpec:
    dates = tuple(date(2023, 5, 2) + timedelta(days=k) for k in range(5))
    return FactorSeriesRunSpec(
        code_artifact=name,
        code_commit=predefined_factor_commit(name),
        release_id=BARS_RELEASE,
        dataset_release_ids=(FIN_RELEASE,) if with_fin else (),
        params={},
        window_start=dates[0],
        window_end=dates[-1],
        dates=dates,
    )


def _settings() -> Any:
    class _S:
        research_sandbox_workspace_root = "data_cache/research_sandbox"
        research_sandbox_max_nan_ratio = 1.0
        research_sandbox_min_coverage = 0.0

    return _S()


class TestFinancialChannelE2E:
    async def test_fin_roe_matches_announcement_step_reference(self, tmp_path) -> None:
        """挂载 → 公告序列 → 采样,与独立参考逐值对照。"""
        result = await run_predefined_factor_series(
            _spec("fin_roe"),
            settings=_settings(),
            release_provider_factory=_provider_factory,
            workspace_root=tmp_path,
        )
        # 决策日(5/2-5/6)在 Q1 公告(4/26,锚 4/27)之后、半年报公告前
        expected = pytest.approx(_ROE_A, abs=1e-4)
        for day in result.dates:
            assert result.values[day][SYMBOL] == expected
        # 值域只含有公告的标的;指标里记录依赖
        assert set(result.values[result.dates[0]]) == {SYMBOL}
        assert result.metrics["data_dependencies"] == [
            "financial_indicators.return_on_equity"
        ]

    async def test_pit_holds_across_announcement_gap(self, tmp_path) -> None:
        """公告间隙持有最近值;公告次日锚点前不可见(步进参考)。"""
        dates = (
            date(2023, 4, 26),  # 锚点(4/27)前 → None
            date(2023, 4, 27),  # 锚点日 → _ROE_A
            date(2023, 6, 30),  # 间隙 → 仍 _ROE_A
            date(2023, 8, 22),  # 半年报公告日(锚 8/23)→ 仍 _ROE_A
            date(2023, 8, 23),  # 锚点日 → _ROE_B
        )
        spec = FactorSeriesRunSpec(
            code_artifact="fin_roe",
            code_commit=predefined_factor_commit("fin_roe"),
            release_id=BARS_RELEASE,
            dataset_release_ids=(FIN_RELEASE,),
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
        assert result.values[dates[1]][SYMBOL] == pytest.approx(_ROE_A, abs=1e-4)
        assert result.values[dates[2]][SYMBOL] == pytest.approx(_ROE_A, abs=1e-4)
        assert result.values[dates[3]][SYMBOL] == pytest.approx(_ROE_A, abs=1e-4)
        assert result.values[dates[4]][SYMBOL] == pytest.approx(_ROE_B, abs=1e-4)

    async def test_all_none_value_column_yields_none(self, tmp_path) -> None:
        """值列全 None(上游字段整体缺失)→ 缺测语义,不炸构建。"""
        provider_fin = _fin_provider(roe_values=[None, None])
        factory = {
            BARS_RELEASE: _bars_provider(),
            FIN_RELEASE: provider_fin,
        }.__getitem__
        result = await run_predefined_factor_series(
            _spec("fin_roe"),
            settings=_settings(),
            release_provider_factory=factory,
            workspace_root=tmp_path,
        )
        for day in result.dates:
            assert result.values[day][SYMBOL] is None

    async def test_financial_release_not_mounted_yields_empty(self, tmp_path) -> None:
        """未挂载财务发布(联合集只有 bars)→ 空映射 → 全 None 截面。"""
        result = await run_predefined_factor_series(
            FactorSeriesRunSpec(
                code_artifact="fin_roe",
                code_commit=predefined_factor_commit("fin_roe"),
                release_id=BARS_RELEASE,
                dataset_release_ids=(BARS_RELEASE,),
                params={},
                window_start=date(2023, 5, 2),
                window_end=date(2023, 5, 6),
                dates=tuple(date(2023, 5, 2) + timedelta(days=k) for k in range(5)),
            ),
            settings=_settings(),
            release_provider_factory={BARS_RELEASE: _bars_provider()}.__getitem__,
            workspace_root=tmp_path,
        )
        for day in result.dates:
            assert result.values[day] == {}

    async def test_unknown_field_rejected(self, tmp_path) -> None:
        """挂载缺字段(请求字段不在挂载列)→ 具名拒绝(output_contract_violation)。"""
        from dataclasses import replace

        from finboard_backtest.research_sandbox.errors import SandboxError

        def compute(inp: Any) -> Any:
            return inp.sample(
                inp.financial_indicators("not_a_field"),
                {},
            )

        entry = get_predefined_factor("fin_roe")
        bogus = replace(
            entry,
            name="zz_bogus_field",
            data_dependencies=("financial_indicators.not_a_field",),
            compute=compute,
        )
        import finboard_backtest.factors.predefined.registry as registry

        registry.PREDEFINED_FACTORS[bogus.name] = bogus
        try:
            with pytest.raises(SandboxError, match="not_a_field"):
                await run_predefined_factor_series(
                    _spec("zz_bogus_field"),
                    settings=_settings(),
                    release_provider_factory=_provider_factory,
                    workspace_root=tmp_path,
                )
        finally:
            registry.PREDEFINED_FACTORS.pop("zz_bogus_field", None)
