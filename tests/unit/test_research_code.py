"""研究代码仓库(issue #215)—— 静态校验 + git bare 仓库 + MCP 工具。

纯单元(mock session / tmp git repo,无需 DB);DB 仓储行为由集成测试覆盖。
"""

from __future__ import annotations

from typing import cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finboard_app.config import Settings
from finboard_backtest.feature_snapshot_jobs import FeatureSnapshotJobManager
from finboard_backtest.research_code import (
    ResearchCodeError,
    ResearchCodeService,
    validate_submission,
)
from finboard_mcp.audit import AuditRecorder
from finboard_mcp.context import McpAppContext
from finboard_mcp.tools import research_code
from finboard_persistence.models import ResearchCodeArtifactModel

_FACTOR_FILES = {
    "factor.py": (
        "import numpy as np\n"
        "\n"
        "\n"
        "def compute(data):\n"
        "    return data[-1] / np.mean(data)\n"
    ),
    "manifest.toml": (
        '[manifest]\nentry = "factor.compute"\n\n'
        '[manifest.params]\nwindow = 20\n'
    ),
}

_STRATEGY_FILES = {
    "strategy.py": (
        "import pandas as pd\n"
        "\n"
        "\n"
        "def decide(bars, params):\n"
        "    return {'symbol': '000001.SZ', 'target': 0.5}\n"
    ),
    "manifest.toml": '[manifest]\nentry = "strategy.decide"\n',
}


