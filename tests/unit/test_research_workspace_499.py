from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import ValidationError

from finboard_app.research_explanation import graph_direction
from finboard_app.research_workspace import TopicInput
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools.workspace import call


def test_direction_negate_rank_weight_and_ambiguous():
    nodes: list[dict[str, Any]] = [
        {"node_id": "f", "source": "p_macd", "inputs": []},
        {"node_id": "n", "operator": "negate", "inputs": ["f"]},
        {"node_id": "r", "operator": "cross_section_rank", "inputs": ["n"]},
        {"node_id": "score", "operator": "weighted_sum", "inputs": ["r"], "weights": [0.15]},
    ]
    assert "原值越低" in graph_direction(nodes, "score", "p_macd")
    nodes.append({"node_id": "ratio", "operator": "ratio", "inputs": ["score", "f"]})
    assert "不能" in graph_direction(nodes, "ratio", "p_macd")


def test_actor_spoofing_and_goal_are_rejected():
    with pytest.raises(ValidationError):
        TopicInput.model_validate(
            {
                "title": "x",
                "question": "x",
                "created_by": "user:api",
                "goal": {
                    "version": "v1",
                    "criteria": "x",
                    "source": {"kind": "unknown", "ref_id": ""},
                },
            }
        )


async def test_mcp_readonly_denies_before_opening_session():
    from types import SimpleNamespace

    from finboard_mcp.audit import AuditRecorder

    app = SimpleNamespace(write_tools_enabled=False, audit=AuditRecorder())
    result = await call(cast(McpAppContext, app), "write", {"operation": "create", "payload": {}})
    assert result.status == "denied"
