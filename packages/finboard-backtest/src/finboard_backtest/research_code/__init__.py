"""研究代码仓库服务(issue #215)。

组合 :class:`ResearchCodeRepo`(本地 bare git 仓库)与静态校验,提供
submit / log / diff / read / rollback 语义。DB 登记(``research_code_artifacts``)
由调用方(MCP 工具层)在 git commit 成功后执行,本模块保持纯文件域,
便于沙箱执行 issue 复用。

边界:只存储与版本化,**不执行任何 agent 代码**;git 写操作收敛在服务端,
agent 容器文件系统只读。
"""

import hashlib

from finboard_backtest.research_code.git_repo import (
    KIND_FACTOR,
    KIND_STRATEGY,
    VALID_KINDS,
    ResearchCodeError,
    ResearchCodeRepo,
)
from finboard_backtest.research_code.promotion import (
    PROMOTION_FAILED,
    PROMOTION_PASSED,
    PROMOTION_PENDING,
    PromotionGateError,
    PromotionGateResult,
    PromotionScreenThresholds,
    evaluate_promotion_gates,
    is_promoted_artifact,
    promotion_status,
    require_promotion_gates,
)
from finboard_backtest.research_code.user_code import (
    active_user_strategy_commits,
    active_user_strategy_names,
    freeze_user_code_commit,
    user_code_reference_gate_error,
)
from finboard_backtest.research_code.user_factors import (
    active_user_factor_names,
    user_factor_reference_gate_error,
)
from finboard_backtest.research_code.validation import (
    IMPORT_WHITELIST,
    MAX_FILE_BYTES,
    MAX_FILES,
    validate_submission,
)


def compute_checksum(files: dict[str, str]) -> str:
    """对提交文件集合计算稳定 checksum(路径排序后逐文件 sha256 累加)。"""
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8"))
        h.update(b"\x00")
        h.update(hashlib.sha256(files[rel].encode("utf-8")).digest())
    return h.hexdigest()[:16]


class ResearchCodeService:
    """提交(校验→commit)与查询的门面。"""

    def __init__(self, repo: ResearchCodeRepo) -> None:
        self._repo = repo

    @classmethod
    def from_path(cls, repo_path: str) -> "ResearchCodeService":
        return cls(ResearchCodeRepo(repo_path))

    @property
    def repo(self) -> ResearchCodeRepo:
        return self._repo

    def submit(
        self,
        *,
        kind: str,
        name: str,
        files: dict[str, str],
        author: str,
        max_files: int = MAX_FILES,
        max_file_bytes: int = MAX_FILE_BYTES,
    ) -> dict[str, str]:
        """静态校验通过后提交新 commit,返回 {name, kind, commit, checksum, path}。

        校验失败抛 :class:`ResearchCodeError`,消息为逐条问题的渲染文本。
        """
        issues = validate_submission(
            kind=kind,
            name=name,
            files=files,
            max_files=max_files,
            max_file_bytes=max_file_bytes,
        )
        if issues:
            raise ResearchCodeError(
                "静态校验失败("
                + str(len(issues))
                + " 个问题):\n"
                + "\n".join(i.render() for i in issues)
            )
        self._repo.ensure_init()
        checksum = compute_checksum(files)
        commit = self._repo.commit(
            kind=kind,
            name=name,
            files=files,
            message=f"submit {kind} {name} checksum={checksum}",
            author=author,
        )
        return {
            "name": name,
            "kind": kind,
            "commit": commit,
            "checksum": checksum,
            "path": self._repo.dir_path(kind, name),
        }

    def log(self, *, kind: str, name: str, limit: int = 50) -> list[dict[str, str]]:
        return self._repo.log(kind=kind, name=name, limit=limit)

    def diff(self, *, kind: str, name: str, from_commit: str, to_commit: str) -> str:
        return self._repo.diff(kind=kind, name=name, from_commit=from_commit, to_commit=to_commit)

    def read(self, *, kind: str, name: str, commit: str | None = None) -> dict[str, str]:
        return self._repo.read(kind=kind, name=name, commit=commit)

    def exists(self, *, kind: str, name: str, commit: str | None = None) -> bool:
        return self._repo.exists(kind=kind, name=name, commit=commit)


__all__ = [
    "IMPORT_WHITELIST",
    "KIND_FACTOR",
    "KIND_STRATEGY",
    "MAX_FILES",
    "MAX_FILE_BYTES",
    "PROMOTION_FAILED",
    "PROMOTION_PASSED",
    "PROMOTION_PENDING",
    "VALID_KINDS",
    "PromotionGateError",
    "PromotionGateResult",
    "PromotionScreenThresholds",
    "ResearchCodeError",
    "ResearchCodeRepo",
    "ResearchCodeService",
    "active_user_factor_names",
    "active_user_strategy_commits",
    "active_user_strategy_names",
    "compute_checksum",
    "evaluate_promotion_gates",
    "freeze_user_code_commit",
    "is_promoted_artifact",
    "promotion_status",
    "require_promotion_gates",
    "user_code_reference_gate_error",
    "user_factor_reference_gate_error",
    "validate_submission",
]
