"""issue #472:决策段 canonical 编码收敛 —— 流式/片段复用的逐字节等值回归。

锁定四组不变量:

* ``canonical_json_text``(分块 + 片段拼接)与旧口径
  ``canonical_json_normalized(to_json_value(payload))`` 逐字节一致 ——
  覆盖浮点指数/大整数/负数零/非 ASCII/日期时区/None 省略键/tuple→list/
  空列表/块边界(chunk_size 1/2/3/默认);
* ``fragments`` 复用路径与整树编码一致(0 次编码的截面文本拼接不变形);
* ``DecisionBundle.canonical_fragments`` 是纯编码缓存:不进 ``to_json_value``、
  不进任何 checksum、不参与相等性,``_slim_decision`` 落地清除;
* ``ResearchArtifact.payload_json`` 权威形态在内存 store 侧按需物化,读回
  ``payload`` 与 #472 之前逐值一致。

真实 run 存量 artifact 的语料级比对见
``tests/integration/test_issue_472_artifact_bytes.py``。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from finboard_backtest.research_run import InMemoryResearchRunStore
from finboard_backtest.research_run.contracts import (
    CANONICAL_CHUNK_SIZE,
    ResearchArtifact,
    ResearchRunStage,
    _slim_decision,
    canonical_json_digest,
    canonical_json_list_text,
    canonical_json_normalized,
    canonical_json_parts,
    canonical_json_text,
    pipeline_output_checksum,
    stable_checksum,
    stable_checksum_text,
    to_json_value,
)


def _legacy_text(payload: object) -> str:
    """与落库时的旧口径逐字节对照用的编码器(#472 之前的 persist 路径)。"""

    return canonical_json_normalized(to_json_value(payload))


def _adversarial_payload() -> dict[str, object]:
    """边界类型大杂烩:浮点指数/大整数/unicode/嵌套/tuple/None/Decimal。"""

    return {
        "business_date": date(2020, 1, 31),
        "decision_at": datetime(2020, 1, 31, 15, 0, tzinfo=UTC),
        "price": Decimal("12345.6789"),
        "floats": [1e-05, 1e16, 1.5e300, -0.0, 0.1, 123456789.123456789, 5e-324],
        "big_ints": [2**63 + 1, -(2**63) - 1],
        "small_ints": [0, 1, -1, 10**20],
        "unicode": "中文字符串 émoji 🚀 制表\u0009",
        "nested": {"b": [1, {"c": None}], "a": True, "d": []},
        "tuples": (1, (2, 3)),
        "none": None,
        "flag": False,
        "empty_list": [],
    }


def _section_payload(count: int) -> dict[str, object]:
    """带全量截面键(features/candidates)的载荷。"""

    return {
        "business_date": date(2020, 2, 28),
        "decision_at": datetime(2020, 2, 28, 15, 0, tzinfo=UTC),
        "candidates": [
            {
                "symbol": f"{600000 + index:06d}.SH",
                "included": index % 3 != 0,
                "market_cap": float(index) * 1e6 + 0.5,
                "available_at": "2020-02-28",
            }
            for index in range(count)
        ],
        "features": [
            {
                "symbol": f"{index:06d}.SZ",
                "name": "return_21d" if index % 2 else "市值对数",
                "value": (index - count / 2) * 1e-05,
                "available_at": "2020-02-28",
            }
            for index in range(count)
        ],
        "signals": [{"symbol": f"{index:06d}.SZ", "score": index * 0.1} for index in range(3)],
    }


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 7, CANONICAL_CHUNK_SIZE])
def test_canonical_text_matches_legacy_bytes(chunk_size: int) -> None:
    for payload in (_adversarial_payload(), _section_payload(11)):
        assert canonical_json_text(payload, chunk_size=chunk_size) == _legacy_text(payload)


def test_canonical_list_text_matches_legacy_bytes() -> None:
    items = [*list(range(9)), 1e-05, "中文", None, (1, 2)]
    for chunk_size in (1, 2, 4, 100):
        assert canonical_json_list_text(items, chunk_size=chunk_size) == _legacy_text(items)
    assert canonical_json_list_text([], chunk_size=2) == "[]"