# --------------------------------------------------------------------------- #
# 静态校验
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_valid_factor_passes(self) -> None:
        issues = validate_submission(
            kind="factor", name="momentum", files=_FACTOR_FILES
        )
        assert issues == []

    def test_valid_strategy_passes(self) -> None:
        issues = validate_submission(
            kind="strategy", name="etf_rot", files=_STRATEGY_FILES
        )
        assert issues == []

    def test_missing_manifest(self) -> None:
        issues = validate_submission(
            kind="factor", name="x", files={"factor.py": _FACTOR_FILES["factor.py"]}
        )
        assert any(i.code == "manifest_missing" for i in issues)

    def test_missing_entry_file(self) -> None:
        issues = validate_submission(
            kind="strategy",
            name="x",
            files={"manifest.toml": '[manifest]\nentry = "strategy.decide"\n'},
        )
        assert any(i.code == "entry_missing" for i in issues)

    def test_entry_signature_missing_function(self) -> None:
        files = {
            "strategy.py": "import math\n\n\ndef other(x):\n    return x\n",
            "manifest.toml": '[manifest]\nentry = "strategy.decide"\n',
        }
        issues = validate_submission(kind="strategy", name="x", files=files)
        assert any(i.code == "entry_signature" for i in issues)

    def test_entry_signature_no_args(self) -> None:
        files = {
            "factor.py": "def compute():\n    return 1\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "entry_signature" for i in issues)

    def test_forbidden_import(self) -> None:
        files = {
            "factor.py": "import subprocess\n\n\ndef compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "forbidden_import" for i in issues)

    def test_import_not_whitelisted(self) -> None:
        files = {
            "factor.py": "import requests\n\n\ndef compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "import_not_whitelisted" for i in issues)

    def test_socket_import_forbidden(self) -> None:
        files = {
            "strategy.py": (
                "import socket\n\n\ndef decide(d, p):\n    return {}\n"
            ),
            "manifest.toml": '[manifest]\nentry = "strategy.decide"\n',
        }
        issues = validate_submission(kind="strategy", name="x", files=files)
        assert any(i.code == "forbidden_import" for i in issues)

    def test_open_write_mode_forbidden(self) -> None:
        files = {
            "factor.py": (
                "def compute(d):\n"
                "    with open('/tmp/x', 'w') as f:\n"
                "        f.write('x')\n"
                "    return d\n"
            ),
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "forbidden_call" for i in issues)

    def test_open_read_mode_allowed(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    open('a', 'r').read()\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        assert (
            validate_submission(kind="factor", name="x", files=files) == []
        )

    def test_eval_forbidden(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return eval('d')\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "forbidden_call" for i in issues)

    def test_binary_rejected(self) -> None:
        issues = validate_submission(
            kind="factor", name="x", files={"model.bin": "\x00\x01binary"}
        )
        assert any(i.code == "binary_rejected" for i in issues)

    def test_nul_rejected(self) -> None:
        issues = validate_submission(
            kind="factor",
            name="x",
            files={"factor.py": "def compute(d):\x00\n    return d\n"},
        )
        assert any(i.code == "binary_rejected" for i in issues)

    def test_file_too_large(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n" + "#" * 300_000,
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "file_too_large" for i in issues)

    def test_too_many_files(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        for i in range(40):
            files[f"note_{i}.md"] = "x"
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "too_many_files" for i in issues)

    def test_illegal_path(self) -> None:
        issues = validate_submission(
            kind="factor", name="x", files={"../escape.py": "x = 1"}
        )
        assert any(i.code == "illegal_path" for i in issues)

    def test_manifest_invalid_toml(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": "not [ valid toml",
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_invalid" for i in issues)

    def test_invalid_kind(self) -> None:
        issues = validate_submission(
            kind="widget", name="x", files=_FACTOR_FILES
        )
        assert any(i.code == "invalid_kind" for i in issues)


class TestManifestEntryContract:
    """manifest.entry 契约提交期校验(issue #236)。

    harness 按 ``module.function`` 加载;格式错此前要到沙箱容器里才
    报 output_contract_violation,浪费一次容器 run。
    """

    def test_entry_with_py_suffix_rejected(self) -> None:
        # "factor.py" 可被解析为 module="factor" func="py",落在函数
        # 不符检查上(harness 里对应 getattr(module, "py") 不可调用)。
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.py"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_entry_mismatch" for i in issues)

    def test_entry_with_colon_rejected(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.py:compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_entry_invalid" for i in issues)

    def test_entry_without_dot_rejected(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_entry_invalid" for i in issues)

    def test_entry_wrong_function_for_kind_rejected(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.decide"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_entry_mismatch" for i in issues)

    def test_entry_module_file_missing_rejected(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "momentum.compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        assert any(i.code == "manifest_entry_file_missing" for i in issues)

    def test_error_message_shows_correct_contract(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.py:compute"\n',
        }
        issues = validate_submission(kind="factor", name="x", files=files)
        issue = next(i for i in issues if i.code == "manifest_entry_invalid")
        assert "factor.compute" in issue.message

    def test_valid_entry_still_passes(self) -> None:
        files = {
            "factor.py": "def compute(d):\n    return d\n",
            "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
        }
        assert validate_submission(kind="factor", name="x", files=files) == []


# --------------------------------------------------------------------------- #
# git bare 仓库(service,tmp_path)
# --------------------------------------------------------------------------- #


class TestResearchCodeService:
    def _service(self, tmp_path) -> ResearchCodeService:
        return ResearchCodeService.from_path(str(tmp_path / "code.git"))

    def test_submit_read_roundtrip(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        r = svc.submit(
            kind="factor", name="momentum", files=_FACTOR_FILES, author="t"
        )
        assert r["kind"] == "factor"
        assert len(r["commit"]) == 40
        assert r["checksum"]
        assert r["path"] == "factors/momentum"
        files = svc.read(kind="factor", name="momentum")
        assert files["factor.py"] == _FACTOR_FILES["factor.py"].replace(
            "\r\n", "\n"
        ) or files["factor.py"] == _FACTOR_FILES["factor.py"]

    def test_resubmit_new_commit_and_history(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        r1 = svc.submit(
            kind="factor", name="momentum", files=_FACTOR_FILES, author="t"
        )
        v2 = dict(_FACTOR_FILES)
        v2["factor.py"] = "import numpy as np\n\n\ndef compute(d):\n    return 2.0\n"
        r2 = svc.submit(kind="factor", name="momentum", files=v2, author="t")
        assert r1["commit"] != r2["commit"]
        assert r1["checksum"] != r2["checksum"]
        history = svc.log(kind="factor", name="momentum")
        assert [h["commit"] for h in history] == [r2["commit"], r1["commit"]]
        # 旧版本仍可读
        old = svc.read(kind="factor", name="momentum", commit=r1["commit"])
        assert "2.0" not in old["factor.py"]
        new = svc.read(kind="factor", name="momentum", commit=r2["commit"])
        assert "2.0" in new["factor.py"]
        # diff 含两版差异
        diff = svc.diff(
            kind="factor",
            name="momentum",
            from_commit=r1["commit"],
            to_commit=r2["commit"],
        )
        assert "2.0" in diff

    def test_full_snapshot_replacement(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        files = dict(_FACTOR_FILES)
        files["extra.py"] = "import math\n\n\ndef helper(x):\n    return math.sqrt(x)\n"
        svc.submit(kind="factor", name="f1", files=files, author="t")
        # 再提交不含 extra.py:同名目录应是完整快照,extra.py 消失
        svc.submit(kind="factor", name="f1", files=_FACTOR_FILES, author="t")
        assert "extra.py" not in svc.read(kind="factor", name="f1")

    def test_submit_invalid_raises_operational_error(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        with pytest.raises(ResearchCodeError, match="禁止 import"):
            svc.submit(
                kind="factor",
                name="bad",
                files={"factor.py": "import os\n\n\ndef compute(d):\n    return d\n"},
                author="t",
            )

    def test_read_missing_raises(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        with pytest.raises(ResearchCodeError):
            svc.read(kind="strategy", name="nope")

    def test_kind_dirs_isolated(self, tmp_path) -> None:
        svc = self._service(tmp_path)
        svc.submit(kind="factor", name="alpha", files=_FACTOR_FILES, author="t")
        svc.submit(
            kind="strategy", name="alpha", files=_STRATEGY_FILES, author="t"
        )
        assert "factor.py" in svc.read(kind="factor", name="alpha")
        assert "strategy.py" in svc.read(kind="strategy", name="alpha")


# --------------------------------------------------------------------------- #
# MCP 工具(mock session)
# --------------------------------------------------------------------------- #


def _artifact_model(**overrides) -> ResearchCodeArtifactModel:
    kwargs: dict[str, object] = {
        "artifact_id": "RC-1",
        "kind": "factor",
        "name": "momentum",
        "commit": "a" * 40,
        "path": "factors/momentum",
        "checksum": "deadbeef",
        "status": "active",
        "created_by": "agent:mcp",
    }
    kwargs.update(overrides)
    return ResearchCodeArtifactModel(**kwargs)


def _mock_sm() -> async_sessionmaker[AsyncSession]:
    session = AsyncMock()
    session.add = MagicMock()
    session.commit = AsyncMock()
    model = _artifact_model()
    result = MagicMock()
    scalars = MagicMock()
    scalars.all.return_value = [model]
    scalars.first.return_value = model
    result.scalars.return_value = scalars
    result.scalar_one_or_none.return_value = model
    session.execute = AsyncMock(return_value=result)
    session.flush = AsyncMock()

    cm = MagicMock()
    cm.return_value.__aenter__ = AsyncMock(return_value=session)
    cm.return_value.__aexit__ = AsyncMock(return_value=None)
    return cast("async_sessionmaker[AsyncSession]", cm)


def _make_app(
    tmp_path, *, write: bool = True
) -> McpAppContext:
    settings = Settings(research_code_repo_path=str(tmp_path / "code.git"))
    return McpAppContext(
        settings=settings,
        session_maker=_mock_sm(),
        audit=AuditRecorder(),
        write_tools_enabled=write,
        engine=MagicMock(),
        feature_snapshot_jobs=FeatureSnapshotJobManager(),
    )


class TestMcpTools:
    async def test_submit_ok(self, tmp_path) -> None:
        app = _make_app(tmp_path)
        env = await research_code.submit(
            app, kind="factor", name="momentum", files=_FACTOR_FILES
        )
        assert env.status == "ok"
        assert env.data["commit"]
        assert env.data["checksum"]
        assert env.data["artifact_id"]

    async def test_submit_invalid_rejected(self, tmp_path) -> None:
        app = _make_app(tmp_path)
        env = await research_code.submit(
            app,
            kind="factor",
            name="bad",
            files={"factor.py": "import requests\n\n\ndef compute(d):\n    return d\n"},
        )
        assert env.status == "error"
        assert env.error is not None
        assert env.error.kind == "invalid_argument"
        assert "import_not_whitelisted" in env.error.message

    async def test_submit_readonly_denied(self, tmp_path) -> None:
        app = _make_app(tmp_path, write=False)
        env = await research_code.submit(
            app, kind="factor", name="momentum", files=_FACTOR_FILES
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"

    async def test_get_roundtrip(self, tmp_path) -> None:
        app = _make_app(tmp_path)
        submitted = await research_code.submit(
            app, kind="strategy", name="etf_rot", files=_STRATEGY_FILES
        )
        assert submitted.status == "ok"
        env = await research_code.get(app, kind="strategy", name="etf_rot")
        assert env.status == "ok"
        assert "decide" in env.data["files"]["strategy.py"]
        assert len(env.data["history"]) == 1

    async def test_get_missing_not_found(self, tmp_path) -> None:
        app = _make_app(tmp_path)
        env = await research_code.get(app, kind="strategy", name="ghost")
        assert env.status in {"error", "not_found"}

    async def test_list_ok(self, tmp_path) -> None:
        app = _make_app(tmp_path)
        env = await research_code.list_artifacts(app, kind="factor")
        assert env.status == "ok"
        assert env.data["count"] == 1

    async def test_rollback_readonly_denied(self, tmp_path) -> None:
        app = _make_app(tmp_path, write=False)
        env = await research_code.rollback(
            app, kind="factor", name="momentum", commit="a" * 40
        )
        assert env.status == "denied"
        assert env.error is not None
        assert env.error.kind == "permission_denied"
