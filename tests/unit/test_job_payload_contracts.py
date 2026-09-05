"""per-kind job payload 入队期契约校验测试(issue #260;#347 加 bulk_download)。

覆盖 ``validate_job_payload`` / ``validate_research_data_sync_payload`` /
``validate_bulk_download_payload``:未知键(``data_types`` 类拼写错误)、
缺 ``start_date`` / ``end_date``、``datasets`` 枚举非法、空 symbol 池拒绝、
未注册 kind 放行(向后兼容)、bulk_download 的 source 白名单 / 坏日期 /
tusharexetf|futures 字面量预检 / symbols 子集类型校验。
REST(``test_api_jobs.py``)/ MCP(``test_mcp_tools_jobs.py``)/ 执行器
(``test_background_jobs_data_executors.py``)三侧接入行为在各自文件覆盖。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.background_jobs.payload_contracts import (
    PayloadContractError,
    validate_job_payload,
    validate_research_data_sync_payload,
)

_VALID: dict[str, object] = {
    "start_date": "2026-01-01",
    "end_date": "2026-01-31",
    "datasets": ["profiles", "daily_metrics"],
}


class TestResearchDataSyncPayloadContract:
    def test_valid_payload_passes(self) -> None:
        validate_research_data_sync_payload(_VALID)

    def test_datasets_omitted_passes(self) -> None:
        """datasets 缺省 = 全部五类(含 profiles),symbols 可省略。"""
        validate_research_data_sync_payload(
            {"start_date": "2026-01-01", "end_date": "2026-01-31"}
        )

    def test_unknown_key_rejected(self) -> None:
        """反馈原样复现:data_types 是不存在的键,入队即拒而非静默忽略。"""
        payload = {**_VALID, "data_types": ["financial_indicators"]}
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(payload)
        assert exc_info.value.code == "unknown_payload_key"
        assert "data_types" in exc_info.value.summary
        assert "datasets" in exc_info.value.summary  # 指出正确参数名

    def test_missing_start_date_rejected(self) -> None:
        payload = {k: v for k, v in _VALID.items() if k != "start_date"}
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(payload)
        assert exc_info.value.code == "missing_required_field"
        assert "start_date" in exc_info.value.summary

    def test_missing_end_date_rejected(self) -> None:
        payload = {k: v for k, v in _VALID.items() if k != "end_date"}
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(payload)
        assert exc_info.value.code == "missing_required_field"

    def test_bad_date_format_rejected(self) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(
                {**_VALID, "start_date": "2026/01/01"}
            )
        assert exc_info.value.code == "invalid_field_value"

    def test_date_object_accepted(self) -> None:
        """执行器重放场景:payload 可能已解析为 date 对象。"""
        validate_research_data_sync_payload(
            {
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 31),
                "datasets": ["profiles"],
            }
        )

    def test_start_after_end_rejected(self) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(
                {**_VALID, "start_date": "2026-02-01", "end_date": "2026-01-01"}
            )
        assert exc_info.value.code == "invalid_field_value"

    def test_unknown_dataset_rejected(self) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(
                {**_VALID, "datasets": ["orders"]}
            )
        assert exc_info.value.code == "invalid_field_value"
        assert "orders" in exc_info.value.summary

    def test_datasets_wrong_type_rejected(self) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload({**_VALID, "datasets": "daily_metrics"})
        assert exc_info.value.code == "invalid_field_value"

    def test_symbols_wrong_type_rejected(self) -> None:
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload({**_VALID, "symbols": "000001.SZ"})
        assert exc_info.value.code == "invalid_field_value"

    def test_per_symbol_without_symbols_and_profiles_rejected(self) -> None:
        """空 symbol 池:逐标的数据集缺 symbols 且缺 profiles → 入队即拒。"""
        with pytest.raises(PayloadContractError) as exc_info:
            validate_research_data_sync_payload(
                {
                    "start_date": "2026-01-01",
                    "end_date": "2026-03-31",
                    "datasets": ["financial_indicators", "industry_memberships"],
                }
            )
        assert exc_info.value.code == "empty_symbol_pool"
        assert "profiles" in exc_info.value.summary

    def test_per_symbol_with_profiles_passes(self) -> None:
        """缺 symbols 但 profiles 在 datasets 里 → symbol 池由 profiles 解析。"""
        validate_research_data_sync_payload(
            {
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
                "datasets": ["profiles", "financial_indicators"],
            }
        )

    def test_per_symbol_with_symbols_passes(self) -> None:
        validate_research_data_sync_payload(
            {
                "start_date": "2026-01-01",
                "end_date": "2026-03-31",
                "datasets": ["financial_indicators"],
                "symbols": ["000001.SZ", "600000.SH"],
            }
        )


class TestRegistry:
    def test_unregistered_kind_is_noop(self) -> None:
        """未注册 kind 保持 payload 自由结构(向后兼容,不必一次性全做)。"""
        validate_job_payload("echo", {"anything": ["goes", "here"], "x": 1})
        validate_job_payload("definitely_new_kind", {"data_types": 1})

    def test_none_payload_is_noop(self) -> None:
        validate_job_payload("research_data_sync", None)

    def test_registered_kind_dispatches(self) -> None:
        with pytest.raises(PayloadContractError):
            validate_job_payload(
                "research_data_sync",
                {"data_types": ["daily_metrics"]},
            )


_BULK_VALID: dict[str, object] = {
    "market": "a_share",
    "source": "akshare",
    "start": "2024-01-01",
}


class TestBulkDownloadPayloadContract:
    """``kind=bulk_download`` 入队期契约(issue #347)。"""

    def test_valid_payload_passes(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        validate_bulk_download_payload(_BULK_VALID)

    def test_empty_source_passes(self) -> None:
        """空串 = 回落配置默认源(#341 跟进语义),不得拒绝。"""
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        validate_bulk_download_payload({**_BULK_VALID, "source": ""})
        validate_bulk_download_payload(
            {k: v for k, v in _BULK_VALID.items() if k != "source"}
        )

    def test_source_case_insensitive(self) -> None:
        """source 大小写不敏感(与 resolve_provider_name 的 lower 口径一致)。"""
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        validate_bulk_download_payload({**_BULK_VALID, "source": "AkShare"})

    def test_unknown_source_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "source": "wind"})
        assert exc_info.value.code == "invalid_field_value"
        assert "wind" in exc_info.value.summary
        assert "akshare" in exc_info.value.summary  # 列出可用源

    def test_source_wrong_type_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "source": 123})
        assert exc_info.value.code == "invalid_field_value"

    def test_missing_market_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        payload = {k: v for k, v in _BULK_VALID.items() if k != "market"}
        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(payload)
        assert exc_info.value.code == "missing_required_field"

    def test_empty_market_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "market": ""})
        assert exc_info.value.code == "missing_required_field"

    def test_bad_start_date_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "start": "2024/01/01"})
        assert exc_info.value.code == "invalid_field_value"
        assert "start" in exc_info.value.summary

    def test_missing_start_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        payload = {k: v for k, v in _BULK_VALID.items() if k != "start"}
        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(payload)
        assert exc_info.value.code == "missing_required_field"

    def test_instrument_type_and_exchange_type_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError):
            validate_bulk_download_payload(
                {**_BULK_VALID, "instrument_type": ["stock"]}
            )
        with pytest.raises(PayloadContractError):
            validate_bulk_download_payload({**_BULK_VALID, "exchange": 1})

    def test_listing_boards_wrong_type_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(
                {**_BULK_VALID, "listing_boards": "szse_main"}
            )
        assert exc_info.value.code == "invalid_field_value"

    def test_symbols_wrong_type_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(
                {**_BULK_VALID, "symbols": "000001.SZ"}
            )
        assert exc_info.value.code == "invalid_field_value"

    def test_symbols_with_non_string_item_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError):
            validate_bulk_download_payload(
                {**_BULK_VALID, "symbols": ["000001.SZ", 600000]}
            )

    def test_empty_symbols_list_rejected(self) -> None:
        """显式空列表几乎必然是调用方笔误,fail-visible(缺省不传 = 全池)。"""
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "symbols": []})
        assert exc_info.value.code == "empty_symbol_pool"

    def test_symbols_subset_passes(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        validate_bulk_download_payload(
            {**_BULK_VALID, "symbols": ["000001.SZ", "600000.SH"]}
        )

    def test_tushare_etf_literal_precheck_rejected(self) -> None:
        """tushare x ETF 字面量预检:复权口径对齐未定稿(#341),入队即拒。"""
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(
                {**_BULK_VALID, "source": "tushare", "instrument_type": "etf"}
            )
        assert exc_info.value.code == "tushare_scope_mismatch"
        assert "akshare" in exc_info.value.summary  # 指路替代源

    def test_tushare_futures_literal_precheck_rejected(self) -> None:
        """tushare x 期货字面量预检(fut_daily 未接线,#267)。"""
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload(
                {
                    "market": "future",
                    "source": "tushare",
                    "start": "2024-01-01",
                    "instrument_type": "futures",
                }
            )
        assert exc_info.value.code == "tushare_scope_mismatch"

    def test_tushare_stock_and_index_pass(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        validate_bulk_download_payload(
            {**_BULK_VALID, "instrument_type": "stock"}
        )
        validate_bulk_download_payload(
            {**_BULK_VALID, "instrument_type": "index"}
        )

    def test_unknown_key_rejected(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            validate_bulk_download_payload,
        )

        with pytest.raises(PayloadContractError) as exc_info:
            validate_bulk_download_payload({**_BULK_VALID, "data_types": 1})
        assert exc_info.value.code == "unknown_payload_key"

    def test_registered_in_registry(self) -> None:
        from finboard_backtest.background_jobs.payload_contracts import (
            PAYLOAD_CONTRACTS,
        )

        assert PAYLOAD_CONTRACTS["bulk_download"].__name__ == (
            "validate_bulk_download_payload"
        )
        with pytest.raises(PayloadContractError):
            validate_job_payload("bulk_download", {"market": "a_share"})