def test_canonical_text_capture_reuse_is_byte_identical() -> None:
    payload = _section_payload(9)
    fragments: dict[str, str] = {}
    fresh = canonical_json_text(payload, capture=fragments)
    assert sorted(fragments) == ["candidates", "features"]
    # 捕获的片段就是该列表单独编码的结果(可作为顶层任意位置的值复用)
    assert fragments["features"] == _legacy_text(payload["features"])
    assert fragments["candidates"] == _legacy_text(payload["candidates"])
    reused = canonical_json_text(payload, fragments=fragments)
    assert reused == fresh == _legacy_text(payload)
    # 片段只覆盖部分键时,其余键仍走整树编码
    partial = canonical_json_text(payload, fragments={"features": fragments["features"]})
    assert partial == fresh


def test_canonical_text_section_key_without_fragment_encodes_fresh() -> None:
    payload = _section_payload(5)
    assert canonical_json_text(payload, fragments={}) == _legacy_text(payload)


def test_canonical_text_ignores_non_sequence_section_value() -> None:
    # features 不是列表(异常形态)时走整树编码,不抛错、不捕获
    payload = {"features": {"a": 1}, "candidates": None}
    capture: dict[str, str] = {}
    assert canonical_json_text(payload, capture=capture) == _legacy_text(payload)
    assert capture == {}


def test_stable_checksum_text_matches_stable_checksum() -> None:
    payload = _section_payload(4)
    assert stable_checksum_text(canonical_json_text(payload)) == stable_checksum(payload)


@pytest.mark.parametrize("chunk_size", [1, 3, CANONICAL_CHUNK_SIZE])
def test_canonical_digest_matches_full_text_checksum(chunk_size: int) -> None:
    adversarial_fragments: dict[str, str] = {}
    assert canonical_json_digest(
        _adversarial_payload(), capture=adversarial_fragments, chunk_size=chunk_size
    ) == stable_checksum(_adversarial_payload())
    assert adversarial_fragments == {}  # 无截面键:不捕获片段

    payload = _section_payload(7)
    fragments: dict[str, str] = {}
    assert canonical_json_digest(
        payload, capture=fragments, chunk_size=chunk_size
    ) == stable_checksum(payload)
    assert sorted(fragments) == ["candidates", "features"]
    # 分片拼接即全文(与 canonical_json_text 同源)
    assert "".join(canonical_json_parts(payload)) == canonical_json_text(payload)


def test_canonical_list_text_chunk_count_is_bounded() -> None:
    # 分块编码不改变字节,但块数 = ceil(n / chunk_size)(churn 有界的依据)
    items = list(range(10))
    assert canonical_json_list_text(items, chunk_size=4) == _legacy_text(items)
    assert canonical_json_list_text(items, chunk_size=10) == _legacy_text(items)


def test_bundle_fragments_excluded_from_serialization_and_checksum(decision_factory) -> None:
    decision = decision_factory()
    framed = replace(decision, canonical_fragments={"features": "[1,2]"})
    assert to_json_value(framed) == to_json_value(decision)
    assert pipeline_output_checksum(framed) == pipeline_output_checksum(decision)
    assert framed == decision  # compare=False:编码缓存不参与相等性


def test_slim_decision_clears_fragments(decision_factory) -> None:
    decision = decision_factory()
    framed = replace(decision, canonical_fragments={"features": "[1,2]"})
    slim = _slim_decision(framed)
    assert slim.canonical_fragments is None
    assert slim.features == ()
    assert framed.canonical_fragments == {"features": "[1,2]"}  # 原 bundle 不被突变


@pytest.mark.asyncio
async def test_in_memory_store_materializes_payload_json(manifest_factory) -> None:
    store = InMemoryResearchRunStore()
    manifest = manifest_factory()
    await store.create_or_get(manifest)
    payload = {"business_date": "2020-02-28", "features": [{"a": 1.5e-05}]}
    artifact = ResearchArtifact(
        artifact_id="A-472",
        run_id=manifest.run_id,
        decision_id=None,
        sequence=1,
        stage=ResearchRunStage.FEATURES,
        trace_id="T-472",
        parent_trace_ids=(),
        payload={},
        payload_json=canonical_json_text(payload),
        checksum=stable_checksum(payload),
    )
    assert await store.append_artifact(artifact) is True
    stored = (await store.list_artifacts(manifest.run_id))[0]
    assert stored.payload == payload
    assert stored.payload_json is None
    # 幂等重放:同一 artifact(文本再次传入)命中,不产生第二行
    assert await store.append_artifact(artifact) is False
    assert len(await store.list_artifacts(manifest.run_id)) == 1
