"""OpenCode Agent 配置契约测试(issue #157:权限 allowlist + #122 语义 + 文档同步)。

锁定三件事,防止配置 / 文档再次漂移:

1. **权限 allowlist**:#242 起 agent 唯一权威 = agent md
   (``.opencode/agent/finboard-researcher.md``;``opencode.json`` 不得再声明
   被覆盖的 ``agent`` 死块),其 permission 必须 ``"*": "deny"`` 起底,只显式放行
   ``read``/``glob``/``grep``/``skill``/``finboard_*``(Skill 渐进式加载 + MCP
   工具)以及 ``bash``/``exa_*``(#182 扩展:bash 上限为只读轮询 / 状态检查,
   行为边界写在 agent md、机械兜底靠 ``.opencode``/``.agents`` 挂载 ``:ro``;
   exa 为 remote 搜索 MCP);``edit``/``write``/``webfetch`` 等执行 / 外联能力
   不得被放行。md 也不得钉死 ``model``(#242:默认模型跟随 OpenCode 选择)。
2. **#122 语义**:agent 定义不得再包含「只读 / 写操作需人工审批」过期措辞,
   必须声明研究写操作可自主执行、实盘能力永久拒绝。
3. **#123 文档同步**:MCP 工具注册表总数 == server ``_INSTRUCTIONS`` 宣称数 ==
   Skill ``SKILL.md`` 宣称数;因子实验室分类数精确一致(#157 修正 11→12)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_OPENCODE_JSON = _REPO_ROOT / ".opencode" / "opencode.json"
_AGENT_MD = _REPO_ROOT / ".opencode" / "agent" / "finboard-researcher.md"
_SKILL_MD = (
    _REPO_ROOT
    / ".agents"
    / "skills"
    / "finboard-opencode-research"
    / "SKILL.md"
)
_MCP_TOOLS_DIR = (
    _REPO_ROOT / "packages" / "finboard-mcp" / "src" / "finboard_mcp" / "tools"
)
_MCP_SERVER_PY = (
    _REPO_ROOT / "packages" / "finboard-mcp" / "src" / "finboard_mcp" / "server.py"
)

#: 唯一允许的 permission 显式放行键(其余全被 "*" deny 覆盖)。
#: #182 扩展:bash(仅只读轮询 / 状态检查,约束在 agent md + 挂载 :ro)与
#: exa_*(remote 搜索 MCP)。
_ALLOWED_PERMISSION_KEYS = {
    "*",
    "read",
    "glob",
    "grep",
    "skill",
    "bash",
    "finboard_*",
    "exa_*",
}


def _registered_tool_names() -> set[str]:
    """从 tools/ 源码统计注册的 finboard_* 工具名(与 server 注册一致)。"""
    names: set[str] = set()
    for module in _MCP_TOOLS_DIR.glob("*.py"):
        names.update(re.findall(r'name="(finboard_[a-z0-9_]+)"', module.read_text(encoding="utf-8")))
    return names


def _factor_tool_count() -> int:
    source = (_MCP_TOOLS_DIR / "factors.py").read_text(encoding="utf-8")
    return len(re.findall(r'name="finboard_', source))


def _extract_permission(source: str) -> dict[str, str]:
    """从 opencode.json / agent md 提取 permission 映射(frontmatter 或 JSON)。"""
    match = re.search(r"permission:\n((?:\s+\S+.*\n)+)", source)
    assert match is not None, "配置缺少 permission 块"
    permission: dict[str, str] = {}
    for line in match.group(1).splitlines():
        item = line.strip()
        key, _, value = item.partition(":")
        key = key.strip().strip('"').strip("'")
        value = value.strip().strip('"').strip("'")
        if key:
            permission[key] = value
    return permission


# ---------------------------------------------------------------------------
# 1. 权限 allowlist
# ---------------------------------------------------------------------------


def test_agent_md_permission_is_allowlist() -> None:
    """权限 allowlist 唯一声明处 = agent md(#242 后 opencode.json 无 agent 块)。"""
    permission = _extract_permission(_AGENT_MD.read_text(encoding="utf-8"))
    assert permission.get("*") == "deny", "必须以 * deny 起底(默认拒绝全部)"
    explicit = set(permission) - {"*"}
    assert explicit <= _ALLOWED_PERMISSION_KEYS - {"*"}, (
        f"出现未约定的放行键: {explicit - (_ALLOWED_PERMISSION_KEYS - {'*'})}"
    )
    for required in ("read", "glob", "grep", "skill", "bash", "finboard_*", "exa_*"):
        assert permission.get(required) == "allow", f"{required} 必须显式放行"
    # 执行 / 外联能力不得放行(由 * deny 覆盖;bash 为 #182 例外,见模块 docstring)。
    for forbidden in ("edit", "write", "webfetch", "websearch", "task"):
        assert permission.get(forbidden) != "allow", f"{forbidden} 不得放行"


def test_opencode_json_has_no_dead_agent_block() -> None:
    """opencode.json 不得声明 agent 块:同名 agent 以 .md 为准,json 声明是被覆盖的死配置。"""
    config = json.loads(_OPENCODE_JSON.read_text(encoding="utf-8"))
    assert "agent" not in config, (
        ".opencode/opencode.json 的 agent 块会被 .opencode/agent/*.md 覆盖"
        "(死配置,#242);agent 定义请改 .md"
    )


def test_agent_md_does_not_pin_model() -> None:
    """agent md 不得钉死默认模型(#242):模型跟随 OpenCode Web UI / 会话选择。"""
    source = _AGENT_MD.read_text(encoding="utf-8")
    frontmatter = source.split("---")[1]
    assert not re.search(r"^model:", frontmatter, re.MULTILINE), (
        "finboard-researcher.md 不得钉死 model(#242 后默认模型跟随 OpenCode 选择)"
    )


def test_opencode_json_mcp_url_uses_env_compatible_default() -> None:
    """仓库 opencode.json 的 MCP URL 与默认 opencode_mcp_remote_url 一致。

    容器启动时会用该配置渲染 runtime 覆盖文件;两处默认值漂移会让
    「未配置时的行为」不可预期。
    """
    config = json.loads(_OPENCODE_JSON.read_text(encoding="utf-8"))
    url = config["mcp"]["finboard"]["url"]
    assert url == "http://host.docker.internal:8765/mcp"


# ---------------------------------------------------------------------------
# 2. #122 语义(写操作自主执行,实盘永久拒绝)
# ---------------------------------------------------------------------------


def test_agent_md_declares_autonomous_writes_and_live_ban() -> None:
    source = _AGENT_MD.read_text(encoding="utf-8")
    lowered = source.lower()
    assert "autonomously" in lowered or "自主" in source, (
        "必须声明研究写操作可自主执行(#122)"
    )
    for banned_phrase in ("read-mostly", "human approval", "只读"):
        assert banned_phrase.lower() not in lowered, (
            f"过期措辞「{banned_phrase}」必须移除(#122 后写操作不再走人工审批)"
        )
    # 实盘红线措辞保留。
    assert "place orders" in lowered or "kill switch" in lowered


def test_agent_md_description_mentions_write_autonomy() -> None:
    source = _AGENT_MD.read_text(encoding="utf-8")
    assert "写操作" in source
    assert "自主执行" in source


# ---------------------------------------------------------------------------
# 3. #123 文档同步(工具总数 / 分类数精确一致)
# ---------------------------------------------------------------------------


def test_instructions_tool_total_matches_registry() -> None:
    source = _MCP_SERVER_PY.read_text(encoding="utf-8")
    match = re.search(r"当前可用工具\((\d+) 个", source)
    assert match is not None, "_INSTRUCTIONS 缺少工具总数声明"
    declared = int(match.group(1))
    actual = len(_registered_tool_names())
    assert declared == actual, (
        f"_INSTRUCTIONS 宣称 {declared} 个工具,注册表实际 {actual} 个;"
        "新增/删除工具必须同步 _INSTRUCTIONS(#123)"
    )


def test_skill_md_tool_total_matches_registry() -> None:
    source = _SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"已实现 (\d+) 个工具", source)
    assert match is not None, "SKILL.md 缺少工具总数声明"
    declared = int(match.group(1))
    actual = len(_registered_tool_names())
    assert declared == actual, (
        f"SKILL.md 宣称 {declared} 个工具,注册表实际 {actual} 个;"
        "新增/删除工具必须同步 SKILL.md(#123)"
    )


def test_skill_md_factor_category_count_matches_registry() -> None:
    """#157 修正:Skill 因子实验室行(12 个)必须与 factors.py 注册数精确一致。"""
    source = _SKILL_MD.read_text(encoding="utf-8")
    match = re.search(r"\.feature_snapshot\.。\*\`(\d+) 个\)", source) or re.search(
        r"feature_snapshot.*?\((\d+) 个\)", source
    )
    assert match is not None, "SKILL.md 缺少因子实验室分类计数"
    declared = int(match.group(1))
    actual = _factor_tool_count()
    assert declared == actual, (
        f"SKILL.md 因子实验室宣称 {declared} 个,注册表实际 {actual} 个"
    )
