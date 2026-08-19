"""ToolEnvelope / ToolData 单元测试。"""

from __future__ import annotations

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
        from pydantic import ValidationError

        env = ok()
        with pytest.raises(ValidationError):
            env.status = "error"

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

    def test_serialization_omits_none_fields(self) -> None:
        """issue #206:pydantic 序列化(模拟 MCP SDK output_model 路径)省略 None 字段。"""
        import json

        from pydantic import TypeAdapter, create_model

        env = ok({"answer": 42})
        model = create_model("Out", result=ToolEnvelope)
        payload = json.loads(TypeAdapter(model).dump_json(model(result=env)))
        assert payload == {
            "result": {"operation_id": env.operation_id, "status": "ok", "data": {"answer": 42}}
        }

    def test_serialization_keeps_error_and_message(self) -> None:
        import json

        from pydantic import TypeAdapter, create_model

        env = error("not_found", "未找到", idempotency_key="K-1")
        model = create_model("Out", result=ToolEnvelope)
        payload = json.loads(TypeAdapter(model).dump_json(model(result=env)))
        assert payload["result"]["error"] == {
            "kind": "not_found",
            "message": "未找到",
            "retryable": False,
        }
        assert payload["result"]["message"] == "未找到"
        assert payload["result"]["idempotency_key"] == "K-1"
        assert "data" not in payload["result"]
        assert "provenance" not in payload["result"]

    def test_serialization_keeps_provenance(self) -> None:
        import json

        from pydantic import TypeAdapter, create_model

        env = ok(1, provenance={"provider": "fake"})
        model = create_model("Out", result=ToolEnvelope)
        payload = json.loads(TypeAdapter(model).dump_json(model(result=env)))
        assert payload["result"]["provenance"] == {"provider": "fake"}

    def test_sdk_convert_result_path_omits_none(self) -> None:
        """issue #206:MCP SDK 真实序列化路径(structured content)省略 None。

        SDK 对 BaseModel 直接用作 output_model(不重建字段),convert_result 的
        model_dump 走 model_serializer;此前 dataclass 形态会被 SDK 重建为
        同名模型导致自定义钩子失效,故固化该回归测试。
        """
        from mcp.types import InputRequiredResult

        from finboard_mcp.server import build_mcp_server

        mcp = build_mcp_server()
        tool = mcp._tool_manager.get_tool("finboard_job_list")
        assert tool is not None
        result = tool.fn_metadata.convert_result(ok([{"job_id": "BJ-1"}]))
        assert not isinstance(result, InputRequiredResult)
        structured = result.structured_content
        assert isinstance(structured, dict)
        assert structured == {
            "operation_id": structured["operation_id"],
            "status": "ok",
            "data": [{"job_id": "BJ-1"}],
        }

        failed = tool.fn_metadata.convert_result(error("not_found", "未找到"))
        assert not isinstance(failed, InputRequiredResult)
        failed_payload = failed.structured_content
        assert isinstance(failed_payload, dict)
        assert failed_payload["status"] == "error"
        assert failed_payload["error"]["kind"] == "not_found"
        assert "data" not in failed_payload
        assert "provenance" not in failed_payload
