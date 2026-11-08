"""DataScope 契约与 manifest 冻结(issue #510 单元测试,不需 DB)。

覆盖:字段校验 / locked 约束 / canonical checksum 稳定 / 解析结论冻结负载 /
``ResearchRunManifest.data_scope`` 缺省时 checksum 零漂移(旧入口不变)。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from finboard_backtest.data_scope import (
    DataScopeSpec,
    ResolvedRelease,
    ScopeGaps,
    ScopeResolution,
    ScopeUniverseResult,
    ScopeWindow,
    scope_checksum,
)
from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchActorType,
    ResearchRunManifest,
    manifest_from_json,
    stable_checksum,
    to_json_value,
)
from finboard_data.datasets import CatalogDataset


def _spec(**overrides: object) -> DataScopeSpec:
    payload: dict[str, object] = {
        "universe": {"explicit_symbols": ["000001.SZ"]},
        "decision": {"start": "2024-01-02", "end": "2024-03-28"},
        "warmup_sessions": 20,
        "tail_sessions": 5,
        "benchmarks": ["000300.SH"],
        "datasets": {"required": ["bars", "daily_metrics"], "optional": ["dividends"]},
        "bars_adjustment": "qfq",
    }
    payload.update(overrides)
    return DataScopeSpec.model_validate(payload)


class TestSpecValidation:
    def test_required_must_contain_bars(self) -> None:
        with pytest.raises(ValidationError, match="必须包含 bars"):
            DataScopeSpec.model_validate(
                {
                    "universe": {},
                    "decision": {"start": "2024-01-02", "end": "2024-01-31"},
                    "datasets": {"required": ["daily_metrics"]},
                }
            )

    def test_required_optional_cannot_overlap(self) -> None:
        with pytest.raises(ValidationError, match="不能重叠"):
            DataScopeSpec.model_validate(
                {
                    "universe": {},
                    "decision": {"start": "2024-01-02", "end": "2024-01-31"},
                    "datasets": {
                        "required": ["bars"],
                        "optional": ["bars"],
                    },
                }
            )

    def test_locked_requires_release_ids(self) -> None:
        with pytest.raises(ValidationError, match="locked_release_ids"):
            _spec(release_selection="locked")

    def test_decision_order_rejected(self) -> None:
        with pytest.raises(ValidationError, match="不能晚于"):
            DataScopeSpec.model_validate(
                {
                    "universe": {},
                    "decision": {"start": "2024-03-01", "end": "2024-01-01"},
                    "datasets": {"required": ["bars"]},
                }
            )


class TestChecksum:
    def test_stable_across_key_order(self) -> None:
        left = _spec()
        right = DataScopeSpec.model_validate(
            {
                "datasets": {"optional": ["dividends"], "required": ["daily_metrics", "bars"]},
                "bars_adjustment": "qfq",
                "benchmarks": ["000300.SH"],
                "tail_sessions": 5,
                "warmup_sessions": 20,
                "decision": {"end": "2024-03-28", "start": "2024-01-02"},
                "universe": {"explicit_symbols": ["000001.SZ"]},
            }
        )
        assert scope_checksum(left) == scope_checksum(right)

    def test_content_change_changes_checksum(self) -> None:
        assert scope_checksum(_spec()) != scope_checksum(_spec(warmup_sessions=21))

    def test_canonical_payload_keeps_empty_collections(self) -> None:
        payload = _spec().canonical_payload()
        assert payload["fields"] == []
        datasets = payload["datasets"]
        assert isinstance(datasets, dict)
        assert datasets["optional"] == ["dividends"]


def _resolution() -> ScopeResolution:
    return ScopeResolution(
        resolution_id="SC-0123456789abcdef",
        scope_checksum="f" * 64,
        as_of=datetime(2024, 1, 1, tzinfo=UTC),
        selection_mode="auto",
        window=ScopeWindow(
            warmup_start=date(2023, 12, 1),
            decision_start=date(2024, 1, 2),
            decision_end=date(2024, 3, 28),
            valuation_end=date(2024, 4, 8),
            warmup_sessions=20,
            decision_sessions=60,
            tail_sessions=5,
            calendar_markets=("SSE",),
            calendar_source="trade_cal",
        ),
        releases=(
            ResolvedRelease(
                artifact_id="DR-bars-1",
                dataset=CatalogDataset.BARS,
                role="bars_primary",
                version="v1",
                checksum="a" * 64,
                published_at=datetime(2024, 1, 1, tzinfo=UTC),
                start_date=date(2023, 12, 1),
                end_date=date(2024, 4, 8),
                coverage_pct=Decimal("0.99"),
                symbol_count=6,
                reason="窗口覆盖 + 标的齐全",
            ),
        ),
        universe=ScopeUniverseResult(
            requested_count=1,
            resolved_count=1,
            truncated=False,
            symbols_preview=("000001.SZ",),
        ),
        gaps=ScopeGaps(),
        warnings=("预热不足:请求 30 个交易日",),
    )


class TestManifestPayload:
    def test_payload_shape(self) -> None:
        spec = _spec()
        payload = _resolution().manifest_payload(spec)
        assert payload["resolution_id"] == "SC-0123456789abcdef"
        assert payload["scope_checksum"] == "f" * 64
        assert payload["window"] == {
            "warmup_start": "2023-12-01",
            "decision_start": "2024-01-02",
            "decision_end": "2024-03-28",
            "valuation_end": "2024-04-08",
            "warmup_sessions": 20,
            "decision_sessions": 60,
            "tail_sessions": 5,
            "calendar_markets": ["SSE"],
            "calendar_source": "trade_cal",
        }
        assert payload["releases"] == [
            {
                "artifact_id": "DR-bars-1",
                "dataset": "bars",
                "role": "bars_primary",
                "version": "v1",
                "checksum": "a" * 64,
            }
        ]
        # scope 原样冻结(canonical payload),便于事后复算 checksum。
        assert payload["scope"] == spec.canonical_payload()
        assert payload["warnings"] == ["预热不足:请求 30 个交易日"]

    def test_run_parameters_are_window_dates(self) -> None:
        assert _resolution().run_parameters() == {
            "warmup_start": "2023-12-01",
            "decision_start": "2024-01-02",
            "decision_end": "2024-03-28",
            "valuation_end": "2024-04-08",
        }


# ---------------------------------------------------------------------------
# manifest:data_scope 可选键的 checksum 语义
# ---------------------------------------------------------------------------


def _manifest(**overrides: object) -> ResearchRunManifest:
    from finboard_backtest.strategy_spec import build_strategy_template

    spec = build_strategy_template(
        "ma_cross",
        strategy_id="ma_cross_research",
        dataset_release_ids=("release-v1",),
    )
    kwargs: dict[str, object] = {
        "run_id": "RR-510-unit-000001",
        "idempotency_key": "idem-510-unit-0001",
        "strategy_spec": spec,
        "strategy_spec_checksum": stable_checksum(spec.canonical_payload()),
        "dataset_releases": (
            FrozenArtifactRef(
                artifact_id="release-v1",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        "code_version": "abcdef0123456789",
        "requested_by": "unit-test",
        "actor_type": ResearchActorType.HUMAN,
    }
    kwargs.update(overrides)
    return ResearchRunManifest(**kwargs)  # type: ignore[arg-type]


class TestManifestChecksum:
    def test_absent_scope_matches_explicit_none(self) -> None:
        # 旧入口(不传)与显式 None 是同一份清单:checksum/input_checksum 一致。
        implicit = _manifest()
        explicit = _manifest(data_scope=None)
        assert implicit.checksum == explicit.checksum
        assert implicit.input_checksum == explicit.input_checksum

    def test_scope_enters_checksums(self) -> None:
        scoped = _manifest(data_scope={"resolution_id": "SC-1", "scope_checksum": "f" * 64})
        plain = _manifest()
        assert scoped.checksum != plain.checksum
        assert scoped.input_checksum != plain.input_checksum

    def test_scope_round_trips_through_json(self) -> None:
        scope = _resolution().manifest_payload(_spec())
        manifest = _manifest(data_scope=scope)
        payload = to_json_value(manifest)
        restored = manifest_from_json(payload)  # type: ignore[arg-type]
        assert restored.data_scope == scope
        assert restored.checksum == manifest.checksum

    def test_replaced_scope_rejected_on_replay_of_checksum(self) -> None:
        manifest = _manifest(data_scope={"resolution_id": "SC-1", "scope_checksum": "f" * 64})
        tampered = replace(manifest, data_scope={"resolution_id": "SC-2", "scope_checksum": "f" * 64})
        assert tampered.checksum != manifest.checksum
