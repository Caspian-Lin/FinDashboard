"""用户自定义因子观测与沙箱快照契约(issue #217)。

覆盖:``u_`` 前缀观测免目录注册、快照 run 锚定(dataset_release_id
可空)、旧发布快照 payload 的 checksum 兼容(回归)。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from finboard_data.factor_lab import (
    USER_FACTOR_PREFIX,
    FeatureObservation,
    FeatureSnapshot,
    build_feature_snapshot,
    is_user_factor_name,
    sandbox_factor_name,
)

_TS = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)


def _obs(feature_name: str, symbol: str = "600000.SH", value: float = 1.5) -> FeatureObservation:
    return FeatureObservation(
        symbol=symbol,
        feature_name=feature_name,
        value=value,
        observed_at=_TS,
        available_at=_TS,
        source="research_code_run",
        source_version="a" * 40,
    )


class TestUserFactorNaming:
    def test_prefix_helpers(self) -> None:
        assert is_user_factor_name("u_mom20")
        assert not is_user_factor_name("pb")
        assert sandbox_factor_name("mom20") == "u_mom20"
        # 已带前缀不叠加
        assert sandbox_factor_name("u_mom20") == "u_mom20"
        assert USER_FACTOR_PREFIX == "u_"

    def test_user_observation_skips_catalog(self) -> None:
        # u_ 前缀不注册进 FACTOR_LAB_CATALOG 也可构造观测
        assert _obs("u_agent_alpha_1").feature_name == "u_agent_alpha_1"

    def test_unknown_builtin_name_still_rejected(self) -> None:
        with pytest.raises(KeyError, match="未知或未实现因子"):
            _obs("not_a_registered_factor")


class TestSandboxSnapshot:
    def test_run_anchored_snapshot_roundtrip(self) -> None:
        snapshot = build_feature_snapshot(
            dataset_release_id=None,
            dataset_release_checksum="c" * 64,
            decision_at=_TS,
            code_version="a" * 40,
            observations=[_obs("u_mom20", "600000.SH", 1.0), _obs("u_mom20", "000001.SZ", 2.0)],
            source_run_id="RCR-abc123",
        )
        assert snapshot.dataset_release_id is None
        assert snapshot.source_run_id == "RCR-abc123"
        assert snapshot.snapshot_id.startswith("feature-")
        # 观测按 (feature, symbol) 排序固化
        assert [o.symbol for o in snapshot.observations] == ["000001.SZ", "600000.SH"]

        restored = FeatureSnapshot.from_dict(snapshot.as_dict())
        assert restored.source_run_id == "RCR-abc123"
        assert restored.dataset_release_id is None
        assert restored.checksum == snapshot.checksum

    def test_both_anchors_missing_rejected(self) -> None:
        with pytest.raises(ValueError, match="dataset_release_id 与 source_run_id"):
            build_feature_snapshot(
                dataset_release_id=None,
                dataset_release_checksum="c" * 64,
                decision_at=_TS,
                code_version="a" * 40,
                observations=[_obs("u_mom20")],
            )

    def test_release_snapshot_payload_unchanged(self) -> None:
        """发布快照路径的 payload 不携带 source_run_id,旧 checksum 不变。"""

        snapshot = build_feature_snapshot(
            dataset_release_id="DR-test",
            dataset_release_checksum="d" * 64,
            decision_at=_TS,
            code_version="v1",
            observations=[_obs("pb")],
        )
        payload = snapshot.as_dict()
        assert "source_run_id" not in payload
        assert payload["dataset_release_id"] == "DR-test"
        restored = FeatureSnapshot.from_dict(payload)
        assert restored.source_run_id is None
        assert restored.checksum == snapshot.checksum
