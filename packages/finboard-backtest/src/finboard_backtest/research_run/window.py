"""显式研究窗口:预热只提供 PIT 历史,决策和估值均有边界 (#502)。"""

from datetime import date
from typing import Any


def research_window(parameters: dict[str, Any]) -> dict[str, date] | None:
    raw = parameters.get("research_window")
    if raw is None:
        return None
    keys = {"warmup_start", "decision_start", "decision_end", "valuation_end"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError(
            "invalid_research_window: require warmup/decision_start/decision_end/valuation_end"
        )
    result = {k: date.fromisoformat(str(v)) for k, v in raw.items()}
    if not (
        result["warmup_start"]
        <= result["decision_start"]
        <= result["decision_end"]
        <= result["valuation_end"]
    ):
        raise ValueError("invalid_research_window_order")
    return result
