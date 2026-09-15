"""报告聚合与导出 service(issue #141)。

把 ResearchRun / 回测历史聚合为结构化报告 dict,并可导出为 CSV / Markdown
文件(纯标准库 csv + 字符串模板,零新依赖,PDF 留后续 issue)。

- 聚合:aggregate_run_report / aggregate_backtest_report —— 纯函数,
  输入 ORM 行(+ artifact 行),输出 JSON 兼容 dict;
- 导出:export_report —— 把聚合报告渲染为 CSV / Markdown,写入导出目录
  (FINBOARD_EXPORT_DIR 环境变量优先,缺省系统临时目录下 finboard_exports),
  返回文件绝对路径。

导出文件不持久化到 DB,由外部管理;agent 经返回的绝对路径读取或转发。
回滚:移除本模块 + finboard_report_* 三个工具即可,不影响任何既有入口。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from finboard_persistence.models import (
        BacktestRunModel,
        ResearchRunArtifactModel,
        ResearchRunModel,
    )
    from finboard_persistence.research_run_repo import ResearchRunArtifactSummary

REPORT_KINDS = ("run", "backtest")
EXPORT_FORMATS = ("csv", "markdown")

_CSV_BOM = "\ufeff"


@dataclass(frozen=True, slots=True)
class _Table:
    """导出用二维表:标题 + 列名 + 行(单元格已字符串化)。"""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def export_dir() -> Path:
    """导出目录:FINBOARD_EXPORT_DIR 优先,缺省 <系统tmp>/finboard_exports。"""
    override = os.environ.get("FINBOARD_EXPORT_DIR")
    if override:
        directory = Path(override).expanduser()
    else:
        directory = Path(tempfile.gettempdir()) / "finboard_exports"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _cell(value: Any) -> str:
    """把单个值字符串化:None → 空串,list/dict → JSON,其余 → str。"""
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _metric_rows(metrics: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    """把扁平指标 dict 展平为 (field, value) 行,嵌套 dict 用点号展开。"""
    rows: list[tuple[str, str]] = []
    for key, value in sorted((metrics or {}).items()):
        if isinstance(value, dict):
            for sub_key, sub_value in sorted(value.items()):
                rows.append((f"{key}.{sub_key}", _cell(sub_value)))
        else:
            rows.append((key, _cell(value)))
    return tuple(rows)


def _kv_table(title: str, pairs: dict[str, Any]) -> _Table:
    return _Table(
        title=title,
        columns=("field", "value"),
        rows=tuple((str(k), _cell(v)) for k, v in pairs.items()),
    )


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


#: detail 视图载荷硬上限(issue #458):估计字节数超过即具名拒绝
#: (``payload_too_large``),不静默截断。估计口径见
#: :func:`estimate_run_detail_bytes`(repr 长度与 JSON 体量同数量级)。
RUN_DETAIL_MAX_ESTIMATED_BYTES = 64 * 1024 * 1024


def estimate_run_detail_bytes(
    artifacts: list[ResearchRunArtifactModel],
) -> int:
    """廉价估计 detail 视图载荷体量(逐 artifact ``len(str(payload))`` 累计,#458)。

    payload 在 DB 读取时已反序列化为 dict,``str()`` 是 C 级实现且逐 artifact
    计完长度即弃 —— 峰值内存为最大单个 artifact 的字符串化结果;合计值与
    JSON 序列化体量同数量级(repr 引号 / 转义差异不影响量级判断)。
    """
    total = 0
    for item in artifacts:
        payload = item.payload
        if payload:
            total += len(str(payload))
    return total


def aggregate_run_report(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel] | None,
    *,
    view: str = "summary",
    decision_id: str | None = None,
    artifact_summary: ResearchRunArtifactSummary | None = None,
) -> dict[str, Any]:
    """聚合 ResearchRun:run 元信息 + result(ResearchRunReport 扁平字段)+ 全部 artifacts。

    issue #206:``view=summary``(默认)不序列化 UNIVERSE/FILLS 等逐标的全量
    payload —— universe 判定聚合为计数,fills 按决策计数;``view=detail``
    保留全量 artifacts(诊断 / 导出用)。纯展示层变换,不修改落库数据。

    issue #458:``decision_id``(仅 detail)把 artifacts 过滤为单个决策的
    13 stage 产物 —— 大 run 的诊断下钻通道;summary 视图传 ``decision_id``
    抛 ``ValueError``(调用方映射为 invalid_argument,不静默忽略)。过滤时
    ``artifact_count`` 保持 run 全量计数,新增 ``decision_id`` 与
    ``filtered_artifact_count`` 两个键;不过滤时输出与旧版本逐键一致。

    issue #478:``view=summary`` 可传 ``artifact_summary``(数据库侧聚合,
    ``ResearchRunRepository.summarize_artifacts`` 的返回)替代全量
    ``artifacts`` —— 输出与传 artifacts 的 Python 聚合逐字段一致;detail
    视图仍必须传全量 artifacts。两者都缺省抛 ``ValueError``。
    """
    if view not in ("summary", "detail"):
        raise ValueError(f"未知视图: {view}")
    if decision_id is not None and view != "detail":
        raise ValueError("decision_id 仅支持 view=detail(summary 视图为聚合计数)")
    if artifacts is None and artifact_summary is not None and (
        view != "summary" or decision_id is not None
    ):
        raise ValueError("仅 view=summary 支持数据库侧聚合(detail 需全量 artifacts)")
    if artifacts is not None:
        artifact_count: int = len(artifacts)
    elif artifact_summary is not None:
        artifact_count = artifact_summary.artifact_count
    else:
        raise ValueError("artifacts 与 artifact_summary 至少提供一个")
    base: dict[str, Any] = {
        "run_id": row.run_id,
        "status": row.status,
        "strategy_id": row.strategy_id,
        "strategy_kind": row.strategy_kind,
        "created_at": _iso(row.created_at),
        "started_at": _iso(row.started_at),
        "completed_at": _iso(row.completed_at),
        "error_code": row.error_code,
        "error_summary": row.error_summary,
        "metrics": dict(row.result) if row.result else {},
        "artifact_count": artifact_count,
        "view": view,
    }
    if view == "summary":
        base["metrics"] = metrics_without_equity_curve(base["metrics"])
        if artifact_summary is not None:
            base.update(artifact_summary.summary_dict())
        else:
            base.update(summarize_run_artifacts(artifacts or []))
        return base
    if artifacts is None:
        raise ValueError("detail 视图需要全量 artifacts(数据库侧聚合仅限 summary)")
    selected = artifacts
    if decision_id is not None:
        selected = [
            item for item in artifacts if item.decision_id == decision_id
        ]
        base["decision_id"] = decision_id
        base["filtered_artifact_count"] = len(selected)
    base["artifacts"] = [
        {
            "artifact_id": item.artifact_id,
            "sequence": item.sequence,
            "stage": item.stage,
            "decision_id": item.decision_id,
            "trace_id": item.trace_id,
            "checksum": item.checksum,
            "payload": item.payload,
        }
        for item in selected
    ]
    return base


#: 默认 fills 分页上限(issue #206 P0:返回体有界,全量走分页翻页)。
DEFAULT_FILLS_LIMIT = 200


def metrics_without_equity_curve(metrics: dict[str, Any]) -> dict[str, Any]:
    """summary 视图剔除 result 内嵌 equity_curve 列表,以点数提示代替。"""
    if not isinstance(metrics, dict) or "equity_curve" not in metrics:
        return metrics
    trimmed = dict(metrics)
    curve = trimmed.pop("equity_curve")
    trimmed["equity_point_count"] = len(curve) if isinstance(curve, list) else 0
    return trimmed


def summarize_run_artifacts(
    artifacts: list[ResearchRunArtifactModel],
) -> dict[str, Any]:
    """把逐决策 artifacts 聚合为计数摘要(issue #206,服务端聚合不传全量)。

    * ``universe``:候选池逐标的判定聚合计数(total / included /
      ``excluded_by_reason``,与 UNIVERSE stage 全量判定一致);
    * ``fills``:按决策计数(``total`` + ``by_decision``)。

    issue #478:大 run 的等价聚合下沉 PostgreSQL
    (``ResearchRunRepository.summarize_artifacts``),不再物化 artifacts;
    本函数保留为小 run 直读路径与两者语义一致性的对照基准。
    """
    total = 0
    included = 0
    excluded_by_reason: dict[str, int] = {}
    fills_by_decision: dict[str, int] = {}
    fill_total = 0
    for item in artifacts:
        stage: Any = getattr(item, "stage", None)
        stage_value = stage.value if hasattr(stage, "value") else stage
        payload: dict[str, Any] = dict(item.payload or {}) if item.payload else {}
        if stage_value == "universe":
            candidates = payload.get("candidates") or []
            for candidate in candidates:
                total += 1
                if candidate.get("included"):
                    included += 1
                    continue
                for reason in candidate.get("reasons") or ["unknown"]:
                    excluded_by_reason[reason] = (
                        excluded_by_reason.get(reason, 0) + 1
                    )
        elif stage_value == "fills":
            count = len(payload.get("fills") or [])
            fill_total += count
            decision_id = item.decision_id or ""
            fills_by_decision[decision_id] = (
                fills_by_decision.get(decision_id, 0) + count
            )
    return {
        "universe": {
            "total": total,
            "included": included,
            "excluded_by_reason": excluded_by_reason,
        },
        "fills": {
            "total": fill_total,
            "by_decision": fills_by_decision,
        },
    }


def aggregate_backtest_report(
    row: BacktestRunModel,
    *,
    equity_mode: str = "summary",
    max_points: int = 200,
    fills_limit: int | None = DEFAULT_FILLS_LIMIT,
    fills_offset: int = 0,
) -> dict[str, Any]:
    """聚合回测历史:运行元信息 + metrics + equity_curve + fills + summary。

    ``equity_mode`` 控制 equity 曲线体积(summary 降采样 / full 全量),
    issue #172;``fills_limit``/``fills_offset`` 控制 fills 分页(默认有界
    200 条,issue #206;``fills_limit=None`` 返回全部);文件导出走全量,
    不受影响。
    """
    from finboard_mcp.downsample import (
        apply_equity_mode,
        clamp_max_points,
        resolve_equity_mode,
    )

    mode = resolve_equity_mode(equity_mode)
    max_equity_points = clamp_max_points(max_points)
    all_equity = list(row.equity_curve) if row.equity_curve else []
    all_fills = list(row.fills) if row.fills else []
    safe_offset = max(0, fills_offset)
    safe_limit = (
        len(all_fills)
        if fills_limit is None
        else max(0, min(len(all_fills), fills_limit))
    )
    return {
        "run_id": row.id,
        "strategy": row.strategy,
        "symbols": list(row.symbols) if row.symbols else [],
        "start": row.start,
        "end": row.end,
        "capital": _cell(row.capital),
        "adjust": row.adjust,
        "created_at": _iso(row.created_at),
        "metrics": dict(row.metrics) if row.metrics else {},
        "equity_curve": apply_equity_mode(
            all_equity, equity_mode=mode, max_points=max_equity_points
        ),
        "equity_point_count": len(all_equity),
        "fills": all_fills[safe_offset : safe_offset + safe_limit],
        "fills_total": len(all_fills),
        "fills_offset": safe_offset,
        "summary": row.summary,
    }


def _run_sections(report: dict[str, Any]) -> list[_Table]:
    tables = [
        _kv_table(
            "metadata",
            {
                "run_id": report.get("run_id"),
                "status": report.get("status"),
                "strategy_id": report.get("strategy_id"),
                "strategy_kind": report.get("strategy_kind"),
                "created_at": report.get("created_at"),
                "completed_at": report.get("completed_at"),
            },
        ),
        _Table(
            title="metrics",
            columns=("field", "value"),
            rows=_metric_rows(report.get("metrics")),
        ),
    ]
    artifacts = report.get("artifacts") or []
    tables.append(
        _Table(
            title="artifacts",
            columns=("artifact_id", "sequence", "stage", "decision_id", "trace_id"),
            rows=tuple(
                (
                    str(item.get("artifact_id", "")),
                    str(item.get("sequence", "")),
                    str(item.get("stage", "")),
                    str(item.get("decision_id") or ""),
                    str(item.get("trace_id", "")),
                )
                for item in artifacts
            ),
        )
    )
    return tables


def _backtest_sections(report: dict[str, Any]) -> list[_Table]:
    tables = [
        _kv_table(
            "metadata",
            {
                "run_id": report.get("run_id"),
                "strategy": report.get("strategy"),
                "symbols": report.get("symbols"),
                "start": report.get("start"),
                "end": report.get("end"),
                "capital": report.get("capital"),
                "adjust": report.get("adjust"),
                "created_at": report.get("created_at"),
                "summary": report.get("summary"),
            },
        ),
        _Table(
            title="metrics",
            columns=("field", "value"),
            rows=_metric_rows(report.get("metrics")),
        ),
    ]
    equity = report.get("equity_curve") or []
    tables.append(
        _Table(
            title="equity_curve",
            columns=("date", "equity", "benchmark"),
            rows=tuple(
                (
                    str(point.get("date", "")),
                    str(point.get("equity", "")),
                    ""
                    if point.get("benchmark") is None
                    else str(point.get("benchmark")),
                )
                for point in equity
            ),
        )
    )
    fills = report.get("fills") or []
    tables.append(
        _Table(
            title="fills",
            columns=("date", "symbol", "side", "quantity", "price", "commission"),
            rows=tuple(
                (
                    str(item.get("date", "")),
                    str(item.get("symbol", "")),
                    str(item.get("side", "")),
                    str(item.get("quantity", "")),
                    str(item.get("price", "")),
                    str(item.get("commission", "")),
                )
                for item in fills
            ),
        )
    )
    return tables


def _tables_for_report(kind: str, report: dict[str, Any]) -> list[_Table]:
    if kind == "run":
        return _run_sections(report)
    if kind == "backtest":
        return _backtest_sections(report)
    raise ValueError(f"未知报告类型: {kind}")


def _md_escape(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", " ")


def render_markdown(title: str, tables: list[_Table]) -> str:
    """渲染 Markdown 报告(## 分节 + 表格)。"""
    lines: list[str] = [f"# {title}", ""]
    for table in tables:
        lines.append(f"## {table.title}")
        lines.append("")
        lines.append("| " + " | ".join(table.columns) + " |")
        lines.append("|" + "|".join("---" for _ in table.columns) + "|")
        for row in table.rows:
            lines.append("| " + " | ".join(_md_escape(cell) for cell in row) + " |")
        lines.append("")
    return "\n".join(lines) + "\n"


def render_csv(tables: list[_Table]) -> str:
    """渲染 CSV 报告:每节以 # 标题 注释行 + 表头 + 行,节间空行。

    加 UTF-8 BOM,保证 Excel 打开中文不乱码。
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    for table in tables:
        writer.writerow([f"# {table.title}"])
        writer.writerow(list(table.columns))
        for row in table.rows:
            writer.writerow(list(row))
        buffer.write("\n")
    return _CSV_BOM + buffer.getvalue()


def _report_id(report: dict[str, Any]) -> str:
    value = report.get("run_id")
    return "" if value is None else str(value)


def render_report(kind: str, report: dict[str, Any], fmt: str) -> str:
    """把聚合报告渲染为字符串(#157:REST 下载端点复用,不写文件)。

    与 :func:`export_report` 共用同一套表格构造 / 渲染逻辑,保证 MCP 导出文件
    与 Web 下载内容一致。kind ∈ run|backtest,fmt ∈ csv|markdown;未知值抛
    ``ValueError``(由调用方映射为 4xx)。
    """
    if kind not in REPORT_KINDS:
        raise ValueError(f"未知报告类型: {kind}")
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"未知导出格式: {fmt}")
    tables = _tables_for_report(kind, report)
    rid = _report_id(report)
    title = f"FinBoard {kind} 报告({rid})"
    return (
        render_csv(tables)
        if fmt == "csv"
        else render_markdown(title, tables)
    )


def export_report(
    kind: str,
    report: dict[str, Any],
    fmt: str,
    *,
    directory: Path | None = None,
) -> dict[str, Any]:
    """把聚合报告导出为文件,返回绝对路径 + 元信息。

    fmt 只支持 csv / markdown;未知 kind / fmt 抛 ValueError(由调用方
    映射为 invalid_argument)。
    """
    content = render_report(kind, report, fmt)
    rid = _report_id(report)
    safe_id = re.sub(r"[^A-Za-z0-9]+", "_", rid) or "unknown"
    stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    filename = f"finboard_{kind}_{safe_id}_{stamp}_{uuid.uuid4().hex[:8]}.{fmt}"
    target = directory or export_dir()
    path = target / filename
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path.resolve()),
        "kind": kind,
        "id": rid,
        "format": fmt,
        "size_bytes": path.stat().st_size,
        "lines": len(content.splitlines()),
    }


__all__ = [
    "DEFAULT_FILLS_LIMIT",
    "EXPORT_FORMATS",
    "REPORT_KINDS",
    "RUN_DETAIL_MAX_ESTIMATED_BYTES",
    "aggregate_backtest_report",
    "aggregate_run_report",
    "estimate_run_detail_bytes",
    "export_dir",
    "export_report",
    "metrics_without_equity_curve",
    "render_csv",
    "render_markdown",
    "render_report",
    "summarize_run_artifacts",
]
