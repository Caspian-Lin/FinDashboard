"""行级质量口径按 Spec 形态分发(issue #392;#389 口径固化)。

provider 层:``dirty_row_policy`` 显式策略(全市场枚举可跳 / 可拒,按 symbol
精确查询恒拒且拒绝 skip);runner 层:SliceQuery.row_policy 由 SyncSpec 形态
推导并透传到 fetch 调用。
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

import finboard_data.tushare_provider as tushare_provider_module
from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_backtest.background_jobs.dataset_sync import DatasetSyncExecutor
from finboard_data import TushareResearchDataProvider
from finboard_data.research import (
    ResearchDataConfigurationError,
    ResearchDataContractError,
)

pytestmark = pytest.mark.unit

OBSERVED_AT = datetime(2026, 9, 9, 2, 0, tzinfo=UTC)


class _WarningCapture:
    def __init__(self) -> None:
        self.entries: list[dict[str, object]] = []

    def warning(self, event: str, **kw: object) -> None:
        self.entries.append({"event": event, **kw})


class NoopBudget:
    async def acquire(self) -> None:
        return None


class _Client:
    """可配置原始行的离线 client;未配置接口被调用即失败。"""

    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, object]]] = {}

    def _rows(self, endpoint: str, **kwargs: str) -> list[dict[str, object]]:
        if endpoint == "daily_basic":
            configured = self.rows.get("daily_basic")
            if configured is not None:
                return list(configured)
            return [self._daily_row(trade_date=kwargs["trade_date"])]
        return list(self.rows.get(endpoint, []))

    @staticmethod
    def _daily_row(**overrides: object) -> dict[str, object]:
        row: dict[str, object] = {
            "ts_code": "000001.SZ",
            "trade_date": "20260724",
            "close": "12.34",
            "turnover_rate": "2.5",
            "turnover_rate_f": "3.1",
            "volume_ratio": "1.2",
            "pe": "8.5",
            "pe_ttm": "9.1",
            "pb": "0.92",
            "ps": "1.1",
            "ps_ttm": "1.2",
            "dv_ratio": "3.0",
            "dv_ttm": "3.2",
            "total_share": "1941000",
            "float_share": "1940000",
            "free_share": "1500000",
            "total_mv": "23950000",
            "circ_mv": "23900000",
            "limit_status": 0,
        }
        row.update(overrides)
        return row

    def stock_basic(self, **kwargs: str) -> object:
        return self._rows("stock_basic", **kwargs)

    def namechange(self, **kwargs: str) -> object:
        return self._rows("namechange", **kwargs)

    def daily_basic(self, **kwargs: str) -> object:
        return self._rows("daily_basic", **kwargs)

    def fina_indicator(self, **kwargs: str) -> object:
        return self._rows("fina_indicator", **kwargs)

    def index_member_all(self, **kwargs: str) -> object:
        return self._rows("index_member_all", **kwargs)

    def cb_basic(self, **kwargs: str) -> object:
        return self._rows("cb_basic", **kwargs)

    def suspend_d(self, **kwargs: str) -> object:
        return self._rows("suspend_d", **kwargs)
    def index_basic(self, **kwargs: str) -> object:
        return self._rows("index_basic", **kwargs)


def _provider(client: _Client) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client,
        now=lambda: OBSERVED_AT,
        budget=NoopBudget(),
    )


def _dirty_stock_row() -> dict[str, object]:
    return {
        "ts_code": "T600018.SH",
        "name": "上港集箱(退)",
        "industry": "港口",
        "market": "主板",
        "exchange": "SSE",
        "list_status": "L",
        "list_date": "20000719",
        "delist_date": "20061020",
    }


def _clean_stock_row() -> dict[str, object]:
    return {
        "ts_code": "000001.SZ",
        "name": "平安银行",
        "industry": "银行",
        "market": "主板",
        "exchange": "SZSE",
        "list_status": "L",
        "list_date": "19910403",
        "delist_date": None,
    }


class TestDailyMarketRowPolicy:
    async def test_default_rejects_dirty_row(self) -> None:
        client = _Client()
        client.rows["daily_basic"] = [
            _Client._daily_row(ts_code="bad code")
        ]
        with pytest.raises(ResearchDataContractError, match="6 位数字"):
            await _provider(client).fetch_daily_metrics(date(2026, 7, 24))

    async def test_skip_policy_skips_dirty_row_with_named_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client()
        # 干净行 + 脏行:脏行被跳过,干净行保留(全脏行护栏见下一条)。
        client.rows["daily_basic"] = [
            _Client._daily_row(ts_code="bad code"),
            _Client._daily_row(ts_code="600000.SH"),
        ]
        capture = _WarningCapture()
        monkeypatch.setattr(tushare_provider_module, "logger", capture)

        metrics = await _provider(client).fetch_daily_metrics(
            date(2026, 7, 24), dirty_row_policy="skip"
        )

        assert [item.symbol for item in metrics] == ["600000.SH"]
        skipped = [
            e for e in capture.entries if e["event"] == "tushare.dirty_row_skipped"
        ]
        assert len(skipped) == 1
        assert skipped[0]["endpoint"] == "daily_basic"

    async def test_skip_policy_all_dirty_rows_still_rejected(self) -> None:
        # #389 口径:全部行被跳过 = 上游 schema 破坏,整批拒。
        client = _Client()
        client.rows["daily_basic"] = [_Client._daily_row(ts_code="bad code")]
        with pytest.raises(ResearchDataContractError, match="均违反契约"):
            await _provider(client).fetch_daily_metrics(
                date(2026, 7, 24), dirty_row_policy="skip"
            )

    async def test_reject_policy_explicit(self) -> None:
        client = _Client()
        client.rows["daily_basic"] = [_Client._daily_row(ts_code="bad code")]
        with pytest.raises(ResearchDataContractError):
            await _provider(client).fetch_daily_metrics(
                date(2026, 7, 24), dirty_row_policy="reject"
            )


class TestConvertibleRowPolicy:
    async def test_skip_policy_skips_dirty_cb_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        dirty_cb: dict[str, object] = {
            "ts_code": "bad-bond",
            "bond_full_name": "脏行转债",
            "bond_short_name": "脏行",
            "stock_code": "000001.SZ",
            "stock_name": "平安银行",
            "list_date": "20210115",
            "delist_date": None,
            "swap_price": "12.80",
            "value_date": "20201201",
            "mature_date": "20261201",
            "coupon_rate": "1.2",
        }
        clean_cb = dict(dirty_cb, ts_code="127012.SZ")
        client = _Client()
        client.rows["cb_basic"] = [dirty_cb, clean_cb]
        capture = _WarningCapture()
        monkeypatch.setattr(tushare_provider_module, "logger", capture)

        profiles = await _provider(client).fetch_convertible_profiles(
            dirty_row_policy="skip"
        )

        assert [item.symbol for item in profiles] == ["127012.SZ"]
        assert any(
            e["event"] == "tushare.dirty_row_skipped" for e in capture.entries
        )

    async def test_default_rejects_dirty_cb_row(self) -> None:
        dirty_cb = dict(
            _Client._daily_row(),
            ts_code="bad-bond",
        )
        client = _Client()
        client.rows["cb_basic"] = [dirty_cb]
        with pytest.raises(ResearchDataContractError):
            await _provider(client).fetch_convertible_profiles()


class TestPerSymbolPolicyGuard:
    async def test_financial_rejects_skip_policy(self) -> None:
        client = _Client()
        with pytest.raises(ResearchDataConfigurationError, match="不支持"):
            await _provider(client).fetch_financial_indicators(
                "000001.SZ",
                start_period=date(2026, 1, 1),
                end_period=date(2026, 7, 27),
                dirty_row_policy="skip",
            )

    async def test_industry_rejects_skip_policy(self) -> None:
        client = _Client()
        with pytest.raises(ResearchDataConfigurationError, match="不支持"):
            await _provider(client).fetch_industry_memberships(
                symbol="000001.SZ", dirty_row_policy="skip"
            )

    async def test_financial_accepts_reject_policy(self) -> None:
        client = _Client()
        client.rows["fina_indicator"] = []
        records = await _provider(client).fetch_financial_indicators(
            "000001.SZ",
            start_period=date(2026, 1, 1),
            end_period=date(2026, 7, 27),
            dirty_row_policy="reject",
        )
        assert records == []

    async def test_invalid_policy_value_rejected(self) -> None:
        client = _Client()
        client.rows["stock_basic"] = [_clean_stock_row()]
        with pytest.raises(ResearchDataConfigurationError, match="dirty_row_policy"):
            await _provider(client).fetch_instrument_profiles(
                dirty_row_policy="yolo"
            )


class TestProfilesExplicitReject:
    async def test_profiles_reject_policy_restores_batch_reject(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = _Client()
        client.rows["stock_basic"] = [_clean_stock_row(), _dirty_stock_row()]
        monkeypatch.setattr(tushare_provider_module, "logger", _WarningCapture())

        with pytest.raises(ResearchDataContractError, match="6 位数字"):
            await _provider(client).fetch_instrument_profiles(
                dirty_row_policy="reject"
            )


# ---- runner 分发 ---------------------------------------------------------------


class _RecordingProvider:
    """记录每次 fetch 收到的 row_policy,验证框架按 Spec 形态分发。"""

    def __init__(self) -> None:
        self.policies: dict[str, list[str]] = {}

    def _record(self, key: str, policy: str | None) -> list[Any]:
        self.policies.setdefault(key, []).append(policy or "<none>")
        return []

    async def fetch_instrument_profiles(
        self, *, list_status: str = "L", dirty_row_policy: str | None = None
    ) -> list[Any]:
        return self._record(f"profiles:{list_status}", dirty_row_policy)

    async def fetch_name_changes(
        self, *, dirty_row_policy: str | None = None
    ) -> list[Any]:
        return self._record("name_changes", dirty_row_policy)

    async def fetch_daily_metrics(
        self, trade_date: date, *, dirty_row_policy: str | None = None
    ) -> list[Any]:
        return self._record("daily_metrics", dirty_row_policy)

    async def fetch_financial_indicators(
        self, symbol: str, *, start_period: date, end_period: date,
        dirty_row_policy: str | None = None,
    ) -> list[Any]:
        return self._record("financial_indicators", dirty_row_policy)

    async def fetch_industry_memberships(
        self, *, symbol: str, current_only: bool = True,
        dirty_row_policy: str | None = None,
    ) -> list[Any]:
        return self._record("industry_memberships", dirty_row_policy)

    async def fetch_convertible_profiles(
        self, *, dirty_row_policy: str | None = None
    ) -> list[Any]:
        return self._record("convertible_profiles", dirty_row_policy)

    async def fetch_suspensions(
        self, trade_date: date, *, dirty_row_policy: str | None = None
    ) -> list[Any]:
        return self._record("suspensions", dirty_row_policy)


def _job(datasets: list[str]) -> JobRecord:
    return JobRecord(
        job_id="BJ-POLICY392",
        kind="dataset_sync",
        queue="data",
        payload={
            "datasets": datasets,
            "start_date": "2026-07-24",
            "end_date": "2026-07-27",
            "symbols": ["000001.SZ"],
        },
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )


async def _noop_progress(_d: int, _t: int | None, _p: str | None) -> None:
    return None


@pytest.mark.unit
async def test_runner_dispatches_skip_policy_for_full_market_shapes() -> None:
    """全市场枚举形态(FULL_PAGED / DAILY_MARKET)→ row_policy=skip 透传。"""

    provider = _RecordingProvider()
    executor = DatasetSyncExecutor(
        session_maker=_unused_session_maker(),
        provider_factory=lambda: provider,
    )
    result = await executor.execute(
        _job(["profiles", "name_changes", "convertible_profiles", "daily_metrics"]),
        _noop_progress,
    )
    assert result.status == "succeeded"
    assert provider.policies["profiles:L"] == ["skip"]
    assert provider.policies["profiles:D"] == ["skip"]
    assert provider.policies["name_changes"] == ["skip"]
    assert provider.policies["convertible_profiles"] == ["skip"]
    # 2026-07-24(周五)~ 2026-07-27(周一):两个工作日切片。
    assert provider.policies["daily_metrics"] == ["skip", "skip"]


@pytest.mark.unit
async def test_runner_dispatches_reject_policy_for_per_symbol_shape() -> None:
    """按 symbol 精确查询(PER_SYMBOL_RANGE)→ row_policy=reject 透传。"""

    provider = _RecordingProvider()
    executor = DatasetSyncExecutor(
        session_maker=_unused_session_maker(),
        provider_factory=lambda: provider,
    )
    result = await executor.execute(
        _job(["financial_indicators", "industry_memberships"]),
        _noop_progress,
    )
    assert result.status == "succeeded"
    assert provider.policies["financial_indicators"] == ["reject"]
    assert provider.policies["industry_memberships"] == ["reject"]


@pytest.mark.unit
async def test_runner_phase_encoding() -> None:
    """切片 phase 形如 ``dataset_sync:<dataset>[:<键>]``。"""

    provider = _RecordingProvider()
    phases: list[tuple[int, str | None]] = []

    async def progress(done: int, total: int | None, phase: str | None) -> None:
        if phase is not None:
            phases.append((done, phase))

    executor = DatasetSyncExecutor(
        session_maker=_unused_session_maker(),
        provider_factory=lambda: provider,
    )
    await executor.execute(_job(["daily_metrics"]), progress)
    names = [phase for _, phase in phases]
    assert "dataset_sync:start" in names
    assert "dataset_sync:daily_metrics:2026-07-24" in names
    assert "dataset_sync:daily_metrics:2026-07-27" in names
    assert names[-1] == "dataset_sync:done"


class _UnusedSessionMaker:
    """空切片路径不触库;session_maker 仅被构造、不被使用。"""

    def __call__(self) -> None:  # pragma: no cover - 防御
        raise AssertionError("unit 测试不应触库")


def _unused_session_maker() -> Any:
    return _UnusedSessionMaker()
