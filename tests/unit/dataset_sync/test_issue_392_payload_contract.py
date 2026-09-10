"""``kind=dataset_sync`` 入队期 payload 契约(issue #392)。

与 REST ``POST /api/jobs`` / MCP ``finboard_job_enqueue`` / 执行器重放共用
``validate_job_payload``;scope 四元组校验经共享解析(与 bulk_download 同函数)。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_dataset_sync_payload,
    validate_job_payload,
)

pytestmark = pytest.mark.unit

_VALID: dict[str, object] = {
    "start_date": "2026-07-24",
    "end_date": "2026-07-27",
    "symbols": ["000001.SZ"],
}


class TestValidPayloads:
    def test_valid_payload_passes(self) -> None:
        validate_dataset_sync_payload(_VALID)

    def test_dispatch_via_validate_job_payload(self) -> None:
        validate_job_payload("dataset_sync", _VALID)

    def test_datasets_omitted_passes(self) -> None:
        validate_dataset_sync_payload(dict(_VALID))

    def test_scope_filters_pass(self) -> None:
        validate_dataset_sync_payload(
            {
                **_VALID,
                "symbols": None,
                "exchange": "sse",
                "listing_boards": ["STAR"],
                "instrument_type": "Stock",
            }
        )

    def test_date_object_accepted(self) -> None:
        validate_dataset_sync_payload(
            {**_VALID, "start_date": date(2026, 7, 24), "end_date": date(2026, 7, 27)}
        )


class TestRejections:
    def test_unknown_key_rejected(self) -> None:
        with pytest.raises(PayloadContractError, match="data_types") as exc_info:
            validate_dataset_sync_payload({**_VALID, "data_types": ["profiles"]})
        assert exc_info.value.code == "unknown_payload_key"

    def test_missing_start_date_rejected(self) -> None:
        payload = dict(_VALID)
        del payload["start_date"]
        with pytest.raises(PayloadContractError) as exc_info:
            validate_dataset_sync_payload(payload)
        assert exc_info.value.code == "missing_required_field"

    def test_missing_end_date_rejected(self) -> None:
        payload = dict(_VALID)
        del payload["end_date"]
        with pytest.raises(PayloadContractError) as exc_info:
            validate_dataset_sync_payload(payload)
        assert exc_info.value.code == "missing_required_field"

    def test_bad_date_format_rejected(self) -> None:
        # 注:Python 3.11+ 的 fromisoformat 接受 "20260724" 基本型;这里用
        # 真非法值锁定拒绝行为。
        with pytest.raises(PayloadContractError) as exc_info:
            validate_dataset_sync_payload({**_VALID, "start_date": "not-a-date"})
        assert exc_info.value.code == "invalid_field_value"

    def test_start_after_end_rejected(self) -> None:
        with pytest.raises(PayloadContractError, match="不能晚于"):
            validate_dataset_sync_payload(
                {**_VALID, "start_date": "2026-07-27", "end_date": "2026-07-24"}
            )

    def test_unknown_dataset_rejected(self) -> None:
        with pytest.raises(PayloadContractError, match="suspensions"):
            validate_dataset_sync_payload({**_VALID, "datasets": ["suspensions"]})

    @pytest.mark.parametrize(
        ("key", "value"),
        [
            ("datasets", "profiles"),
            ("symbols", "000001.SZ"),
            ("exchange", ["SSE"]),
            ("listing_boards", "star"),
            ("instrument_type", 1),
        ],
    )
    def test_field_wrong_type_rejected(
        self, key: str, value: object
    ) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_dataset_sync_payload({**_VALID, key: value})
        assert exc_info.value.code == "invalid_field_value"


class TestEmptySymbolPoolGuard:
    def test_per_symbol_without_any_pool_source_rejected(self) -> None:
        with pytest.raises(PayloadContractError, match="symbol 池将解析为空") as exc_info:
            validate_dataset_sync_payload(
                {"start_date": "2026-07-24", "end_date": "2026-07-27",
                 "datasets": ["financial_indicators"]}
            )
        assert exc_info.value.code == "empty_symbol_pool"

    def test_per_symbol_with_symbols_passes(self) -> None:
        validate_dataset_sync_payload(
            {**_VALID, "datasets": ["financial_indicators"]}
        )

    def test_per_symbol_with_universe_filters_passes(self) -> None:
        validate_dataset_sync_payload(
            {
                "start_date": "2026-07-24",
                "end_date": "2026-07-27",
                "datasets": ["industry_memberships"],
                "instrument_type": "stock",
            }
        )

    def test_per_symbol_with_profiles_passes(self) -> None:
        validate_dataset_sync_payload(
            {
                "start_date": "2026-07-24",
                "end_date": "2026-07-27",
                "datasets": ["profiles", "financial_indicators"],
            }
        )

    def test_empty_symbols_list_behaves_as_omitted(self) -> None:
        # 旧契约口径:空列表 = 未声明(缺省回落 profiles / 宇宙过滤)。
        with pytest.raises(PayloadContractError, match="symbol 池将解析为空") as exc_info:
            validate_dataset_sync_payload(
                {"start_date": "2026-07-24", "end_date": "2026-07-27",
                 "symbols": [], "datasets": ["financial_indicators"]}
            )
        assert exc_info.value.code == "empty_symbol_pool"
