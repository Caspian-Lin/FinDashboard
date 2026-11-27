"""研究记录只读路由(docs/research)的单元测试。

GET /api/research/docs(列表)与 GET /api/research/docs/{path}(读取):
containment + ``.md`` 白名单 + 置顶排序;handler 直调风格对齐
tests/unit/test_api_data_preview.py。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException
from scripts.build_research_doc_index import write_index


def _fake_request(settings: Any = None) -> Any:
    """handlers 只读 request.app.state.settings;SimpleNamespace 足够。"""

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=settings)))


def _write_doc(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.asyncio
async def test_list_canonical_order_and_rounds_desc(tmp_path: Path) -> None:
    from finboard_api.routes import research_docs as module

    root = tmp_path / "docs" / "research"
    _write_doc(root, "FINDINGS.md", "# 结论注册表\n")
    _write_doc(root, "ROADMAP.md", "# 研究路线\n")
    _write_doc(root, "README.md", "# 研究记录治理\n")
    _write_doc(root, "data-ops.md", "# 数据基座运营\n")
    _write_doc(root, "rounds/2026-09-01-feasibility.md", "# 可行性轮次\n")
    _write_doc(root, "rounds/2026-09-04-round3.md", "# 第三轮反馈\n")

    out = await module.list_research_docs(
        _fake_request(SimpleNamespace(research_docs_dir=str(root)))
    )
    assert [d.path for d in out.docs] == [
        "README.md",
        "ROADMAP.md",
        "FINDINGS.md",
        "data-ops.md",
        "rounds/2026-09-04-round3.md",
        "rounds/2026-09-01-feasibility.md",
    ]
    kinds = {d.path: d.kind for d in out.docs}
    assert kinds["README.md"] == "overview"
    assert kinds["ROADMAP.md"] == "roadmap"
    assert kinds["FINDINGS.md"] == "findings"
    assert kinds["data-ops.md"] == "other"
    assert kinds["rounds/2026-09-01-feasibility.md"] == "round"
    assert out.docs[0].title == "研究记录治理"
    assert out.docs[0].size_bytes > 0
    assert out.docs[0].updated_at.tzinfo is not None


@pytest.mark.asyncio
async def test_list_empty_when_root_missing(tmp_path: Path) -> None:
    from finboard_api.routes import research_docs as module

    settings = SimpleNamespace(research_docs_dir=str(tmp_path / "nope"))
    out = await module.list_research_docs(_fake_request(settings))
    assert out.docs == []


@pytest.mark.asyncio
async def test_read_returns_content_with_metadata(tmp_path: Path) -> None:
    from finboard_api.routes import research_docs as module

    root = tmp_path / "research"
    _write_doc(root, "rounds/2026-09-01-x.md", "# 轮次标题\n\n正文\n")
    settings = SimpleNamespace(research_docs_dir=str(root))

    detail = await module.read_research_doc(
        "rounds/2026-09-01-x.md", _fake_request(settings)
    )
    assert detail.title == "轮次标题"
    assert detail.content.startswith("# 轮次标题")
    assert detail.kind == "round"
    assert detail.source_revision is None


@pytest.mark.asyncio
async def test_read_rejects_traversal_non_md_and_missing(tmp_path: Path) -> None:
    from finboard_api.routes import research_docs as module

    root = tmp_path / "research"
    _write_doc(root, "README.md", "# t\n")
    _write_doc(root, "secret.txt", "x")
    _write_doc(tmp_path, "outside.md", "# 根外的秘密\n")
    settings = SimpleNamespace(research_docs_dir=str(root))

    with pytest.raises(HTTPException) as exc:
        await module.read_research_doc("../outside.md", _fake_request(settings))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException) as exc:
        await module.read_research_doc("secret.txt", _fake_request(settings))
    assert exc.value.status_code == 404

    with pytest.raises(HTTPException):
        await module.read_research_doc("missing.md", _fake_request(settings))


@pytest.mark.asyncio
async def test_history_revision_must_match_served_bytes_and_published_ref(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from finboard_api.routes import research_docs as module

    root = tmp_path / "docs" / "research"
    name = "candidate-evidence.md"
    content = "# Candidate evidence\n"
    revision = "a" * 40
    states = {"ref": "refs/remotes/origin/m/web-usability", "content": content}
    _write_doc(root, name, content)
    settings = SimpleNamespace(research_docs_dir=str(root))

    def git(args: list[str], **kwargs: Any) -> SimpleNamespace:
        if args[3] == "remote":
            return SimpleNamespace(stdout="git@github.com:Caspian-Lin/FinDashboard.git")
        if args[3] == "log":
            assert args[-1] == "docs/research/candidate-evidence.md"
            return SimpleNamespace(stdout=revision)
        if args[3] == "for-each-ref":
            return SimpleNamespace(stdout=states["ref"])
        assert args[-1] == f"{revision}:docs/research/candidate-evidence.md"
        return SimpleNamespace(stdout=states["content"])

    monkeypatch.setattr("finboard_api.routes.research_docs.subprocess.run", git)
    detail = await module.read_research_doc(name, _fake_request(settings))
    assert detail.source_revision == revision
    states["content"] = "# Different checkout"
    detail = await module.read_research_doc(name, _fake_request(settings))
    assert detail.source_revision is None
    states.update({"content": content, "ref": ""})
    detail = await module.read_research_doc(name, _fake_request(settings))
    assert detail.source_revision is None


@pytest.mark.asyncio
async def test_packaged_history_is_bound_to_the_served_document(tmp_path: Path) -> None:
    from finboard_api.routes import research_docs as module

    root = tmp_path / "docs" / "research"
    index = root.parent / "source-index.json"
    name = "candidate-evidence.md"
    revision = "a" * 40
    _write_doc(root, name, "# Frozen candidate\n")
    write_index(root, index, revision)
    settings = SimpleNamespace(research_docs_dir=str(root), research_docs_source_index=str(index))

    detail = await module.read_research_doc(name, _fake_request(settings))
    assert detail.source_revision == revision
    _write_doc(root, name, "# Locally edited content\n")
    edited = await module.read_research_doc(name, _fake_request(settings))
    assert edited.source_revision is None
    assert "Locally edited content" in edited.content
