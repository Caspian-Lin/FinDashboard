"""研究代码仓库 —— 本地 bare git repo 读写(issue #215)。

L3 路线第一环:外置研究 agent 通过 MCP 提交策略/因子 Python 代码。git 写操作
收敛在服务端(通过本模块的 subprocess ``git`` 调用完成),agent 容器文件系统保持
只读、不获得仓库写权限。

本模块**只做存储与版本化,不执行任何 agent 代码**(执行见后续沙箱 issue)。
静态校验(``validation``)是纵深防御第一层,硬边界在后续沙箱容器。

红线:不触交易安全红线;纯研究域;LLM 产出仍走 研究→回测→OOS→模拟→影子→
小资金 完整晋级链。
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

KIND_FACTOR = "factor"
KIND_STRATEGY = "strategy"
VALID_KINDS = (KIND_FACTOR, KIND_STRATEGY)

_KIND_DIR = {KIND_FACTOR: "factors", KIND_STRATEGY: "strategies"}


class ResearchCodeError(Exception):
    """研究代码提交/读取失败(可操作报错,消息面向 agent)。"""


class ResearchCodeRepo:
    """本地 bare git 仓库封装(init / commit / log / diff / 读文件)。

    仓库路径由 settings 配置(``research_code_repo_path``),不存在则自动
    ``git init --bare``。目录约定:一因子/策略一目录
    ``factors/<name>/{factor.py, manifest.toml}`` /
    ``strategies/<name>/{strategy.py, manifest.toml}``,深度解耦、独立演进。
    """

    def __init__(self, repo_path: str | Path) -> None:
        self._path = Path(repo_path)

    @property
    def path(self) -> Path:
        return self._path

    # ---- 初始化 -----------------------------------------------------------

    def ensure_init(self) -> None:
        """确保 bare 仓库存在(幂等;父目录自动创建)。"""
        if (self._path / "HEAD").exists():
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._git("init", "--bare", "-b", "main", str(self._path))

    # ---- 提交 -------------------------------------------------------------

    def commit(
        self,
        *,
        kind: str,
        name: str,
        files: dict[str, str],
        message: str,
        author: str,
    ) -> str:
        """把 ``files``(相对 ``<kind_dir>/<name>/`` 的路径→内容)提交为新 commit。

        返回 commit sha。不校验内容(静态校验由上层 ``submit`` 负责)。
        """
        base = self._dir(kind, name)
        with tempfile.TemporaryDirectory(prefix="finboard-code-") as tmp:
            work = Path(tmp) / "w"
            self._git("clone", "--quiet", "--no-hardlinks", str(self._path), str(work))
            target = work / base
            target.mkdir(parents=True, exist_ok=True)
            # 全量替换式提交:同名目录旧文件不在本次 files 中即删除,
            # 保证一个 (kind, name) 目录的内容 = 本次提交的完整快照。
            for child in target.iterdir():
                if child.is_file():
                    child.unlink()
            for rel, content in files.items():
                dest = target / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(content, encoding="utf-8", newline="\n")
            self._git("-C", str(work), "add", "-A", "--", base)
            self._git(
                "-C",
                str(work),
                "-c",
                f"user.name={author}",
                "-c",
                "user.email=agent@finboard.local",
                "commit",
                "--quiet",
                "-m",
                message,
                "--allow-empty",
            )
            sha = self._git("-C", str(work), "rev-parse", "HEAD").strip()
            self._git(
                "-C",
                str(work),
                "push",
                "--quiet",
                "origin",
                "HEAD:refs/heads/main",
            )
            return sha

    # ---- 查询 -------------------------------------------------------------

    def log(self, *, kind: str, name: str, limit: int = 50) -> list[dict[str, str]]:
        """提交历史(新→旧),每条 {commit, date, message, author}。"""
        out = self._git_bare(
            "log",
            f"-{max(1, min(limit, 200))}",
            "--format=%H%x1f%ad%x1f%s%x1f%an",
            "--date=iso",
            "main",
            "--",
            self._dir(kind, name),
        )
        entries: list[dict[str, str]] = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                entries.append(
                    {
                        "commit": parts[0],
                        "date": parts[1],
                        "message": parts[2],
                        "author": parts[3],
                    }
                )
        return entries

    def diff(self, *, kind: str, name: str, from_commit: str, to_commit: str) -> str:
        """两个 commit 间该 (kind, name) 目录的 unified diff。"""
        path = self._dir(kind, name)
        return self._git_bare("diff", f"{from_commit}:{path}", f"{to_commit}:{path}")

    def read(self, *, kind: str, name: str, commit: str | None = None) -> dict[str, str]:
        """读某版本的全部文件(路径相对 ``<kind_dir>/<name>/``)。

        目录不存在时抛 :class:`ResearchCodeError`。
        """
        base = self._dir(kind, name)
        tree_ref = f"{commit}:{base}" if commit else f"main:{base}"
        listing = self._try_ls_tree(tree_ref)
        if listing is None:
            raise ResearchCodeError(
                f"代码目录不存在或 commit 无效: {tree_ref}(kind={kind}, "
                f"name={name}, commit={commit or 'main'})"
            )
        files: dict[str, str] = {}
        for line in listing.splitlines():
            meta, rel = line.split("\t", 1)
            if not meta.startswith("040000") and rel.endswith(
                (".py", ".toml", ".md", ".txt", ".json")
            ):
                files[rel] = self._git_bare("show", f"{tree_ref}/{rel}")
        return files

    def exists(self, *, kind: str, name: str, commit: str | None = None) -> bool:
        base = self._dir(kind, name)
        tree_ref = f"{commit}:{base}" if commit else f"main:{base}"
        return self._try_ls_tree(tree_ref) is not None

    # ---- 内部 -------------------------------------------------------------

    def dir_path(self, kind: str, name: str) -> str:
        """(kind, name) 对应的仓库内目录路径(校验合法性)。"""
        return self._dir(kind, name)

    def _dir(self, kind: str, name: str) -> str:
        if kind not in VALID_KINDS:
            raise ResearchCodeError(f"非法 kind {kind!r},允许: {list(VALID_KINDS)}")
        if not name or any(c in name for c in "/\\.. \t"):
            raise ResearchCodeError(f"非法 name {name!r}:须为非空且不含路径分隔符/点号/空白")
        return f"{_KIND_DIR[kind]}/{name}"

    def _try_ls_tree(self, tree_ref: str) -> str | None:
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "--git-dir",
                str(self._path),
                "ls-tree",
                tree_ref,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout

    def _git(self, *args: str) -> str:
        """bare 仓库作用域的 git 调用(--git-dir 固定指向本仓库)。"""
        proc = subprocess.run(
            [
                "git",
                "-c",
                "core.quotepath=false",
                "-c",
                "core.autocrlf=false",
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        if proc.returncode != 0:
            raise ResearchCodeError(f"git {' '.join(args[:3])} 失败: {proc.stderr.strip()[:500]}")
        return proc.stdout

    def _git_bare(self, *args: str) -> str:
        """同 :meth:`_git`,但以 ``--git-dir`` 固定作用到本 bare 仓库。

        读类命令(ls-tree/log/diff/show)必须显式 ``--git-dir``:否则会在
        调用方进程的 CWD(很可能是 FinBoard 自身的 git 仓库)执行,读到
        错误的仓库。
        """
        return self._git("--git-dir", str(self._path), *args)


@dataclass(frozen=True)
class ResearchCodeSubmission:
    """一次提交的结果回执。"""

    name: str
    kind: str
    commit: str
    checksum: str
    path: str


__all__ = [
    "KIND_FACTOR",
    "KIND_STRATEGY",
    "VALID_KINDS",
    "ResearchCodeError",
    "ResearchCodeRepo",
    "ResearchCodeSubmission",
]
