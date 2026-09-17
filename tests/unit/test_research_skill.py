"""研究 Skill 加载与结构验证(issue #110)。

验证 ``.agents/skills/finboard-opencode-research/`` 的渐进式加载结构:
SKILL.md(frontmatter + 核心规则)+ references/(工具契约 / 工作流 / 记忆规则)。
"""

from __future__ import annotations

from pathlib import Path

_SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "finboard-opencode-research"
)


def test_skill_md_exists() -> None:
    assert (_SKILL_DIR / "SKILL.md").is_file()


def test_skill_md_has_frontmatter_description() -> None:
    content = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    assert content.startswith("---")
    frontmatter = content.split("---", 2)[1]
    assert "description:" in frontmatter


def test_references_exist() -> None:
    refs = _SKILL_DIR / "references"
    assert (refs / "tools.md").is_file()
    assert (refs / "workflow.md").is_file()
    assert (refs / "memory.md").is_file()


def test_skill_covers_key_rules() -> None:
    content = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    # 审批门
    assert "审批" in content
    # 来源引用
    assert "来源" in content
    # 记忆
    assert "记忆" in content
    # Skill 不授工具权限的边界声明
    assert "不授予" in content or "不授予任何" in content


def test_tools_contract_lists_all_tool_families() -> None:
    content = (_SKILL_DIR / "references" / "tools.md").read_text(encoding="utf-8")
    for name in [
        "finboard_run_list",
        "finboard_memory_remember",
        "finboard_memory_correct",
        "finboard_memory_confirm",
    ]:
        assert name in content, f"工具契约缺失: {name}"


def test_memory_reference_covers_lifecycle() -> None:
    content = (_SKILL_DIR / "references" / "memory.md").read_text(encoding="utf-8")
    for keyword in ["active", "forgotten", "archived", "supersedes", "source_refs"]:
        assert keyword in content, f"记忆规则缺失: {keyword}"
