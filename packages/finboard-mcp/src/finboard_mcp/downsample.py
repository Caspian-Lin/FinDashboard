"""MCP 工具返回体积控制:equity 曲线降采样(issue #172)+ 选股快照裁剪
(issue #258)。

equity:``none`` 模式不返回 equity 曲线(默认,供网格对比表使用,只保留指标
矩阵 + 排名);``summary`` 模式默认返回体积可控的 equity 曲线:首末点必保留、
时序单调、点数不超过上限;``full`` 模式返回与现状完全一致的全量数据。

selection_snapshots:逐决策选股快照(带 selection 的 run 单次历史响应可达
MB 级的主膨胀点)按 ``none``(默认,不返回)/ ``summary``(决策时点 + 状态 +
selected_symbols 计数)/ ``full``(全量)裁剪。

裁剪是纯展示层变换,不修改落库数据(``backtest_runs`` JSON 列仍存全量)。
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
    """按 ``equity_mode`` 应用降采样(full 原样返回;none 返回空)。"""
    if equity_mode == "full":
        return list(points)
    if equity_mode == "none":
        return []
    return downsample_equity(points, max_points)


def resolve_equity_mode(value: str) -> str:
    """校验 ``equity_mode`` 取值;非法值抛 ``ValueError``(调用方映射错误)。

    ``none`` 表示不返回 equity 曲线(默认,体积最小);``summary`` 降采样到
    ``max_points`` 个关键点;``full`` 返回完整曲线。
    """
    if value not in {"none", "summary", "full"}:
        raise ValueError(f"equity_mode 必须是 none/summary/full,收到: {value!r}")
    return value


def clamp_max_points(value: int) -> int:
    """夹逼 ``max_points`` 到 [2, 5000]。"""
    return max(2, min(5000, value))


def resolve_selection_mode(value: str) -> str:
    """校验 ``selection_snapshots`` 取值;非法值抛 ``ValueError``(调用方映射错误)。

    ``none`` 不返回逐决策选股快照(默认,体积最小,只附计数元数据);
    ``summary`` 每期只留决策时点/日期/状态与 selected_symbols 计数;
    ``full`` 返回全量(含 selected_symbols 列表)。
    """
    if value not in {"none", "summary", "full"}:
        raise ValueError(
            f"selection_snapshots 必须是 none/summary/full,收到: {value!r}"
        )
    return value


def apply_selection_mode(
    snapshots: list[dict[str, Any]],
    *,
    selection_mode: str,
) -> list[dict[str, Any]]:
    """按 ``selection_snapshots`` 模式裁剪逐决策选股快照(issue #258)。

    ``summary`` 投影保留 ``checksum``(与 full 同名同值),消费方可用它稳定
    标识/键控单条快照;``selected_symbols`` 列表折叠为计数。
    """
    if selection_mode == "none":
        return []
    if selection_mode == "full":
        return list(snapshots)
    return [
        {
            "decision_at": snap.get("decision_at"),
            "business_date": snap.get("business_date"),
            "effective_date": snap.get("effective_date"),
            "status": snap.get("status"),
            "skip_reason": snap.get("skip_reason"),
            "checksum": snap.get("checksum"),
            "selected_symbol_count": len(snap.get("selected_symbols") or []),
        }
        for snap in snapshots
    ]


__all__ = [
    "apply_equity_mode",
    "apply_selection_mode",
    "clamp_max_points",
    "downsample_equity",
    "resolve_equity_mode",
    "resolve_selection_mode",
]
