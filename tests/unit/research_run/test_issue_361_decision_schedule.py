"""决策日历泛化 decision_schedule(issue #361)。

覆盖验收:

* 四频 + custom 推导 —— ``_derive_decision_dates`` 纯函数:daily 逐交易日 /
  weekly 每周最后一个交易日 / monthly、quarterly 与 #183 旧口径逐值等值 /
  custom 显式日期;统一「发布末尾之后无成交日不产决策」;周末(非交易日)
  天然跳过;custom 日期 ⊄ 交易日历 fail-closed;
* 入队期校验 —— REST ``ResearchRunQueueIn`` schema(与 MCP 共用同一
  pydantic 校验,#183/#203 同口径)对非法 schedule 入队即拒;legacy
  ``rebalance_frequency`` 扩展 daily/weekly,旧值零变化;两键互斥;
* 覆盖检查接线 —— ``enqueue_decision_dates`` 与执行期推导消费同一实现;
* 日频冒烟 —— 合成 30 个交易日的 multi_period daily 端到端
  ``build_decision_inputs``(stub 冻结发布,照既有 multi_period 测试风格),
  逐日决策、特征按发布重算、决策时点严格递增。

纯离线研究域,不连 broker 不下单。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from finboard_api.research_run_schemas import ResearchRunQueueIn
from finboard_backtest.research_run import (
    DecisionSchedule,
    FrozenArtifactRef,
    ResearchExecutionMode,
    ResearchRunManifest,
    execution_mode_for,
    parse_decision_schedule,
    resolve_decision_schedule,
    stable_checksum,
)
from finboard_backtest.research_run.signal_engine import (
    _derive_decision_dates,
    build_decision_inputs,
    decision_schedule_dates_gate_error,
    enqueue_decision_dates,
)
from finboard_backtest.strategy_spec.contracts import ResearchStrategySpec

from .test_issue_306_load_probe import _noop_snapshot_provider
from .test_signal_engine import (
    _price_only_spec,
    _StubBar,
    _StubInstrument,
    _StubProvider,
    _StubRelease,
)

# ---- 四频 + custom 推导(纯函数) ---------------------------------------------

_JAN = [date(2024, 1, day) for day in range(1, 32) if date(2024, 1, day).weekday() < 5]
_FEB = [date(2024, 2, day) for day in range(1, 30) if date(2024, 2, day).weekday() < 5]
_MAR = [date(2024, 3, day) for day in range(1, 32) if date(2024, 3, day).weekday() < 5]
#: 2024-01-02 → 2024-03-29 的周内交易日(周末天然不在日历 = 非交易日跳过)。
CALENDAR: list[date] = _JAN + _FEB + _MAR


class TestDeriveDecisionDates:
    def test_daily_every_trading_day_drop_last(self) -> None:
        """daily = 每个交易日;发布末日之后无成交日,剔除(与 #183 期末口径一致)。"""
        dates = _derive_decision_dates(CALENDAR, DecisionSchedule(kind="daily"))
        assert dates == CALENDAR[:-1]
        # 全部是交易日(非交易日被日历排除,不存在跳到周末的决策)。
        assert all(day.weekday() < 5 for day in dates)

    def test_weekly_is_last_trading_day_of_each_iso_week(self) -> None:
        """weekly = 每个 ISO 周最后一个交易日;末日剔除。"""
        dates = _derive_decision_dates(CALENDAR, DecisionSchedule(kind="weekly"))
        assert all(day.weekday() == 4 for day in dates)  # 全是周五
        assert len(dates) == len(dates)  # 去重后无重复
        assert dates == sorted(set(dates))
        # 每个周五之后仍存在后续交易日(末日周五 2024-03-29 被剔除)。
        assert date(2024, 3, 29) not in dates
        assert date(2024, 1, 5) in dates

    def test_monthly_matches_issue_183_semantics(self) -> None:
        """monthly 与 #183 旧实现逐值等值:每自然月最后一个交易日,末日剔除。"""
        dates = _derive_decision_dates(CALENDAR, DecisionSchedule(kind="monthly"))
        # 参照实现:旧 _derive_rebalance_decision_days 的周期桶口径。
        bucket: dict[tuple[int, int], date] = {}
        for day in CALENDAR:
            bucket[(day.year, day.month)] = day
        expected = [day for day in sorted(bucket.values()) if day < CALENDAR[-1]]
        assert dates == expected
        assert [day.isoformat() for day in dates[:2]] == ["2024-01-31", "2024-02-29"]

    def test_quarterly_matches_issue_183_semantics(self) -> None:
        """quarterly 与 #183 旧实现逐值等值:每季最后一个交易日,末日剔除。"""
        dates = _derive_decision_dates(CALENDAR, DecisionSchedule(kind="quarterly"))
        # 日历止于 2024-03-29 = Q1 桶内最后交易日 = 发布末日 → 剔除后无决策。
        assert dates == []
        # 扩展到 Q2 后,Q1 末(3-29)之后存在交易日 → 恢复产出。
        april = [
            date(2024, 4, day) for day in range(1, 30) if date(2024, 4, day).weekday() < 5
        ]
        dates_q2 = _derive_decision_dates(
            CALENDAR + april, DecisionSchedule(kind="quarterly")
        )
        assert dates_q2 == [date(2024, 3, 29)]

    def test_custom_exact_dates_dedup_and_order(self) -> None:
        """custom = 显式日期原样(升序去重已由 schema 校验,此处逐值透传);末日剔除。"""
        picked = (date(2024, 1, 31), date(2024, 2, 28), date(2024, 3, 28))
        dates = _derive_decision_dates(
            CALENDAR, DecisionSchedule(kind="custom", dates=picked)
        )
        assert dates == list(picked)

    def test_custom_date_on_last_trading_day_dropped(self) -> None:
        dates = _derive_decision_dates(
            CALENDAR, DecisionSchedule(kind="custom", dates=(date(2024, 3, 29),))
        )
        assert dates == []

    def test_custom_date_outside_calendar_fails_closed(self) -> None:
        """custom 日期不在发布交易日历:具名拒绝(防御手工构造的 manifest)。"""
        with pytest.raises(ValueError, match="不在发布交易日历中") as excinfo:
            _derive_decision_dates(
                CALENDAR,
                DecisionSchedule(
                    kind="custom",
                    dates=(date(2024, 1, 6), date(2024, 1, 31)),  # 1-06 是周六
                ),
            )
        assert "2024-01-06" in str(excinfo.value)

    def test_duplicate_calendar_days_deduped(self) -> None:
        """输入日历含重复(防御):推导结果去重升序。"""
        dates = _derive_decision_dates(
            [date(2024, 1, 8), date(2024, 1, 8), date(2024, 1, 9)],
            DecisionSchedule(kind="daily"),
        )
        assert dates == [date(2024, 1, 8)]

    def test_empty_calendar_returns_empty(self) -> None:
        assert _derive_decision_dates([], DecisionSchedule(kind="daily")) == []


# ---- legacy 映射与 schema 入队校验 -------------------------------------------


class TestLegacyFrequencyMapping:
    def test_legacy_values_map_to_same_kind(self) -> None:
        """legacy rebalance_frequency 四值映射为同名 kind schedule(旧值零变化)。"""
        for frequency in ("daily", "weekly", "monthly", "quarterly"):
            assert resolve_decision_schedule({"rebalance_frequency": frequency}) == (
                DecisionSchedule(kind=frequency)
            )

    def test_no_declaration_returns_none(self) -> None:
        assert resolve_decision_schedule({}) is None

    def test_both_keys_rejected(self) -> None:
        with pytest.raises(ValueError, match="不可同时声明"):
            resolve_decision_schedule(
                {"decision_schedule": {"kind": "daily"}, "rebalance_frequency": "monthly"}
            )

    def test_legacy_invalid_value_fails_closed(self) -> None:
        with pytest.raises(ValueError, match="rebalance_frequency"):
            resolve_decision_schedule({"rebalance_frequency": "yearly"})

    def test_parse_rejects_bad_shape(self) -> None:
        for raw in (
            "daily",
            {"kind": "yearly"},
            {"kind": "custom"},
            {"kind": "daily", "dates": ["2024-01-02"]},
            {"kind": "custom", "dates": ["2024-01-02", "2024-01-01"]},
            {"kind": "custom", "dates": ["2024-01-02", "2024-01-02"]},
            {"kind": "custom", "dates": ["not-a-date"]},
            {"kind": "daily", "freq": "x"},
        ):
            with pytest.raises(ValueError, match="decision_schedule"):
                parse_decision_schedule(raw)


def _queue_payload(parameters: dict[str, Any]) -> dict[str, Any]:
    """最小合法入队 payload(仅过 schema 形状,不做业务校验)。"""
    return {
        "idempotency_key": "issue-361-schema-0001",
        "strategy_id": "s",
        "strategy_version": 1,
        "dataset_release_ids": ["r1"],
        "parameters": parameters,
        "code_version": "abcdef0123456789",
        "initial_capital": "100000",
        "requested_by": "tester",
    }


class TestRestSchemaGate:
    """REST ``ResearchRunQueueIn`` 入队即拒(与 MCP ``parse_queue_payload``
    共用同一 schema,两口径一致;MCP 侧见 tests/unit/test_mcp_tools_runs.py)。"""

    def test_daily_and_weekly_legacy_now_accepted(self) -> None:
        """issue #361:legacy daily/weekly 放行(此前仅 monthly/quarterly)。"""
        for parameters in (
            {"rebalance_frequency": "daily"},
            {"rebalance_frequency": "weekly"},
            {"rebalance_frequency": "monthly"},
            {"rebalance_frequency": "quarterly"},
        ):
            body = ResearchRunQueueIn.model_validate(_queue_payload(parameters))
            assert body.parameters == parameters

    def test_decision_schedule_accepted_all_kinds(self) -> None:
        for schedule in (
            {"kind": "daily"},
            {"kind": "weekly"},
            {"kind": "monthly"},
            {"kind": "quarterly"},
            {"kind": "custom", "dates": ["2024-01-02", "2024-01-31"]},
        ):
            body = ResearchRunQueueIn.model_validate(
                _queue_payload({"decision_schedule": schedule})
            )
            assert body.parameters["decision_schedule"] == schedule

    def test_invalid_kind_rejected(self) -> None:
        with pytest.raises(Exception, match="decision_schedule"):
            ResearchRunQueueIn.model_validate(
                _queue_payload({"decision_schedule": {"kind": "yearly"}})
            )

    def test_custom_without_dates_rejected(self) -> None:
        with pytest.raises(Exception, match="decision_schedule"):
            ResearchRunQueueIn.model_validate(
                _queue_payload({"decision_schedule": {"kind": "custom"}})
            )

    def test_custom_unsorted_dates_rejected(self) -> None:
        with pytest.raises(Exception, match="decision_schedule"):
            ResearchRunQueueIn.model_validate(
                _queue_payload(
                    {
                        "decision_schedule": {
                            "kind": "custom",
                            "dates": ["2024-02-01", "2024-01-02"],
                        }
                    }
                )
            )

    def test_legacy_with_schedule_conflict_rejected(self) -> None:
        with pytest.raises(Exception, match="不可同时声明"):
            ResearchRunQueueIn.model_validate(
                _queue_payload(
                    {
                        "decision_schedule": {"kind": "daily"},
                        "rebalance_frequency": "monthly",
                    }
                )
            )


# ---- 入队期决策日推导接线 -----------------------------------------------------


class TestEnqueueDecisionDates:
    def test_matches_execution_derivation(self) -> None:
        """入队期 ``enqueue_decision_dates`` 与执行期推导消费同一实现(同口径)。"""
        assert enqueue_decision_dates(
            parameters={"decision_schedule": {"kind": "weekly"}},
            trading_days=CALENDAR,
        ) == _derive_decision_dates(CALENDAR, DecisionSchedule(kind="weekly"))

    def test_custom_dates_gate_error(self) -> None:
        schedule = DecisionSchedule(kind="custom", dates=(date(2024, 1, 6),))
        error = decision_schedule_dates_gate_error(schedule, CALENDAR)
        assert error is not None
        assert "decision_schedule_dates_not_trading_days" in error
        assert "2024-01-06" in error

    def test_custom_dates_gate_passes_when_all_trading_days(self) -> None:
        schedule = DecisionSchedule(kind="custom", dates=(date(2024, 1, 2),))
        assert decision_schedule_dates_gate_error(schedule, CALENDAR) is None

    def test_single_shot_returns_empty(self) -> None:
        assert enqueue_decision_dates(parameters={}, trading_days=CALENDAR) == []


# ---- 日频 multi_period 端到端冒烟 ---------------------------------------------

#: 决策窗口前预热(工作日):momentum(lookback=20)需 ≥21 个收盘,预热使
#: 决策窗口首日即可重算价格特征(与 #183 monthly「发布起点需足够历史」
#: 同一约束;daily 推导从发布首个交易日起,预热必须落在发布窗口之前)。
_WARMUP_SESSIONS = 25
_DECISION_SESSIONS = 30
_DAILY_SYMBOLS = ("000001.SZ", "000002.SZ", "000003.SZ")


def _weekdays(count: int, *, start: date) -> list[date]:
    days: list[date] = []
    cursor = start
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor += timedelta(days=1)
    return days


class _WarmupProvider(_StubProvider):
    """日历只含发布区间 bars,close 历史保留发布前预热(照 #304 loose stub
    先例:``fetch_close_history`` 不按 start 裁剪;真实发布语义下预热即发布
    自身起点之前的历史,这里以 stub 表达「决策窗口从发布中段开始」)。"""

    async def fetch_bars(
        self,
        symbol: object,
        period: object,
        start: date,
        end: date,
        *,
        adjust: str = "qfq",
    ) -> list[_StubBar]:
        del period, adjust
        by_date = self.closes_by_symbol.get(symbol.code)  # type: ignore[attr-defined]
        if not by_date:
            return []
        return [
            _StubBar(
                close,
                timestamp=datetime.combine(day, datetime.min.time(), tzinfo=UTC),
            )
            for day, close in sorted(by_date.items())
            if start <= day <= end
        ]


def _daily_smoke_provider() -> tuple[_WarmupProvider, list[date], list[date]]:
    """合成价格:预热 25 个工作日 + 决策窗口 30 个工作日,固定漂移 + 交替抖动
    (非零波动率,照 #288 风格),保证 momentum / volatility_20d 可算。"""
    decision_days = _weekdays(_DECISION_SESSIONS, start=date(2024, 2, 1))
    warmup_days = _weekdays(_WARMUP_SESSIONS, start=date(2023, 12, 18))
    all_days = warmup_days + decision_days
    closes: dict[str, dict[date, Decimal]] = {}
    for symbol in _DAILY_SYMBOLS:
        series: dict[date, Decimal] = {}
        base = Decimal("10.0")
        for index, day in enumerate(all_days):
            drift = Decimal("1.001") ** index
            wiggle = Decimal("1.01") if index % 2 == 0 else Decimal("0.99")
            series[day] = base * drift * wiggle
        closes[symbol] = series
    provider = _WarmupProvider(
        release=_StubRelease(
            "release-multi",
            tuple(_StubInstrument(code=code) for code in _DAILY_SYMBOLS),
            start_date=decision_days[0],
            end_date=decision_days[-1],
        ),
        closes_by_symbol=closes,
    )
    return provider, warmup_days, decision_days


def _daily_schedule_manifest(
    spec: ResearchStrategySpec,
    *,
    kind: str,
) -> ResearchRunManifest:
    return ResearchRunManifest(
        run_id="RR-issue361-daily-0001",
        idempotency_key="issue-361-daily-0001",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="release-multi",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"decision_schedule": {"kind": kind}},
        code_version="abcdef0123456789",
        initial_capital=Decimal("100000"),
        requested_by="unit-test",
    )


