"""研究代码静态校验 —— 纵深防御第一层(issue #215)。

在代码进入仓库前做纯静态检查(不 import、不执行):

* manifest 必填字段与入口声明;
* 入口函数存在且签名可调用(factor.compute / strategy.decide,至少一个
  位置参数接收输入数据,不接受 ``*args`` 转发);
* AST import 白名单(pandas / numpy / polars / math / statistics /
  finboard_research_kit);
* 危险调用黑名单(subprocess / socket / os.system / eval / exec /
  ``__import__`` / 文件写模式 ``open``);
* 文件数与单文件大小上限、拒二进制(非 UTF-8 或含 NUL)。

硬边界在后续沙箱容器 issue;本层目标是让明显非法的提交秒级可操作报错。
"""

from __future__ import annotations

import ast
import tomllib
from dataclasses import dataclass

MAX_FILES = 32
MAX_FILE_BYTES = 256 * 1024

# import 白名单:仅数值/统计计算库 + 研究工具包(沙箱内可用集合的镜像)。
IMPORT_WHITELIST: frozenset[str] = frozenset(
    {
        "pandas",
        "numpy",
        "polars",
        "math",
        "statistics",
        "finboard_research_kit",
    }
)

# 危险模块/函数:模块名完全匹配即拒(含 from X import Y)。
FORBIDDEN_MODULES: frozenset[str] = frozenset(
    {"subprocess", "socket", "ctypes", "sys", "os", "shutil", "importlib"}
)
FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {"eval", "exec", "compile", "__import__", "system", "popen"}
)

_ENTRY_FILE = {"factor": "factor.py", "strategy": "strategy.py"}
_ENTRY_FUNC = {"factor": "compute", "strategy": "decide"}
_TEXT_SUFFIXES = (".py", ".toml", ".md", ".txt", ".json")


@dataclass(frozen=True)
class ValidationIssue:
    """单条校验问题(可操作,面向 agent)。"""

    file: str
    code: str
    message: str

    def render(self) -> str:
        return f"[{self.code}] {self.file}: {self.message}"


