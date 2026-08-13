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


def aggregate_run_report(
    row: ResearchRunModel,
    artifacts: list[ResearchRunArtifactModel],
) -> dict[str, Any]:
    """聚合 ResearchRun:run 元信息 + result(ResearchRunReport 扁平字段)+ 全部 artifacts。"""
    return {
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
        "artifact_count": len(artifacts),
        "artifacts": [
            {
                "artifact_id": item.artifact_id,
                "sequence": item.sequence,
                "stage": item.stage,
                "decision_id": item.decision_id,
                "trace_id": item.trace_id,
                "checksum": item.checksum,
                "payload": item.payload,
            }
            for item in artifacts
        ],
    }


def aggregate_backtest_report(row: BacktestRunModel) -> dict[str, Any]:
    """聚合回测历史:运行元信息 + metrics + equity_curve + fills + summary。"""
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
        "equity_curve": list(row.equity_curve) if row.equity_curve else [],
        "fills": list(row.fills) if row.fills else [],
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
    if kind not in REPORT_KINDS:
        raise ValueError(f"未知报告类型: {kind}")
    if fmt not in EXPORT_FORMATS:
        raise ValueError(f"未知导出格式: {fmt}")
    tables = _tables_for_report(kind, report)
    rid = _report_id(report)
    title = f"FinBoard {kind} 报告({rid})"
    content = (
        render_csv(tables)
        if fmt == "csv"
        else render_markdown(title, tables)
    )
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
    "EXPORT_FORMATS",
    "REPORT_KINDS",
    "aggregate_backtest_report",
    "aggregate_run_report",
    "export_dir",
    "export_report",
    "render_csv",
    "render_markdown",
]
