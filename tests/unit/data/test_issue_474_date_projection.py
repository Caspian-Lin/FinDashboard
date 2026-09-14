"""#474 run-scoped factor-series date projection tests."""

from __future__ import annotations

import asyncio
import math
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_run.contracts import FrozenArtifactRef
from finboard_backtest.research_run.frozen_loader import FrozenInputLoader
from finboard_data.factor_series_store import (
    FactorSeriesArtifactError,
    LazySeriesValues,
    write_series_artifact,
)


def _artifact(tmp_path: Path) -> tuple[Path, str, str, dict[str, dict[str, float | None]]]:
    values: dict[str, dict[str, float | None]] = {}
    first = date(2024, 1, 1)
    for index in range(4):
        day = (first + timedelta(days=index)).isoformat()
        values[day] = {
            "000001.SZ": None if index == 1 else float(index),
            "600000.SH": math.nan if index == 2 else float(index + 10),
        }
    meta = write_series_artifact(tmp_path, "v2-projection", values)
    return tmp_path, meta.relpath, meta.checksum, values


def _assert_value_equal(
    actual: dict[str, dict[str, float | None]],
    expected: dict[str, dict[str, float | None]],
) -> None:
    assert actual.keys() == expected.keys()
    for day, expected_row in expected.items():
        assert actual[day].keys() == expected_row.keys()
        for symbol, expected_value in expected_row.items():
            got = actual[day][symbol]
            if isinstance(expected_value, float) and math.isnan(expected_value):
                assert isinstance(got, float)
                assert math.isnan(got)
            else:
                assert got == expected_value


def test_projection_matches_full_mapping_and_preserves_missing_and_null(
    tmp_path: Path,
) -> None:
    root, relpath, checksum, values = _artifact(tmp_path)
    full = LazySeriesValues(root, relpath, checksum)
    projected = LazySeriesValues(root, relpath, checksum)

    _assert_value_equal(dict(full.items()), values)
    projected.project_dates(
        [date(2024, 1, 2), date(2024, 1, 4), date(2024, 2, 1)]
    )
    _assert_value_equal(
        dict(projected.items()),
        {
            "2024-01-02": values["2024-01-02"],
            "2024-01-04": values["2024-01-04"],
        },
    )
    assert len(projected) == 2
    assert "2024-02-01" not in projected
    assert projected.get("2024-02-01") is None
    assert projected["2024-01-02"]["000001.SZ"] is None


def test_projection_keeps_declared_empty_cross_section_as_missing(
    tmp_path: Path,
) -> None:
    meta = write_series_artifact(
        tmp_path,
        "v2-empty-cross-section",
        {"2024-01-01": {}, "2024-01-02": {"000001.SZ": 1.0}},
    )
    projected = LazySeriesValues(tmp_path, meta.relpath, meta.checksum)
    projected.project_dates([date(2024, 1, 1), date(2024, 1, 2)])
    assert projected.get("2024-01-01") is None
    assert projected.get("2024-01-02") == {"000001.SZ": 1.0}


def test_projection_uses_arrow_filter_without_full_batch_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, relpath, checksum, _values = _artifact(tmp_path)
    calls: list[object] = []
    real_read_table = pq.read_table

    def read_table(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        calls.append(kwargs.get("filters"))
        return real_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", read_table)
    projected = LazySeriesValues(root, relpath, checksum)
    projected.project_dates([date(2024, 1, 3)])
    assert calls == [[("date", "in", [date(2024, 1, 3)])]]
    assert set(projected) == {"2024-01-03"}


def test_projection_is_one_time_and_concurrent_gets_do_not_reload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, relpath, checksum, _values = _artifact(tmp_path)
    real_read_table = pq.read_table
    calls = 0

    def read_table(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", read_table)
    projected = LazySeriesValues(root, relpath, checksum)
    projected.project_dates([date(2024, 1, 1), date(2024, 1, 3)])
    async def concurrent_gets() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(projected.get, day)
                for day in ("2024-01-01", "2024-01-03", "2024-01-01")
            )
        )

    asyncio.run(concurrent_gets())
    projected.project_dates([date(2024, 1, 3), date(2024, 1, 1)])
    assert calls == 1
    with pytest.raises(FactorSeriesArtifactError, match="另一组决策日期"):
        projected.project_dates([date(2024, 1, 2)])


def test_concurrent_projection_installs_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, relpath, checksum, _values = _artifact(tmp_path)
    real_read_table = pq.read_table
    calls = 0

    def read_table(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return real_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", read_table)
    projected = LazySeriesValues(root, relpath, checksum)

    async def concurrent_projection() -> None:
        await asyncio.gather(
            *(
                asyncio.to_thread(
                    projected.project_dates, [date(2024, 1, 1), date(2024, 1, 3)]
                )
                for _ in range(4)
            )
        )

    asyncio.run(concurrent_projection())
    assert calls == 1
    assert set(projected) == {"2024-01-01", "2024-01-03"}


def test_projection_failure_can_retry_without_partial_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, relpath, checksum, _values = _artifact(tmp_path)
    real_read_table = pq.read_table
    calls = 0

    def flaky(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient parquet read")
        return real_read_table(*args, **kwargs)

    monkeypatch.setattr(pq, "read_table", flaky)
    projected = LazySeriesValues(root, relpath, checksum)
    with pytest.raises(OSError, match="transient"):
        projected.project_dates([date(2024, 1, 1)])
    projected.project_dates([date(2024, 1, 1)])
    assert projected.get("2024-01-01") is not None
    assert calls == 2


def test_loader_rejects_checksum_mismatch_and_metadata_missing_date(
    tmp_path: Path,
) -> None:
    _root, _relpath, checksum, _values = _artifact(tmp_path)
    ref = FrozenArtifactRef("FS-projection", "v1", checksum)

    async def run(record: object) -> None:
        async def provider(_series_id: str) -> object:
            return record

        loader = FrozenInputLoader(
            release_provider_factory=lambda _release_id: None,  # type: ignore[return-value]
            snapshot_provider=lambda _snapshot_id: None,  # type: ignore[return-value]
            series_provider=provider,  # type: ignore[arg-type]
        )
        await loader.project_factor_series_dates((ref,), (date(2024, 1, 1),))

    mismatch = SimpleNamespace(
        content_checksum="different",
        dates=(date(2024, 1, 1),),
        values=LazySeriesValues(_root, _relpath, checksum),
    )
    with pytest.raises(ValueError, match="校验和"):
        asyncio.run(run(mismatch))

    missing = SimpleNamespace(
        content_checksum=checksum,
        dates=(date(2024, 1, 2),),
        values=LazySeriesValues(_root, _relpath, checksum),
    )
    with pytest.raises(ValueError, match="缺少冻结决策日期"):
        asyncio.run(run(missing))


def test_loader_caches_record_when_provider_returns_new_instances() -> None:
    calls = 0

    async def scenario() -> None:
        nonlocal calls

        async def provider(_series_id: str) -> object:
            nonlocal calls
            calls += 1
            return SimpleNamespace()

        loader = FrozenInputLoader(
            release_provider_factory=lambda _release_id: None,  # type: ignore[return-value]
            snapshot_provider=lambda _snapshot_id: None,  # type: ignore[return-value]
            series_provider=provider,  # type: ignore[arg-type]
        )
        assert await loader._get_series_record("FS-new-instance") is not None
        assert await loader._get_series_record("FS-new-instance") is not None

    asyncio.run(scenario())
    assert calls == 1
