"""finboard-opencode:OpenCode 研究运行时集成(issue #109 / #118 / 重构 #121)。

OpenCode 自身管理会话 / 历史 / 恢复;FinBoard 负责:
* Docker 容器级隔离的 ``opencode web`` 进程托管(:mod:`process_manager`);
* OpenCode 运行时 HTTP/SSE 客户端(:mod:`runtime`);
* Web 访问凭证签发(:mod:`access`)。

红线:OpenCode 是受控研究运行时,不连接实盘 broker / 账户 / 订单 / 持仓 / 风控。
"""

from finboard_opencode.access import (
    AccessCredentialIssuer,
    OpenCodeAccessInfo,
)
from finboard_opencode.process_manager import (
    OpenCodeProcessConfig,
    OpenCodeProcessError,
    OpenCodeProcessManager,
    ProcessStatus,
    which_opencode,
)
from finboard_opencode.runtime import (
    OpenCodeRuntimeClient,
    OpenCodeRuntimeError,
    OpenCodeUnavailableError,
)

__all__ = [
    "AccessCredentialIssuer",
    "OpenCodeAccessInfo",
    "OpenCodeProcessConfig",
    "OpenCodeProcessError",
    "OpenCodeProcessManager",
    "OpenCodeRuntimeClient",
    "OpenCodeRuntimeError",
    "OpenCodeUnavailableError",
    "ProcessStatus",
    "which_opencode",
]
