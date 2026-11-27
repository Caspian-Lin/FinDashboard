"""MCP 协议夹具回归(issue #519)。

锁定**协议形状**而不是业务结果:

* 统一响应信封 ``ToolEnvelope`` 的字段 / 枚举 / ``OP-`` ID 形态
  (``tests/fixtures/mcp_protocol/envelope.schema.json``);
* 少量入口工具的**调用参数 JSON Schema 快照**
  (``tests/fixtures/mcp_protocol/tool_input_schemas.json``)。

刻意不锁工具总数:清单与作用域的唯一事实源是
``finboard_mcp.scopes.TOOL_SCOPES``(#516 新增工具不该让这里失败,除非它改了
本节锁定工具的入参)。夹具说明见 ``tests/fixtures/mcp_protocol/README.md``。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, get_args

from finboard_mcp.envelope import (
    ErrorKind,
    ToolEnvelope,
    ToolStatus,
    denied,
    error,
    ok,
    pending_approval,
)
from finboard_mcp.scopes import TOOL_SCOPES

_FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "mcp_protocol"


def _load(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(
        (_FIXTURE_DIR / name).read_text(encoding="utf-8")
    )
    return payload


def _type_matches(instance: Any, expected: str) -> bool:
    match expected:
        case "object":
            return isinstance(instance, dict)
        case "array":
            return isinstance(instance, list)
        case "string":
            return isinstance(instance, str)
        case "boolean":
            return isinstance(instance, bool)
        case "integer":
            return isinstance(instance, int) and not isinstance(instance, bool)
        case "number":
            return isinstance(instance, int | float) and not isinstance(instance, bool)
        case "null":
            return instance is None
        case _:  # pragma: no cover - 夹具写错时给出具名失败
            raise AssertionError(f"夹具里出现未支持的 JSON Schema type: {expected}")


def _validate(instance: Any, schema: dict[str, Any], path: str = "$") -> list[str]:
    """最小 JSON Schema 校验(只覆盖夹具用到的关键字,零新依赖)。"""
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type is not None:
        allowed = expected_type if isinstance(expected_type, list) else [expected_type]
        if not any(_type_matches(instance, item) for item in allowed):
            errors.append(f"{path}: 期望类型 {allowed},实际 {type(instance).__name__}")
            return errors
    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: {instance!r} 不在枚举 {schema['enum']}")
    if (
        "pattern" in schema
        and isinstance(instance, str)
        and re.search(schema["pattern"], instance) is None
    ):
        errors.append(f"{path}: {instance!r} 不匹配 {schema['pattern']}")
    if isinstance(instance, dict):
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{path}: 缺少必填字段 {key}")
        if schema.get("additionalProperties") is False:
            extra = sorted(set(instance) - set(properties))
            if extra:
                errors.append(f"{path}: 出现未声明字段 {extra}")
        for key, value in instance.items():
            if key in properties:
                errors.extend(_validate(value, properties[key], f"{path}.{key}"))
    return errors


class TestEnvelopeFixture:
    def test_fixture_matches_code_enums(self) -> None:
        schema = _load("envelope.schema.json")
        properties = schema["properties"]
        # 枚举从代码派生:改状态机 / 错误分类必须同步夹具(而不是只改文档)。
        assert set(properties["status"]["enum"]) == set(get_args(ToolStatus))
        assert set(properties["error"]["properties"]["kind"]["enum"]) == set(
            get_args(ErrorKind)
        )

    def test_all_envelope_variants_validate(self) -> None:
        schema = _load("envelope.schema.json")
        samples = [
            ok(data={"items": []}, provenance={"source": "pytest"}),
            denied("permission_denied", "客户端缺少所需作用域:research.write"),
            error("payload_too_large", "载荷超限", retryable=False),
            error("timeout", "工具执行超时", retryable=True, idempotency_key="K-1"),
            pending_approval("AP-1", message="需要人工确认"),
        ]
        for envelope in samples:
            payload = envelope.model_dump()
            assert _validate(payload, schema) == [], payload

    def test_serialization_omits_none_fields(self) -> None:
        """#206:序列化省略 null 可选字段,但 operation_id / status 恒在。"""
        payload = ok().model_dump()
        assert set(payload) == {"operation_id", "status"}
        assert payload["status"] == "ok"
        assert re.fullmatch(r"OP-[0-9a-f]{12}", payload["operation_id"])
        denied_payload = denied("permission_denied", "x").model_dump()
        assert denied_payload["error"]["kind"] == "permission_denied"
        assert denied_payload["error"]["retryable"] is False
        assert "data" not in denied_payload

    def test_tool_envelope_fields_are_declared_in_fixture(self) -> None:
        schema = _load("envelope.schema.json")
        declared = set(schema["properties"])
        assert set(ToolEnvelope.model_fields) == declared


class TestToolInputSchemaFixtures:
    async def test_snapshot_tools_are_registered_with_scopes(self) -> None:
        snapshot = _load("tool_input_schemas.json")
        assert snapshot, "工具入参夹具为空"
        # 夹具只覆盖已登记作用域的工具;未登记工具 fail-closed,不该进夹具。
        assert set(snapshot) <= set(TOOL_SCOPES)

    async def test_live_input_schemas_match_fixture(self) -> None:
        from finboard_mcp.server import build_mcp_server

        snapshot = _load("tool_input_schemas.json")
        live = {tool.name: tool for tool in await build_mcp_server().list_tools()}
        missing = sorted(set(snapshot) - set(live))
        assert not missing, f"夹具里的工具已不在注册表中: {missing}"
        for name, expected in snapshot.items():
            actual = live[name].input_schema
            assert actual == expected["input_schema"], (
                f"{name} 的调用参数 schema 变了。若是有意的契约变更,请更新 "
                f"tests/fixtures/mcp_protocol/tool_input_schemas.json 并在 PR 说明;"
                f"否则是回归。实际: {json.dumps(actual, ensure_ascii=False)}"
            )
