"""AccessIssuer 单元测试(issue #118 / 重构 #121 / #157 移除 basic auth)。

验证:
* 签发器从 ProcessManager 读取 base_url 构造明文访问信息(#157 后无凭证字段);
* issue_default 不依赖 conversation(#121 重构后唯一签发路径);
* OpenCodeAccessInfo 不再有 username / password 字段。
"""

from __future__ import annotations

import dataclasses

import pytest

from finboard_opencode import (
    AccessIssuer,
    OpenCodeAccessInfo,
    OpenCodeProcessConfig,
    OpenCodeProcessManager,
)


@pytest.fixture
def manager() -> OpenCodeProcessManager:
    config = OpenCodeProcessConfig(port=4097, hostname="127.0.0.1")
    return OpenCodeProcessManager(config, manage_process=False)


@pytest.fixture
def issuer(manager) -> AccessIssuer:
    return AccessIssuer(manager, agent_name="finboard-researcher")


def test_issue_default_returns_access_info(issuer) -> None:
    info = issuer.issue_default()
    assert isinstance(info, OpenCodeAccessInfo)
    assert info.web_url == "http://127.0.0.1:4097"
    assert info.agent_name == "finboard-researcher"


def test_access_info_has_no_credential_fields() -> None:
    """#157 移除 basic auth:访问信息是 frozen dataclass,只含 web_url + agent_name。"""
    fields = {f.name for f in dataclasses.fields(OpenCodeAccessInfo)}
    assert fields == {"web_url", "agent_name"}
