"""issue #472:存量 artifact 语料级字节等值与落库直写回归(需 PostgreSQL)。

两组:

* 真实 run 语料:从开发库已完成 run 读回逐决策 13 个 stage artifact
  payload(psycopg 同步只读),经 ``checkpoint_resume.rebuild_decision``
  还原决策对象,用新编码路径复算 canonical 文本,断言
  ① ``sha256(新文本) == 库中存量 checksum``(与「当时真正落库的那份字节」
     逐字节一致,而不是新旧实现互洽);
  ② 新文本 == 旧口径 ``canonical_json_normalized(to_json_value(payload))``
     (整树编码的逐字节对照);
  ③ 片段复用路径(features/candidates 直接拼接)与全新编码逐字节一致。
  语料缺失(本地无该 run / 无开发库)时 skip,不让 CI 误红。
* 落库直写:测试库中经 ``ResearchRunRepository.append_artifact(payload_json=...)``
  写入后回读 —— payload 值不变、库内文本 == canonical 文本(驱动层无二次
  编码),并核对默认 dict 路径逐值等价。
"""

from __future__ import annotations

import hashlib
import os
from collections import defaultdict
from pathlib import Path

import psycopg
import pytest
from sqlalchemy import text

from finboard_app.research_run_store import SqlAlchemyResearchRunStore
from finboard_backtest.research_run.checkpoint_resume import rebuild_decision
from finboard_backtest.research_run.contracts import (
    ResearchArtifact,
    ResearchRunStage,
    canonical_json_normalized,
    canonical_json_text,
    stable_checksum_text,
    to_json_value,
)
from finboard_backtest.research_run.runner import (
    _DECISION_STAGES,
    _decision_stage_payloads,
)
from finboard_persistence import ResearchRunRepository

#: 存量全历史 run(#472 取证样本;可用环境变量覆盖)。
_SAMPLE_RUN = os.environ.get("FINBOARD_472_SAMPLE_RUN", "RR-09844db849d0c6ca5fc123d8")
#: 逐决策取样的期数上限(每期 13 个 stage ≈ 13-14MB 文本)。
_SAMPLE_DECISIONS = int(os.environ.get("FINBOARD_472_SAMPLE_DECISIONS", "3"))


def _dev_db_url() -> str | None:
    url = os.environ.get("FINBOARD_DB_URL")
    if url is None:
        env_path = Path.cwd() / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("FINBOARD_DB_URL="):
                    url = line.split("=", 1)[1].strip()
                    break
    if not url:
        return None
    return url.replace("postgresql+psycopg://", "postgresql://")


