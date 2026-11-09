"""#520 退役内置 OpenCode 托管的残留守卫。

覆盖三件事:

* ``packages/`` 与 ``web/src`` 不再引用已删除的 ``finboard_opencode`` 包 /
  ``/api/opencode`` 网关 / ``opencodeGatewayApi`` 前端客户端;
* API 配置不再有 ``opencode_*`` 字段(旧 ``FINBOARD_OPENCODE_*`` 环境变量
  由启动弃用提示处理,不会被解析);
* 代码不删除命名卷(``opencode-data`` / ``opencode-config``):只允许文档 /
  页面提供只读导出备份命令,``docker volume rm`` 之类的删除指令不得进入代码。
"""

from __future__ import annotations

from pathlib import Path

from finboard_app.config import Settings

_ROOT = Path(__file__).resolve().parents[2]
_PACKAGES = _ROOT / "packages"
_WEB_SRC = _ROOT / "web" / "src"

#: 只属于已退役实现 / 网关 / 前端客户端的标识符(泛称 "OpenCode" 的历史文档
#: 说明不在此列:外部 Harness 章节仍会提到 OpenCode)。
_RETIRED_BACKEND_TOKENS = (
    "finboard_opencode",
    "finboard-opencode",
    "opencode_gateway",
    "opencodeGatewayApi",
    "OpenCodeProcessManager",
    "OpenCodeRuntimeClient",
    "OpenCodeProcessConfig",
    "opencode_mcp_remote_url",
)
_RETIRED_WEB_TOKENS = (
    "opencodeGatewayApi",
    "lib/opencode",
    "opencode-url",
    "buildWorkbenchUrl",
    "ResearchWorkbench",
)


def _iter_files(root: Path, suffixes: tuple[str, ...]) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix in suffixes and "__pycache__" not in path.parts
    )


def test_packages_have_no_retired_opencode_implementation() -> None:
    # 历史文档(ROADMAP / README)允许提到旧目录;这里只锁可执行代码与依赖声明。
    hits: list[str] = []
    for path in _iter_files(_PACKAGES, (".py", ".toml")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in _RETIRED_BACKEND_TOKENS:
            if token in text:
                hits.append(f"{path.relative_to(_ROOT)}: {token}")
    assert not hits, "退役 OpenCode 实现仍有残留:\n" + "\n".join(hits)


def test_web_src_has_no_retired_opencode_client() -> None:
    hits: list[str] = []
    for path in _iter_files(_WEB_SRC, (".ts", ".tsx")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for token in _RETIRED_WEB_TOKENS:
            if token in text:
                hits.append(f"{path.relative_to(_ROOT)}: {token}")
    assert not hits, "退役 OpenCode 前端客户端仍有残留:\n" + "\n".join(hits)


def test_settings_have_no_opencode_fields() -> None:
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    retired = [name for name in dir(settings) if name.startswith("opencode_")]
    assert not retired, f"Settings 仍有退役字段: {retired}"
    assert not hasattr(settings, "opencode_web_enabled")


def test_code_never_deletes_named_volumes() -> None:
    offenders: list[str] = []
    deletion_markers = ("docker volume rm", "volume prune", "volume rm -f")
    for path in _iter_files(_PACKAGES, (".py",)):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        offenders.extend(
            f"{path.relative_to(_ROOT)}: {marker}"
            for marker in deletion_markers
            if marker in text
        )
    for path in _iter_files(_WEB_SRC, (".ts", ".tsx")):
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        offenders.extend(
            f"{path.relative_to(_ROOT)}: {marker}"
            for marker in deletion_markers
            if marker in text
        )
    assert not offenders, "代码不得删除命名卷(升级不删卷):\n" + "\n".join(offenders)
