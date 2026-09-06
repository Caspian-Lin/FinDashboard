"""因子序列内容寻址键与覆盖 helper 的纯函数测试(issue #360)。

覆盖:series_key 对 params dict 键序 / 日期序列化形态漂移免疫、canonical
规则逐段拼接、content_checksum 的 date/ISO 双态一致、series_coverage_missing、
Record 构造校验(series_key / series_id / content_checksum 防漂移)。
"""

from __future__ import annotations

import hashlib
from datetime import date
from typing import Any

import pytest

from finboard_persistence.factor_series_repo import (
    FactorSeriesConflictError,
    FactorSeriesRecord,
    compute_content_checksum,
    compute_series_key,
    series_coverage_missing,
    series_id_for,
)

_COMMIT = "c" * 40
_RELEASE = "DR-bars-1"
_JOINT = ["DR-fin-1", "DR-daily-1"]
_PARAMS = {"window": 20, "min_periods": 5}


class TestSeriesKey:
    def test_matches_issue_canonical_rule(self) -> None:
        """逐段拼接规则与 issue #360 文本一致(v2 前缀 + sorted 联合集)。"""
        key = compute_series_key(
            code_commit=_COMMIT,
            release_id=_RELEASE,
            dataset_release_ids=_JOINT,
            params=_PARAMS,
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        expected_payload = "|".join(
            (
                "v2",
                _COMMIT,
                _RELEASE,
                ",".join(sorted(_JOINT)),
                '{"min_periods":5,"window":20}',
                "2022-01-01",
                "2023-06-30",
            )
        )
        assert key == hashlib.sha256(
            expected_payload.encode("utf-8")
        ).hexdigest()

    def test_params_dict_order_immune(self) -> None:
        """params dict 键序漂移(sort_keys canonical)产出同一 series_key。"""
        a = compute_series_key(
            code_commit=_COMMIT,
            release_id=_RELEASE,
            dataset_release_ids=_JOINT,
            params={"window": 20, "min_periods": 5},
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        b = compute_series_key(
            code_commit=_COMMIT,
            release_id=_RELEASE,
            dataset_release_ids=_JOINT,
            params={"min_periods": 5, "window": 20},
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        assert a == b

    def test_joint_release_order_immune(self) -> None:
        a = compute_series_key(
            code_commit=_COMMIT,
            release_id=_RELEASE,
            dataset_release_ids=["DR-a", "DR-b"],
            params=_PARAMS,
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        b = compute_series_key(
            code_commit=_COMMIT,
            release_id=_RELEASE,
            dataset_release_ids=["DR-b", "DR-a"],
            params=_PARAMS,
            window_start=date(2022, 1, 1),
            window_end=date(2023, 6, 30),
        )
        assert a == b

    def test_any_segment_changes_key(self) -> None:
        def _key(**overrides: Any) -> str:
            return compute_series_key(
                code_commit=overrides.get("code_commit", _COMMIT),
                release_id=overrides.get("release_id", _RELEASE),
                dataset_release_ids=overrides.get("dataset_release_ids", _JOINT),
                params=overrides.get("params", _PARAMS),
                window_start=overrides.get("window_start", date(2022, 1, 1)),
                window_end=overrides.get("window_end", date(2023, 6, 30)),
            )

        original = _key()
        # 换 bars 主发布 / 换 commit / 换窗口 / 换 params 各自成新键。
        assert _key(release_id="DR-bars-2") != original
        assert _key(code_commit="d" * 40) != original
        assert _key(window_end=date(2023, 7, 1)) != original
        assert _key(params={"window": 5}) != original


class TestContentChecksum:
    def test_date_and_iso_key_agree(self) -> None:
        dates = [date(2024, 1, 31), date(2024, 2, 29)]
        by_date: dict[Any, dict[str, float | None]] = {
            date(2024, 1, 31): {"600000.SH": 1.5}
        }
        by_iso = {"2024-01-31": {"600000.SH": 1.5}}
        assert compute_content_checksum(
            dates, by_date
        ) == compute_content_checksum(dates, by_iso)

    def test_symbol_order_within_day_immune(self) -> None:
        day_a = {"600000.SH": 1.5, "000001.SZ": 2.5}
        day_b = {"000001.SZ": 2.5, "600000.SH": 1.5}
        assert compute_content_checksum(
            [date(2024, 1, 31)], {"2024-01-31": day_a}
        ) == compute_content_checksum([date(2024, 1, 31)], {"2024-01-31": day_b})

    def test_null_value_changes_checksum(self) -> None:
        assert compute_content_checksum(
            [date(2024, 1, 31)], {"2024-01-31": {"600000.SH": None}}
        ) != compute_content_checksum(
            [date(2024, 1, 31)], {"2024-01-31": {"600000.SH": 0.0}}
        )


class TestCoverageMissing:
    def test_returns_uncovered_dates_sorted(self) -> None:
        record = _record(dates=[date(2024, 1, 31), date(2024, 2, 29)])
        missing = series_coverage_missing(
            record,
            [date(2024, 3, 29), date(2024, 1, 31), date(2024, 2, 15)],
        )
        assert missing == [date(2024, 2, 15), date(2024, 3, 29)]

    def test_full_coverage_empty(self) -> None:
        record = _record(dates=[date(2024, 1, 31)])
        assert series_coverage_missing(record, [date(2024, 1, 31)]) == []


class TestRecordValidation:
    def test_series_id_derivation(self) -> None:
        assert series_id_for("a" * 64) == "FS-" + "a" * 12

    def test_build_derives_all_content_addresses(self) -> None:
        record = _record()
        assert record.series_id == series_id_for(record.series_key)
        assert record.series_key == compute_series_key(
            code_commit=record.code_commit,
            release_id=record.release_id,
            dataset_release_ids=record.dataset_release_ids,
            params=record.params,
            window_start=record.window_start,
            window_end=record.window_end,
        )

    def test_tampered_series_key_rejected(self) -> None:
        from dataclasses import replace

        record = _record()
        with pytest.raises(ValueError, match="series_key"):
            replace(record, series_key="f" * 64)

    def test_tampered_content_checksum_rejected(self) -> None:
        from dataclasses import replace

        record = _record()
        with pytest.raises(ValueError, match="content_checksum"):
            replace(record, content_checksum="e" * 64)

    def test_conflict_error_is_runtime_error(self) -> None:
        # 确定性破坏分类可供上层 fail-closed 捕获(#204 复查先例语义)。
        assert issubclass(FactorSeriesConflictError, RuntimeError)


def _record(*, dates: list[date] | None = None) -> FactorSeriesRecord:
    effective_dates = dates or [date(2024, 1, 31), date(2024, 2, 29)]
    values = {day.isoformat(): {"600000.SH": float(i)} for i, day in enumerate(effective_dates)}
    return FactorSeriesRecord.build(
        code_artifact="mom20",
        code_commit=_COMMIT,
        kind="factor",
        release_id=_RELEASE,
        dataset_release_ids=_JOINT,
        params=_PARAMS,
        window_start=date(2022, 1, 1),
        window_end=date(2023, 6, 30),
        dates=effective_dates,
        values=values,
        quality={"nan_ratio": 0.0, "coverage": 1.0},
        source_run_id="RCR-test",
    )
