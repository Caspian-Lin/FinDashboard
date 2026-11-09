"""研究 Skill 结构 / 协议契约(issue #110 → #519 重定向)。

#519 把 OpenCode 专用 Skill 改成通用 `finboard-research`:

* 新目录是 canonical:``SKILL.md`` + ``references/`` + ``presets/``;
* 旧目录 ``finboard-opencode-research`` 只保留**弃用 shim**(迁移映射表,
  #520 删除),不再维护 references;
* 工具名必须是**真实注册名** ``finboard_<family>_<operation>``,且工具契约文档
  不能写死工具总数 —— 清单唯一事实源是 ``TOOL_SCOPES``(与注册表集合相等,
  由 ``test_mcp_server_contract.py`` 锁定)。#516 等后续 issue 新增工具不应让
  本文件失败。
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

from finboard_mcp.scopes import SCOPE_PRESETS, TOOL_SCOPES

_SKILLS_ROOT = Path(__file__).resolve().parents[2] / ".agents" / "skills"
_SKILL_DIR = _SKILLS_ROOT / "finboard-research"
_LEGACY_DIR = _SKILLS_ROOT / "finboard-opencode-research"

_REFERENCE_FILES = (
    "tools.md",
    "workflow.md",
    "memory.md",
    "research-workflow.md",
    "system-overview.md",
)

#: 预设文件 → CLI `--preset` 值。预设名从 ``SCOPE_PRESETS`` 派生,不写字面量。
_PRESET_FILES = {
    "read-only.md": "readonly",
    "research-write.md": "topic-writer",
    "data-operator.md": "data-operator",
}


def _all_skill_text() -> str:
    parts = [_SKILL_DIR.joinpath("SKILL.md").read_text(encoding="utf-8")]
    for name in _REFERENCE_FILES:
        parts.append((_SKILL_DIR / "references" / name).read_text(encoding="utf-8"))
    return "\n".join(parts)


def _section_headings(text: str) -> list[str]:
    return re.findall(r"^### (finboard_[a-z_]+)", text, flags=re.MULTILINE)


class TestCanonicalSkill:
    def test_skill_md_exists_with_frontmatter(self) -> None:
        content = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert content.startswith("---")
        frontmatter = content.split("---", 2)[1]
        assert "description:" in frontmatter
        # 通用名 Skill 不得把客户端权限写进 frontmatter(旧 OpenCode 耦合)。
        assert "permission:" not in frontmatter

    def test_references_exist(self) -> None:
        refs = _SKILL_DIR / "references"
        for name in _REFERENCE_FILES:
            assert (refs / name).is_file(), f"缺少 references/{name}"

    def test_presets_carry_scope_and_warning(self) -> None:
        for filename, preset in _PRESET_FILES.items():
            assert preset in SCOPE_PRESETS, f"未知预设 {preset}(检查 SCOPE_PRESETS)"
            text = (_SKILL_DIR / "presets" / filename).read_text(encoding="utf-8")
            assert f"--preset {preset}" in text, f"{filename} 缺少 --preset {preset}"
            for scope in SCOPE_PRESETS[preset]:
                assert scope in text, f"{filename} 未声明作用域 {scope}"
            # 只读预设的硬告警必须在文件里逐字出现。
            assert "只读预设不得" in text or "只读预设不得用于发起运行" in text

    def test_no_opencode_product_coupling_in_entry_doc(self) -> None:
        skill = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "OpenCode" not in skill, "SKILL.md 不得绑定 OpenCode 产品"
        assert "/workspace" not in skill, "SKILL.md 不得使用容器绝对路径"
        assert ".opencode/agent" not in skill
        # 全目录:不得再指向旧 Skill 的 references(旧副本已删除),也不得留容器路径。
        assert "finboard-opencode-research/references" not in _all_skill_text()
        for path in _SKILL_DIR.rglob("*.md"):
            assert "/workspace" not in path.read_text(encoding="utf-8"), path

    def test_session_protocol_is_mcp_driven(self) -> None:
        skill = (_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        # 启动:课题 context + 证据核验 + 防重测。
        assert "finboard_topic_read" in skill
        assert 'view="context"' in skill
        assert "finboard_source_check" in skill
        assert "已证伪" in skill
        # 收尾:追加轮次 + 记忆 + canonical 区分。
        assert 'operation="append"' in skill
        assert "finboard_memory_remember" in skill
        assert "canonical" in skill
        assert "docs/research/" in skill
        # 交接语义:RT- 深链、续等 BJ-、引用 RR-。
        for token in ("RT-", "BJ-", "RR-"):
            assert token in skill, f"缺少交接语义 {token}"

    def test_tool_reference_uses_registered_names_only(self) -> None:
        tools_md = (_SKILL_DIR / "references" / "tools.md").read_text(encoding="utf-8")
        documented = set(_section_headings(tools_md))
        assert documented, "tools.md 未列出任何工具小节"
        # 文档里的工具名必须是真实注册名(TOOL_SCOPES 键):文档不会跑来一个
        # 只存在于想象中的工具;新增工具也不要求本测试改数字。
        unknown = documented - set(TOOL_SCOPES)
        assert not unknown, f"tools.md 出现未注册工具名: {sorted(unknown)}"
        # 关键入口必须被文档覆盖(逐工具契约,不是家族一句话);允许以表格行
        # 而不是独立小节的形式出现(课题读侧就是一张表)。
        mentioned = documented | set(re.findall(r"`(finboard_[a-z0-9_]+)`", tools_md))
        for required in (
            "finboard_topic_read",
            "finboard_topic_write",
            "finboard_source_check",
            "finboard_run_list",
            "finboard_memory_remember",
        ):
            assert required in mentioned, f"tools.md 缺少工具契约 {required}"

    def test_no_dotted_tool_names_in_skill(self) -> None:
        """工具名统一 ``finboard_*``;审计名 ``finboard.*`` 只允许出现在审计语境。"""
        dotted = re.compile(r"finboard\.[a-z_]+\.[a-z_]+")
        for path in _SKILL_DIR.rglob("*.md"):
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), start=1
            ):
                if "审计事件名" in line or "审计名" in line:
                    continue
                assert not dotted.search(line), (
                    f"{path.relative_to(_SKILL_DIR)}:{lineno} 使用 dotted 工具名: {line.strip()}"
                )

    def test_no_hardcoded_tool_total(self) -> None:
        """文档不写死工具总数(唯一事实源 = TOOL_SCOPES / 运行时注册表)。"""
        pattern = re.compile(r"(?:共|工具总数|已实现)\s*\d{2,3}\s*个工具|\d{3}\s*个工具")
        for path in _SKILL_DIR.rglob("*.md"):
            text = path.read_text(encoding="utf-8")
            match = pattern.search(text)
            assert match is None, (
                f"{path.relative_to(_SKILL_DIR)} 写死了工具总数({match.group(0) if match else ''});"
                " 改为「当前注册工具见 TOOL_SCOPES」"
            )

    def test_memory_reference_covers_lifecycle(self) -> None:
        content = (_SKILL_DIR / "references" / "memory.md").read_text(encoding="utf-8")
        for keyword in ["active", "forgotten", "archived", "supersedes", "source_refs"]:
            assert keyword in content, f"记忆规则缺失: {keyword}"


class TestLegacyShim:
    def test_legacy_dir_is_a_shim_pointing_at_new_name(self) -> None:
        shim = (_LEGACY_DIR / "SKILL.md").read_text(encoding="utf-8")
        assert "finboard-research" in shim
        assert "已更名" in shim
        assert "迁移映射表" in shim
        assert "#520" in shim
        # shim 不是第二份 Skill:不再带启动协议与 references 副本。
        assert "会话启动协议" not in shim
        assert not (_LEGACY_DIR / "references").exists(), (
            "旧 references/ 必须删除,避免两份文档漂移"
        )


#: 工具调用表达式:``finboard_xxx(...)``;参数名 ``key=``。
_CALL = re.compile(r"(finboard_[a-z0-9_]+)\s*\(")
_ARG = re.compile(r"(?:^|[,{(\s])([a-z_][a-z0-9_]*)\s*=")

#: schema 之外、由工具语义要求的参数(``operation`` 维度)。
#: ``finboard_topic_write(operation="append")`` 必须带幂等键,否则重试会重复建轮次
#: (tools.md:「追加需幂等键」;schema 里它是可选的)。
_EXTRA_REQUIRED: dict[tuple[str, str], set[str]] = {
    ("finboard_topic_write", "append"): {"idempotency_key"},
}


def _iter_calls(text: str) -> Iterator[tuple[str, str]]:
    """抽取文本里的工具调用表达式(带括号配平),产出 ``(tool, 实参文本)``。"""
    for match in _CALL.finditer(text):
        tool = match.group(1)
        depth = 0
        index = match.end() - 1
        start = index
        while index < len(text):
            char = text[index]
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        yield tool, text[start : index + 1]


class TestDocumentedCallExamples:
    """Skill / 预设里的示例调用必须带齐真实必填参数(#519 评审阻断项回归)。

    背景:收尾协议曾把 ``entry_type`` / ``source_refs`` 写成 ``finboard_topic_write``
    的顶层参数、并漏掉必填的 ``payload`` 与 ``idempotency_key`` —— 照抄会直接
    报 ``invalid_argument``。这里用运行时注册表的 ``input_schema.required`` 反查,
    新增必填参数时文档不同步就会被这条测试抓住。

    只约束**带具名参数的调用表达式**:``finboard_source_check(…)`` 这类纯占位不算
    示例;函数名本身也必须是已注册工具。
    """

    async def test_examples_carry_required_arguments(self) -> None:
        from finboard_mcp.server import build_mcp_server

        tools = {tool.name: tool for tool in await build_mcp_server().list_tools()}
        files = [
            _SKILL_DIR / "SKILL.md",
            *sorted((_SKILL_DIR / "presets").glob("*.md")),
        ]
        checked = 0
        for path in files:
            text = path.read_text(encoding="utf-8")
            for tool, body in _iter_calls(text):
                assert tool in tools, (
                    f"{path.relative_to(_SKILLS_ROOT)} 引用了未注册工具 {tool}"
                )
                present = set(_ARG.findall(body))
                if not present:
                    continue  # 纯占位调用,不是可抄的示例
                schema = tools[tool].input_schema or {}
                required = set(schema.get("required") or [])
                for (extra_tool, operation), extra in _EXTRA_REQUIRED.items():
                    if extra_tool == tool and f'operation="{operation}"' in body:
                        required |= extra
                missing = sorted(required - present)
                assert not missing, (
                    f"{path.relative_to(_SKILLS_ROOT)} 的 {tool} 示例缺少必填参数 "
                    f"{missing}(真实 required={sorted(required)});"
                    f" 调用片段: {body[:160]}"
                )
                checked += 1
        assert checked >= 5, f"只抽取到 {checked} 个示例调用,正则可能失配"
