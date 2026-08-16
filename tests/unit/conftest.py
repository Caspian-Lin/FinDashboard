"""单元测试目录级默认超时(issue #167)。

pytest-timeout 全局默认 300s(见 pyproject.toml),单元测试收紧到 60s;
个别长用例用 ``@pytest.mark.timeout(N)`` 调大或 ``timeout(None)`` 豁免。
async 用例由 tests/conftest.py 的 asyncio.timeout 守卫读取同一 marker。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.timeout(60)
