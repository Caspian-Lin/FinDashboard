"""``research_data_sync`` convertible_profiles 数据集单元测试(issue #265)。

不依赖 DB:repo / enrichment 均以假对象注入,验证执行器编排、akshare 兜底
失败降级与强赎事件的 available_at 领域不变量。真实落库由集成测试覆盖
(``tests/integration/test_convertible_chain.py``)。
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from finboard_backtest.background_jobs.contracts import JobRecord
from finboard_data.akshare_provider import ConvertibleRedemptionEvent


def _make_job(payload: dict[str, object]) -> JobRecord:
    return JobRecord(
        job_id="BJ-CONV01",
        kind="research_data_sync",
        queue="data",
        payload=payload,
        attempt=1,
        max_attempts=3,
        requested_by="unit-test",
    )


async def _noop_progress(_done: int, _total: int | None, _phase: str | None) -> None:
    pass


def _profile_record(symbol: str) -> object:
    """构造最小 ConvertibleProfile 形状(frozen dataclass,构造时校验)。"""
    from finboard_data.research import ConvertibleProfile

    observed = datetime(2026, 9, 1, tzinfo=UTC)
    return ConvertibleProfile(
        symbol=symbol,
        name="南银转债" if symbol.endswith("SH") else "天赐转2",
        underlying_symbol="601009.SH" if symbol.endswith("SH") else "002709.SZ",
        underlying_name=None,
        list_date=date(2021, 6, 28),
        delist_date=None,
        conversion_price=Decimal("8.5"),
        issue_date=date(2021, 6, 21),
        maturity_date=date(2027, 6, 21),
        coupon_rate=Decimal("0.003"),
        source="tushare",
        observed_at=observed,
        available_at=observed,
    )


class _CommitSession:
    async def __aenter__(self) -> _CommitSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        pass

    async def commit(self) -> None:
        pass


class _CommitSessionMaker:
    def __call__(self) -> _CommitSession:
        return _CommitSession()


class _RecordingRepos:
    """假 persistence 层:记录 upsert / backfill / 事件导入调用。"""

    def __init__(self) -> None:
        self.upsert_calls: list[dict[str, object]] = []
        self.backfill_calls: list[dict[str, dict[str, tuple[date | None, date | None]]]] = []
        self.event_calls: list[dict[str, list[object]]] = []

    def metadata_repository(self) -> type[Any]:
        upsert_calls = self.upsert_calls

        class _FakeConvertibleMetadataRepository:
            def __init__(self, session: object) -> None:
                pass

            async def upsert_many(
                self, profiles: object, **kwargs: object
            ) -> object:
                from finboard_persistence.convertible_metadata_repo import (
                    ConvertibleMetadataSyncResult,
                )

                upsert_calls.append({"profiles": profiles, **kwargs})
                return ConvertibleMetadataSyncResult(
                    total=1,
                    created=1,
                    updated=0,
                    skipped_missing_price=0,
                    missing_maturity_date=0,
                    missing_rating=1,
                )

        return _FakeConvertibleMetadataRepository

    def instrument_repository(self) -> type[Any]:
        backfill_calls = self.backfill_calls
        event_calls = self.event_calls

        class _FakeInstrumentRepository:
            def __init__(self, session: object) -> None:
                pass

            async def backfill_listing_dates(
                self,
                records: dict[str, tuple[date | None, date | None]],
            ) -> dict[str, int]:
                backfill_calls.append({"records": records})
                return {"scoped": len(records)}

            async def import_lifecycle_events(
                self, events: list[object]
            ) -> dict[str, int]:
                event_calls.append({"events": events})
                return {"received": len(events), "inserted": len(events), "skipped": 0}

        return _FakeInstrumentRepository


def _build_executor(provider: object, monkeypatch: pytest.MonkeyPatch) -> Any:
    from finboard_backtest.background_jobs.executors.research_data_sync import (
        ResearchDataSyncExecutor,
    )

    return ResearchDataSyncExecutor(
        session_maker=_CommitSessionMaker(),  # type: ignore[arg-type]
        provider_factory=lambda: provider,  # type: ignore[arg-type,return-value]
    )


def _payload() -> dict[str, object]:
    return {
        "datasets": ["convertible_profiles"],
        "start_date": "2026-01-01",
        "end_date": "2026-01-02",
    }


class TestConvertibleProfilesDataset:
    @pytest.mark.asyncio
    async def test_upserts_metadata_backfills_and_imports_events(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#265 主链路:cb_basic → convertible_metadata upsert + 上市日期回填
        + 强赎事件导入;评级映射与 dataset_version 正确透传。"""
        from finboard_backtest.background_jobs.executors import (
            research_data_sync as sync_mod,
        )

        profiles = [
            _profile_record("113050.SH"),
            _profile_record("123101.SZ"),
        ]

        class _Provider:
            async def fetch_convertible_profiles(self) -> list[object]:
                return profiles

        async def _fake_enrichment() -> tuple[dict[str, str], list[Any]]:
            event = ConvertibleRedemptionEvent(
                code="113050.SH",
                name="南银转债",
                underlying_symbol="601009.SH",
                redemption_date=date(2026, 8, 20),
                stop_transfer_date=None,
                redemption_price=Decimal("100.32"),
                observed_at=datetime(2026, 9, 1, tzinfo=UTC),
            )
            return {"113050.SH": "AA+"}, [sync_mod._redemption_to_lifecycle_event(event)]

        monkeypatch.setattr(sync_mod, "_fetch_convertible_enrichment", _fake_enrichment)
        repos = _RecordingRepos()
        import finboard_persistence as persistence_pkg

        monkeypatch.setattr(
            persistence_pkg, "ConvertibleMetadataRepository", repos.metadata_repository()
        )
        monkeypatch.setattr(
            persistence_pkg, "InstrumentRepository", repos.instrument_repository()
        )

        executor = _build_executor(_Provider(), monkeypatch)
        result = await executor.execute(_make_job(_payload()), _noop_progress)

        assert result.status == "succeeded"
        assert len(repos.upsert_calls) == 1
        assert repos.upsert_calls[0]["profiles"] == profiles
        assert repos.upsert_calls[0]["ratings"] == {"113050.SH": "AA+"}
        assert repos.upsert_calls[0]["source"] == "tushare"
        assert str(repos.upsert_calls[0]["dataset_version"]).startswith(
            "convertible_profiles:"
        )
        # 上市日期回填:{code: (list_date, delist_date)}。
        assert set(repos.backfill_calls[0]["records"]) == {"113050.SH", "123101.SZ"}
        assert len(repos.event_calls[0]["events"]) == 1

    @pytest.mark.asyncio
    async def test_akshare_enrichment_failure_degrades_to_warning(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """akshare 兜底失败 → 主链路不阻断:空评级、零事件,任务成功。"""
        from finboard_backtest.background_jobs.executors import (
            research_data_sync as sync_mod,
        )

        class _Provider:
            async def fetch_convertible_profiles(self) -> list[object]:
                return [_profile_record("113050.SH")]

        async def _boom() -> tuple[dict[str, str], list[Any]]:
            raise ConnectionError("东财限流")

        monkeypatch.setattr(sync_mod, "_fetch_convertible_enrichment", _boom)
        repos = _RecordingRepos()
        import finboard_persistence as persistence_pkg

        monkeypatch.setattr(
            persistence_pkg, "ConvertibleMetadataRepository", repos.metadata_repository()
        )
        monkeypatch.setattr(
            persistence_pkg, "InstrumentRepository", repos.instrument_repository()
        )

        executor = _build_executor(_Provider(), monkeypatch)
        result = await executor.execute(_make_job(_payload()), _noop_progress)

        assert result.status == "succeeded"
        assert repos.upsert_calls[0]["ratings"] == {}
        assert repos.event_calls == []  # 零事件不触发导入


class TestRedemptionEventConversion:
    def test_available_at_never_precedes_effective_date(self) -> None:
        """领域不变量:未来生效的强赎按生效日可见,真实观察时间留 details。"""
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            _redemption_to_lifecycle_event,
        )

        observed = datetime(2026, 9, 1, tzinfo=UTC)
        future = ConvertibleRedemptionEvent(
            code="113050.SH",
            name="南银转债",
            underlying_symbol="601009.SH",
            redemption_date=date(2026, 12, 20),
            stop_transfer_date=None,
            redemption_price=None,
            observed_at=observed,
        )
        event = _redemption_to_lifecycle_event(future)
        # available_at 被夹到生效日开盘(保守方向:不会提前可知)。
        assert event.available_at == datetime(2026, 12, 20, tzinfo=UTC)
        assert event.details["observed_at"] == observed.isoformat()
        assert event.event_type.value == "forced_redemption"
        assert event.dataset_version == "jsl_redeem_v1"

    def test_past_effective_date_keeps_observed_at(self) -> None:
        """历史生效日:available_at = 观察时间,不被追溯放大。"""
        from finboard_backtest.background_jobs.executors.research_data_sync import (
            _redemption_to_lifecycle_event,
        )

        observed = datetime(2026, 9, 1, tzinfo=UTC)
        past = ConvertibleRedemptionEvent(
            code="113050.SH",
            name="南银转债",
            underlying_symbol="601009.SH",
            redemption_date=date(2026, 8, 20),
            stop_transfer_date=None,
            redemption_price=None,
            observed_at=observed,
        )
        event = _redemption_to_lifecycle_event(past)
        assert event.available_at == observed
        assert event.effective_date == date(2026, 8, 20)
        assert (event.available_at - timedelta(days=12)) < event.available_at