def _load_run_decisions(
    url: str, run_id: str
) -> tuple[
    dict[str, dict[ResearchRunStage, dict[str, object]]],
    dict[str, dict[ResearchRunStage, str]],
    dict[str, dict[ResearchRunStage, ResearchArtifact]],
]:
    """只取样本期(首期 / 文本最大期 / 末期)的 13 行 —— 全历史 run 的
    payload 总量数 GB,全量读回既慢又没必要。"""

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select decision_id, sum(octet_length(payload::text)) as size
            from research_run_artifacts
            where run_id = %s and decision_id is not null
            group by decision_id
            """,
            (run_id,),
        )
        sizes = cur.fetchall()
        if not sizes:
            return {}, {}, {}
        ordered = sorted(item[0] for item in sizes)
        largest = max(sizes, key=lambda item: item[1])[0]
        picked = list(dict.fromkeys([ordered[0], largest, ordered[-1]]))[:_SAMPLE_DECISIONS]
        cur.execute(
            """
            select decision_id, stage, payload, checksum, artifact_id, sequence,
                   trace_id, parent_trace_ids
            from research_run_artifacts
            where run_id = %s and decision_id = any(%s)
            order by sequence
            """,
            (run_id, picked),
        )
        rows = cur.fetchall()
    decisions: dict[str, dict[ResearchRunStage, dict[str, object]]] = defaultdict(dict)
    checksums: dict[str, dict[ResearchRunStage, str]] = defaultdict(dict)
    rebuilt: dict[str, dict[ResearchRunStage, ResearchArtifact]] = defaultdict(dict)
    for decision_id, stage, payload, checksum, artifact_id, sequence, trace_id, parents in rows:
        decisions[decision_id][ResearchRunStage(stage)] = payload
        checksums[decision_id][ResearchRunStage(stage)] = checksum
        rebuilt[decision_id][ResearchRunStage(stage)] = ResearchArtifact(
            artifact_id=artifact_id,
            run_id=run_id,
            decision_id=decision_id,
            sequence=sequence,
            stage=ResearchRunStage(stage),
            trace_id=trace_id,
            parent_trace_ids=tuple(parents),
            payload=payload,
            checksum=checksum,
        )
    return dict(decisions), dict(checksums), dict(rebuilt)


def _load_run_report(url: str, run_id: str) -> tuple[dict[str, object], str] | None:
    """run 级 report artifact(decision_id 为 NULL,不进逐决策取样)。"""

    with psycopg.connect(url) as conn, conn.cursor() as cur:
        cur.execute(
            """
            select payload, checksum from research_run_artifacts
            where run_id = %s and stage = 'report' limit 1
            """,
            (run_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    return row[0], row[1]


def test_existing_run_artifacts_reencode_byte_identical() -> None:
    url = _dev_db_url()
    if url is None:
        pytest.skip("FINBOARD_DB_URL 未配置")
    try:
        decisions, checksums, rebuilt = _load_run_decisions(url, _SAMPLE_RUN)
    except psycopg.OperationalError as exc:  # pragma: no cover - 本地无库
        pytest.skip(f"开发库不可达: {exc}")
    sample = [item for item in decisions if len(decisions[item]) == len(_DECISION_STAGES)]
    if not sample:
        pytest.skip(f"开发库无 run {_SAMPLE_RUN} 的 artifact 语料")

    for decision_id in sample:
        index = int(decision_id.rsplit(":", 1)[1])
        by_stage = decisions[decision_id]
        bundle = rebuild_decision(_SAMPLE_RUN, index, rebuilt[decision_id])
        stage_payloads = _decision_stage_payloads(bundle)
        fragments: dict[str, str] = {}
        typed_texts: dict[ResearchRunStage, str] = {}
        for stage in _DECISION_STAGES:
            typed_payload = {
                "business_date": bundle.business_date,
                "decision_at": bundle.decision_at,
                **stage_payloads[stage],
            }
            label = f"{decision_id} {stage.value}"
            # ① 编码器等值:真实领域对象(dataclass 截面)上新旧编码逐字节一致
            legacy_typed = canonical_json_normalized(to_json_value(typed_payload))
            typed_texts[stage] = legacy_typed
            chunked = canonical_json_text(typed_payload, capture=fragments)
            reused = canonical_json_text(typed_payload, fragments=fragments)
            assert chunked == legacy_typed, f"{label} 流式编码与整树编码不一致"
            assert reused == legacy_typed, f"{label} 片段复用与整树编码不一致"
            # ② 语料保真:存量 payload dict 由同一 canonical 口径产出,新编码器
            #    复算的 sha256 == 库中存量 checksum(对齐当时真正落库的字节)
            stored_payload = by_stage[stage]
            stored_chunked = canonical_json_text(stored_payload)
            assert stored_chunked == canonical_json_normalized(
                to_json_value(stored_payload)
            ), f"{label} 存量载荷新旧编码不一致"
            assert stable_checksum_text(stored_chunked) == checksums[decision_id][stage], (
                f"{label} 存量 checksum 不复现"
            )
            # ③ 型别往返无损的 stage:typed 重建载荷的 checksum 与存量对齐
            #    (``risk_exits`` 的 ``0``/``0.0`` 之类既有型别往返差异不属于
            #    编码器范畴 —— checkpoint_resume 载荷逆变换,resume 不重编码
            #    typed 对象,详见 #472 报告)
            if typed_texts[stage] == stored_chunked:
                assert stable_checksum_text(typed_texts[stage]) == checksums[
                    decision_id
                ][stage], f"{label} typed 重建 checksum 漂移"
        # ④ 片段就是 features/candidates 列表本身(且复用拼接 == 整树编码)
        assert set(fragments) == {"features", "candidates"}
        for key in ("features", "candidates"):
            stage = _SECTION_STAGE[key]
            assert fragments[key] == canonical_json_normalized(
                to_json_value(stage_payloads[stage][key])
            )


_SECTION_STAGE = {
    "features": ResearchRunStage.FEATURES,
    "candidates": ResearchRunStage.UNIVERSE,
}


@pytest.mark.asyncio
async def test_repository_payload_json_writes_canonical_text_verbatim(db_session) -> None:
    """落库直写:文本按原样进库(无二次编码),dict 路径逐值等价。"""

    repository = ResearchRunRepository(db_session)
    run_id = "RR-472-probe"
    await repository.create_or_get(
        run_id=run_id,
        idempotency_key="idem-472-probe",
        replay_of_run_id=None,
        strategy_id="ST-472",
        strategy_kind="ma_cross",
        status="running",
        schema_version="research-run/v1",
        manifest_checksum="a" * 64,
        manifest={"run_id": run_id},
        requested_by="pytest",
    )
    canonical_payload: dict[str, object] = {
        "business_date": "2020-02-28",
        "decision_at": "2020-02-28T15:00:00+00:00",
        "features": [
            {"symbol": "000001.SZ", "value": 1.5e-05, "unicode": "中文"},
            {"symbol": "600000.SH", "value": None, "unicode": "€"},
        ],
    }
    canonical_text = canonical_json_text(canonical_payload)
    _, created = await repository.append_artifact(
        run_id=run_id,
        artifact_id=f"{run_id}:A:00000000:features",
        decision_id=f"{run_id}:D:00000000",
        sequence=0,
        stage=ResearchRunStage.FEATURES.value,
        trace_id="RRT-472",
        parent_trace_ids=[],
        payload={},
        checksum=stable_checksum_text(canonical_text),
        payload_json=canonical_text,
    )
    assert created is True
    # 默认 dict 路径(同一列的旧写入形态)保持可用
    fallback_payload: dict[str, object] = {
        "business_date": "2020-03-31",
        "signals": [{"score": 0.25}],
    }
    await repository.append_artifact(
        run_id=run_id,
        artifact_id=f"{run_id}:A:00000000:signals",
        decision_id=f"{run_id}:D:00000000",
        sequence=1,
        stage=ResearchRunStage.SIGNALS.value,
        trace_id="RRT-473",
        parent_trace_ids=[],
        payload=fallback_payload,
        checksum=stable_checksum_text(canonical_json_text(fallback_payload)),
    )
    rows = await repository.list_artifacts(run_id)
    by_id = {row.artifact_id: row for row in rows}
    assert by_id[f"{run_id}:A:00000000:features"].payload == canonical_payload
    assert by_id[f"{run_id}:A:00000000:signals"].payload == fallback_payload
    # 组装层端口(app store)同样把 payload_json 透传到直写路径
    store = SqlAlchemyResearchRunStore(repository)
    artifact = ResearchArtifact(
        artifact_id=f"{run_id}:A:00000001:features",
        run_id=run_id,
        decision_id=f"{run_id}:D:00000001",
        sequence=13,
        stage=ResearchRunStage.FEATURES,
        trace_id="RRT-472-store",
        parent_trace_ids=(),
        payload={},
        payload_json=canonical_text,
        checksum=stable_checksum_text(canonical_text),
    )
    assert await store.append_artifact(artifact) is True
    assert await store.append_artifact(artifact) is False  # 幂等命中
    listed = {
        item.artifact_id: item for item in await store.list_artifacts(run_id)
    }
    assert listed[artifact.artifact_id].payload == canonical_payload
    assert listed[artifact.artifact_id].checksum == artifact.checksum
    stored_text = (
        await db_session.execute(
            text("select payload::text from research_run_artifacts where run_id = :run_id"),
            {"run_id": run_id},
        )
    ).scalars()
    # json 列按原文本存储:canonical 文本逐字节进库(驱动未二次 json.dumps)
    assert canonical_text in set(stored_text)
    assert hashlib.sha256(canonical_text.encode("utf-8")).hexdigest() == by_id[
        f"{run_id}:A:00000000:features"
    ].checksum