def validate_submission(
    *,
    kind: str,
    name: str,
    files: dict[str, str],
    max_files: int = MAX_FILES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> list[ValidationIssue]:
    """校验一次提交的全部文件,返回问题清单(空 = 通过)。

    ``files`` 键为相对 ``<kind_dir>/<name>/`` 的路径;上限可通过
    settings(``research_code_max_files`` / ``research_code_max_file_bytes``)覆盖。
    """
    issues: list[ValidationIssue] = []

    if kind not in _ENTRY_FILE:
        issues.append(
            ValidationIssue(
                "(submission)", "invalid_kind",
                f"kind 须为 factor|strategy,收到 {kind!r}",
            )
        )
        return issues

    if not files:
        issues.append(
            ValidationIssue(
                "(submission)", "no_files", "提交不含任何文件"
            )
        )
        return issues

    if len(files) > max_files:
        issues.append(
            ValidationIssue(
                "(submission)", "too_many_files",
                f"文件数 {len(files)} 超过上限 {max_files}",
            )
        )

    normalized: dict[str, bytes] = {}
    for rel, content in files.items():
        if ".." in rel.split("/") or rel.startswith(("/", "~")) or "\\" in rel:
            issues.append(
                ValidationIssue(rel, "illegal_path", f"非法相对路径 {rel!r}")
            )
            continue
        if not rel.endswith(_TEXT_SUFFIXES):
            issues.append(
                ValidationIssue(rel, "binary_rejected",
                                "仅接受文本文件(.py/.toml/.md/.txt/.json)")
            )
            continue
        data = content.encode("utf-8", errors="strict") if isinstance(
            content, str
        ) else content
        if b"\x00" in data:
            issues.append(
                ValidationIssue(rel, "binary_rejected", "内容含 NUL,疑似二进制")
            )
            continue
        if len(data) > max_file_bytes:
            issues.append(
                ValidationIssue(
                    rel, "file_too_large",
                    f"文件 {len(data)} 字节超过上限 {max_file_bytes}",
                )
            )
            continue
        normalized[rel] = data

    manifest_name = "manifest.toml"
    if manifest_name not in normalized:
        issues.append(
            ValidationIssue(manifest_name, "manifest_missing",
                            "缺少 manifest.toml(必填)")
        )
    else:
        issues.extend(_validate_manifest(manifest_name, normalized[manifest_name]))

    entry = _ENTRY_FILE[kind]
    if entry not in normalized:
        issues.append(
            ValidationIssue(entry, "entry_missing",
                            f"缺少入口文件 {entry}")
        )
    else:
        issues.extend(
            _validate_entry(
                entry, normalized[entry], _ENTRY_FUNC[kind]
            )
        )

    for rel, data in normalized.items():
        if rel.endswith(".py"):
            issues.extend(_validate_python(rel, data))

    return issues


def _validate_manifest(rel: str, data: bytes) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        doc = tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        return [ValidationIssue(rel, "manifest_invalid", f"TOML 解析失败: {exc}")]
    top = doc.get("manifest", doc)
    if not isinstance(top, dict):
        return [ValidationIssue(rel, "manifest_invalid", "manifest 根节点须为表")]
    for field in ("entry",):
        if not top.get(field):
            issues.append(
                ValidationIssue(rel, "manifest_missing",
                                f"manifest 缺少必填字段 {field!r}(manifest.entry)")
            )
    # params 声明为 schema 时须是表(浅校验,深校验在后续沙箱 issue)
    params = top.get("params")
    if params is not None and not isinstance(params, dict):
        issues.append(
            ValidationIssue(rel, "manifest_invalid", "manifest.params 须为表")
        )
    return issues


def _validate_entry(
    rel: str, data: bytes, func_name: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        tree = ast.parse(data.decode("utf-8"))
    except SyntaxError as exc:
        return [ValidationIssue(rel, "syntax_error", f"Python 语法错误: {exc}")]
    func = next(
        (
            n
            for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == func_name
        ),
        None,
    )
    if func is None:
        issues.append(
            ValidationIssue(
                rel, "entry_signature",
                f"入口文件未定义模块级函数 {func_name}()",
            )
        )
        return issues
    positional = list(func.args.posonlyargs) + list(func.args.args)
    if not positional and not func.args.kwonlyargs:
        issues.append(
            ValidationIssue(
                rel, "entry_signature",
                f"{func_name}() 至少须有一个输入参数(接收数据/上下文)",
            )
        )
    if func.args.vararg is not None:
        issues.append(
            ValidationIssue(
                rel, "entry_signature",
                f"{func_name}() 不接受 *args(签名须显式,便于沙箱静态装配)",
            )
        )
    return issues


def _validate_python(rel: str, data: bytes) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    try:
        tree = ast.parse(data.decode("utf-8"))
    except SyntaxError as exc:
        return [ValidationIssue(rel, "syntax_error", f"Python 语法错误: {exc}")]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                issues.extend(_check_import(rel, root, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                root = node.module.split(".")[0]
                issues.extend(_check_import(rel, root, node.module))
            else:
                issues.append(
                    ValidationIssue(rel, "forbidden_import",
                                    "禁止相对 import")
                )
        elif isinstance(node, ast.Call):
            issues.extend(_check_call(rel, node))
    return issues


def _check_import(rel: str, root: str, full: str) -> list[ValidationIssue]:
    if root in FORBIDDEN_MODULES:
        return [ValidationIssue(rel, "forbidden_import", f"禁止 import {full}")]
    if root not in IMPORT_WHITELIST:
        return [
            ValidationIssue(
                rel, "import_not_whitelisted",
                f"import {full} 不在白名单 {sorted(IMPORT_WHITELIST)}",
            )
        ]
    return []


def _check_call(rel: str, node: ast.Call) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    name = None
    if isinstance(node.func, ast.Name):
        name = node.func.id
    elif isinstance(node.func, ast.Attribute):
        name = node.func.attr
    if name in FORBIDDEN_CALLS:
        issues.append(
            ValidationIssue(rel, "forbidden_call", f"禁止调用 {name}()")
        )
    if name == "open" and node.args:
        mode = node.args[1] if len(node.args) > 1 else None
        mode_s = None
        if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
            mode_s = mode.value
        elif isinstance(mode, ast.Constant) and mode.value is None:
            mode_s = None
        else:
            mode_s = "?"  # 非字面量模式:按可疑处理
        if mode_s is not None and any(c in mode_s for c in "wax+"):
            issues.append(
                ValidationIssue(rel, "forbidden_call",
                                "open() 禁止写模式(w/a/x/+,代码仓库层只读)")
            )
    return issues


__all__ = [
    "IMPORT_WHITELIST",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "ValidationIssue",
    "validate_submission",
]
