"""研究记录(docs/research)只读路由。

外置研究 agent(OpenCode)把研究记录写入仓库 ``docs/research/`` 三件套
(README / ROADMAP / FINDINGS / data-ops 与 ``rounds/`` 轮次日志,issue #268),
本路由把它们只读暴露给前端:列表 + 读取。canonical 仍是仓库文件(经 PR 维护),
前端只读展示、不提供任何写入口。

安全:服务端 containment(解析后必须落在根目录内)+ ``.md`` 后缀白名单,
对齐 ``finboard_data.releases._safe_release_artifact`` 的先例;不泄露根目录
绝对路径,响应只携带相对路径。

红线:纯只读研究域;不触碰交易链路,不连 broker 不下单。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

if TYPE_CHECKING:
    from finboard_app.config import Settings

router = APIRouter(prefix="/api/research/docs", tags=["research-docs"])

_DOC_SUFFIX = ".md"
_ROUNDS_DIR = "rounds"

# 置顶顺序:README(治理)→ ROADMAP(路线)→ FINDINGS(结论)→ data-ops(运营)
_CANONICAL_ORDER = {
    "README.md": 0,
    "ROADMAP.md": 1,
    "FINDINGS.md": 2,
    "data-ops.md": 3,
}


class ResearchDocSummary(BaseModel):
    path: str
    kind: Literal["overview", "roadmap", "findings", "round", "other"]
    title: str
    size_bytes: int
    updated_at: datetime


class ResearchDocListOut(BaseModel):
    docs: list[ResearchDocSummary]


class ResearchDocDetail(ResearchDocSummary):
    content: str


def _docs_root(request: Request) -> Path:
    """完整应用有 settings;最小化路由测试允许缺省(回落仓库相对默认值)。"""
    settings: Settings | None = getattr(request.app.state, "settings", None)
    configured = (
        settings.research_docs_dir if settings is not None else None
    )
    return Path(configured or "docs/research")


def _classify(rel_path: str) -> Literal["overview", "roadmap", "findings", "round", "other"]:
    if rel_path == "README.md":
        return "overview"
    if rel_path == "ROADMAP.md":
        return "roadmap"
    if rel_path == "FINDINGS.md":
        return "findings"
    if rel_path.startswith(f"{_ROUNDS_DIR}/"):
        return "round"
    return "other"


def _extract_title(text: str, fallback: str) -> str:
    """取首个一级标题;文档约定首行即 ``# 标题``,缺失时回退文件名。"""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
        if stripped:
            break
    return fallback


def _summary_from_path(root: Path, path: Path) -> ResearchDocSummary | None:
    rel = path.relative_to(root).as_posix()
    try:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
    except OSError:
        return None
    return ResearchDocSummary(
        path=rel,
        kind=_classify(rel),
        title=_extract_title(text, Path(rel).stem),
        size_bytes=stat.st_size,
        updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
    )


def _scan_docs(root: Path) -> list[ResearchDocSummary]:
    if not root.is_dir():
        return []
    summaries: list[ResearchDocSummary] = []
    for path in sorted(root.glob(f"*{_DOC_SUFFIX}")) + sorted(
        root.glob(f"{_ROUNDS_DIR}/*{_DOC_SUFFIX}")
    ):
        summary = _summary_from_path(root, path)
        if summary is not None:
            summaries.append(summary)
    top = [d for d in summaries if not d.path.startswith(f"{_ROUNDS_DIR}/")]
    rounds = [d for d in summaries if d.path.startswith(f"{_ROUNDS_DIR}/")]
    top.sort(key=lambda d: (_CANONICAL_ORDER.get(d.path, 9), d.path))
    rounds.sort(key=lambda d: d.path, reverse=True)
    return top + rounds


def _safe_doc_path(root: Path, rel_path: str) -> Path:
    """containment + ``.md`` 白名单;越界/缺失/非白名单一律 404,不区分原因。"""
    if not rel_path.endswith(_DOC_SUFFIX):
        raise HTTPException(status_code=404, detail="文档不存在")
    candidate = (root / rel_path).resolve()
    if not candidate.is_relative_to(root.resolve()) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="文档不存在")
    return candidate


@router.get("", response_model=ResearchDocListOut)
async def list_research_docs(request: Request) -> ResearchDocListOut:
    root = _docs_root(request)
    return ResearchDocListOut(docs=await asyncio.to_thread(_scan_docs, root))


@router.get("/{rel_path:path}", response_model=ResearchDocDetail)
async def read_research_doc(rel_path: str, request: Request) -> ResearchDocDetail:
    root = _docs_root(request)
    path = await asyncio.to_thread(_safe_doc_path, root, rel_path)

    def _read() -> ResearchDocDetail:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
        rel = path.relative_to(root.resolve()).as_posix()
        return ResearchDocDetail(
            path=rel,
            kind=_classify(rel),
            title=_extract_title(text, Path(rel).stem),
            size_bytes=stat.st_size,
            updated_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content=text,
        )

    return await asyncio.to_thread(_read)
