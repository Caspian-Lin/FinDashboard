"""MCP 工具返回体积控制:equity 曲线降采样(issue #172)。

``summary`` 模式默认返回体积可控的 equity 曲线:首末点必保留、时序单调、
点数不超过上限;``full`` 模式返回与现状完全一致的全量数据。降采样是纯
展示层变换,不修改落库数据(``backtest_runs`` JSON 列仍存全量)。
"""

from __future__ import annotations

from typing import Any


def downsample_equity(
    points: list[dict[str, Any]],
    max_points: int,
) -> list[dict[str, Any]]:
    """等距降采样 equity 曲线,首末点必保留,输出保持时间顺序。

    ``max_points < 2`` 或点数不超过上限时原样返回;等距抽点后索引去重
    (round 可能产生重复),因此输出点数 ``<= max_points`` 恒成立。
    """
    count = len(points)
    if count <= max_points or max_points < 2:
        return list(points)
    indices: list[int] = [0]
    for index in range(1, max_points - 1):
        indices.append(round(index * (count - 1) / (max_points - 1)))
    indices.append(count - 1)
    return [points[index] for index in dict.fromkeys(indices)]


def apply_equity_mode(
    points: list[dict[str, Any]],
    *,
    equity_mode: str,
    max_points: int,
) -> list[dict[str, Any]]:
    """按 ``equity_mode`` 应用降采样(full 原样返回)。"""
    if equity_mode == "full":
        return list(points)
    return downsample_equity(points, max_points)


def resolve_equity_mode(value: str) -> str:
    """校验 ``equity_mode`` 取值;非法值抛 ``ValueError``(调用方映射错误)。"""
    if value not in {"summary", "full"}:
        raise ValueError(f"equity_mode 必须是 summary 或 full,收到: {value!r}")
    return value


def clamp_max_points(value: int) -> int:
    """夹逼 ``max_points`` 到 [2, 5000]。"""
    return max(2, min(5000, value))


__all__ = [
    "apply_equity_mode",
    "clamp_max_points",
    "downsample_equity",
    "resolve_equity_mode",
]