@pytest.mark.asyncio
class TestDailyMultiPeriodSmoke:
    async def test_daily_schedule_end_to_end_small_window(self) -> None:
        """合成 30 个交易日 multi_period daily 冒烟:逐日决策 + 特征每期重算。

        execution_mode=multi_period 由 ``decision_schedule`` 判定(未声明
        legacy frequency);29 个决策日(发布末日剔除),决策时点严格递增,
        特征来源绑定冻结发布,signal 的 factor_snapshot_id 恒为 None。
        """
        provider, _warmup, decision_days = _daily_smoke_provider()
        manifest = _daily_schedule_manifest(_price_only_spec(), kind="daily")
        assert execution_mode_for(manifest.parameters) is ResearchExecutionMode.MULTI_PERIOD

        inputs = await build_decision_inputs(
            manifest,
            release_provider_factory=lambda release_id: provider,  # type: ignore[arg-type,unused-ignore]
            snapshot_provider=_noop_snapshot_provider,
        )
        # 每个交易日一个决策;发布末日之后无成交日 → 29 期。
        assert len(inputs) == _DECISION_SESSIONS - 1
        decision_dates = [item.decision_at.date() for item in inputs]
        assert decision_dates == decision_days[:-1]
        assert decision_dates == sorted(decision_dates)
        assert len(set(decision_dates)) == len(decision_dates)
        # 每期特征由发布重算(价格特征 + 来源绑定冻结 release)。
        for item in inputs:
            names = {feature.feature_id for feature in item.features}
            assert "momentum" in names
            assert all(
                feature.source_artifact_ids == ("release-multi",) for feature in item.features
            )
            assert all(signal.factor_snapshot_id is None for signal in item.signals)

    async def test_weekly_schedule_end_to_end(self) -> None:
        """weekly 端到端对照:同期数据只有每周最后一个交易日出决策。"""
        provider, _warmup, decision_days = _daily_smoke_provider()
        manifest = _daily_schedule_manifest(_price_only_spec(), kind="weekly")

        inputs = await build_decision_inputs(
            manifest,
            release_provider_factory=lambda release_id: provider,  # type: ignore[arg-type,unused-ignore]
            snapshot_provider=_noop_snapshot_provider,
        )
        weekly_dates = [item.decision_at.date() for item in inputs]
        assert weekly_dates == _derive_decision_dates(
            decision_days, DecisionSchedule(kind="weekly")
        )
        # 显著少于 daily(30 个交易日 ≈ 6 周减末日)。
        assert len(weekly_dates) < _DECISION_SESSIONS - 1
