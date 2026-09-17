"""issue #476:冻结候选池/特征截面原生 canonical 编码回归。"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from finboard_backtest.research_run.contracts import (
    FeatureValue,
    ResearchArtifact,
    ResearchRunStage,
    UniverseCandidate,
    canonical_json_list_text,
    canonical_json_normalized,
    canonical_json_text,
    stable_checksum,
    to_json_value,
)


def _legacy(value: object) -> str:
    return canonical_json_normalized(to_json_value(value))


def _candidate(index: int) -> UniverseCandidate:
    return UniverseCandidate(
        symbol=f"{600000 + index:06d}.SH",
        included=index % 2 == 0,
        reasons=("通过" if index % 2 == 0 else "停牌", "特殊\t字符🚀"),
        asset_class="equity",
        market="SH",
    )


def _feature(index: int, value: float | None) -> FeatureValue:
    return FeatureValue(
        symbol=f"{index:06d}.SZ",
        feature_id="return_21d",
        value=value,
        source_artifact_ids=("DR-2020", "FA-1"),
        available_at=datetime(2020, 1, 2, 15, 0, 0, 123000, tzinfo=UTC),
    )


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4096, 8192])
def test_native_candidate_path_is_byte_identical_at_chunk_boundaries(chunk_size: int) -> None:
    items = [_candidate(index) for index in range(4097)]
    assert canonical_json_list_text(items, chunk_size=chunk_size) == _legacy(items)


@pytest.mark.parametrize("value", [None, 1e-5, 1e300, 5e-324, -0.0, 0.1])
def test_native_feature_path_reproduces_python_float_and_date_text(value: float | None) -> None:
    items = [_feature(0, value), _feature(1, value)]
    assert canonical_json_list_text(items, chunk_size=1) == _legacy(items)


def test_empty_and_mixed_sections_use_existing_generic_path() -> None:
    assert canonical_json_list_text([]) == "[]"
    mixed: list[object] = [_candidate(0), {"symbol": "not-a-candidate", "value": 1e-5}]
    assert canonical_json_list_text(mixed) == _legacy(mixed)


def test_unsupported_chunk_size_fails_closed_or_is_bounded() -> None:
    with pytest.raises(ValueError, match="chunk_size"):
        canonical_json_list_text([], chunk_size=0)
    # Values above the bound are capped, not allowed to create an unbounded tree.
    assert canonical_json_list_text([_candidate(0)], chunk_size=10000) == _legacy([_candidate(0)])


def test_nonfinite_feature_value_fails_closed() -> None:
    item = object.__new__(FeatureValue)
    object.__setattr__(item, "symbol", "000001.SZ")
    object.__setattr__(item, "feature_id", "bad")
    object.__setattr__(item, "value", float("nan"))
    object.__setattr__(item, "source_artifact_ids", ("FA-1",))
    object.__setattr__(item, "available_at", datetime(2020, 1, 2, tzinfo=UTC))
    with pytest.raises(ValueError, match="有限"):
        canonical_json_list_text([item])


@pytest.mark.parametrize("surrogate", [chr(0xD800), chr(0xDFFF)])
def test_candidate_surrogates_match_legacy_fallback(surrogate: str) -> None:
    item = UniverseCandidate(
        symbol=surrogate,
        included=True,
        reasons=(surrogate,),
        asset_class=surrogate,
        market=surrogate,
    )
    assert canonical_json_list_text([item]) == _legacy([item])


@pytest.mark.parametrize("surrogate", [chr(0xD800), chr(0xDFFF)])
def test_feature_surrogates_match_legacy_fallback(surrogate: str) -> None:
    item = FeatureValue(
        symbol=surrogate,
        feature_id=surrogate,
        value=1e-5,
        source_artifact_ids=(surrogate,),
        available_at=datetime(2020, 1, 2, tzinfo=UTC),
    )
    assert canonical_json_list_text([item]) == _legacy([item])


def test_missing_orjson_import_keeps_stdlib_path_in_isolated_process() -> None:
    script = """
import builtins
from datetime import UTC, datetime

original_import = builtins.__import__
def import_without_orjson(name, *args, **kwargs):
    if name == "orjson":
        raise ImportError("test: orjson unavailable")
    return original_import(name, *args, **kwargs)
builtins.__import__ = import_without_orjson

from finboard_backtest.research_run.contracts import (
    UniverseCandidate,
    canonical_json_list_text,
)

item = UniverseCandidate("000001.SZ", True, ("ok",), "equity", "SZ")
assert canonical_json_list_text([item]) == (
    '[{"asset_class":"equity","included":true,"market":"SZ",'
    '"reasons":["ok"],"symbol":"000001.SZ"}]'
)
print("isolated stdlib fallback passed")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True
    )
    assert "isolated stdlib fallback passed" in completed.stdout


def test_real_artifact_payload_and_checksum_are_unchanged() -> None:
    payload = {
        "candidates": [_candidate(index) for index in range(9)],
        "features": [_feature(index, (index - 4) * 1e-5) for index in range(9)],
    }
    payload_json = canonical_json_text(payload, chunk_size=4)
    assert payload_json == _legacy(payload)
    artifact = ResearchArtifact(
        artifact_id="A-476",
        run_id="RR-476",
        decision_id="D-476",
        sequence=1,
        stage=ResearchRunStage.FEATURES,
        trace_id="T-476",
        parent_trace_ids=(),
        payload={},
        checksum=stable_checksum(payload),
        payload_json=payload_json,
    )
    assert artifact.checksum == stable_checksum(to_json_value(payload))
    assert artifact.payload_json == payload_json
