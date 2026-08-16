"""ToolEnvelope / ToolData 单元测试。"""

from __future__ import annotations

import dataclasses

import pytest

from finboard_mcp.envelope import (
    ToolData,
    ToolEnvelope,
    denied,
    error,
    new_operation_id,
    ok,
    pending_approval,
)
from finboard_mcp.execution import _unpack


class TestEnvelope:
    def test_ok_carries_data_and_op_id(self) -> None:
        env = ok({"x": 1})
        assert env.status == "ok"
        assert env.data == {"x": 1}
        assert env.operation_id.startswith("OP-")
        assert env.error is None

    def test_ok_with_provenance_and_idempotency(self) -> None:
        env = ok("d", provenance={"provider": "fake"}, idempotency_key="K-1")
        assert env.provenance == {"provider": "fake"}
        assert env.idempotency_key == "K-1"

    def test_denied_is_not_retryable(self) -> None:
        env = denied("permission_denied", "越权")
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"
        assert env.error.retryable is False

    def test_error_can_be_retryable(self) -> None:
        env = error("timeout", "超时", retryable=True)
        assert env.status == "error"
        assert env.error is not None
        assert env.error.retryable is True

    def test_pending_approval_marks_confirmation_required(self) -> None:
        env = pending_approval("AP-1", message="待审批")
        assert env.status == "pending_approval"
        assert env.data["requires_human_confirmation"] is True
        assert env.data["approval_ref"] == "AP-1"

    def test_operation_id_unique(self) -> None:
        assert new_operation_id() != new_operation_id()

    def test_envelope_is_frozen(self) -> None:
        env = ok()
        with pytest.raises(dataclasses.FrozenInstanceError):
            env.status = "error"  # type: ignore[misc]

    def test_unpack_tooldata_lifts_provenance(self) -> None:
        provenance, data = _unpack(ToolData(data=1, provenance={"k": 1}))
        assert data == 1
        assert provenance == {"k": 1}

    def test_unpack_bare_data(self) -> None:
        provenance, data = _unpack(42)
        assert data == 42
        assert provenance is None

    def test_envelope_is_a_toolenvelope(self) -> None:
        assert isinstance(ok(), ToolEnvelope)
