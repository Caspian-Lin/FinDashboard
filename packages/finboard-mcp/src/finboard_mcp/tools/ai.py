"""``finboard.ai.*`` 工具 —— AI 研究助手(复用 ``ResearchAssistant``)。

这些工具接受自然语言 ``prompt``,底层 ``ResearchAssistant`` 已强制:

* :func:`assert_research_only_request` —— 拒绝越权(下单/撤单/持仓/Kill Switch/
  凭证探测/启动回测)与提示词注入;
* :func:`sanitize_prompt` —— 抹掉 ``sk-*`` / 密码 / token 等凭证。

因此 MCP 层无需重复权限校验:越权请求会抛 :class:`PermissionDeniedError`,
由 :func:`run_tool` 统一映射为 ``denied`` 信封。
"""

from __future__ import annotations

import asyncio

from mcp.server import MCPServer
from mcp.server.mcpserver.context import Context

from finboard_mcp.context import McpAppContext, app_context
from finboard_mcp.envelope import ToolData, ToolEnvelope
from finboard_mcp.execution import run_tool
from finboard_mcp.tools._serde import to_jsonable

_AI_INSTRUCTIONS = (
    "FinBoard AI 研究助手:只回答研究/教育问题,引用项目来源,数据不足时明确声明。"
    "越权请求(下单/撤单/持仓/Kill Switch/凭证/启动回测)会被拒绝。"
)


async def ask(app: McpAppContext, prompt: str) -> ToolEnvelope:
    """同步问答的内存可测核心(不依赖 MCP Context)。"""

    async def _do() -> ToolData:
        response = await asyncio.to_thread(app.research_assistant.ask, prompt)
        return ToolData(
            data=to_jsonable(response.result),
            provenance=to_jsonable(response.provenance),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.ai.ask",
        arguments={"prompt": prompt},
        handler=_do,
        sensitive=("prompt",),
    )


async def propose_hypothesis(app: McpAppContext, prompt: str) -> ToolEnvelope:
    async def _do() -> ToolData:
        response = await asyncio.to_thread(
            app.research_assistant.propose_hypothesis, prompt
        )
        return ToolData(
            data=to_jsonable(response.result),
            provenance=to_jsonable(response.provenance),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.ai.propose_hypothesis",
        arguments={"prompt": prompt},
        handler=_do,
        sensitive=("prompt",),
    )


async def propose_strategy_draft(app: McpAppContext, prompt: str) -> ToolEnvelope:
    async def _do() -> ToolData:
        response = await asyncio.to_thread(
            app.research_assistant.propose_strategy_draft, prompt
        )
        return ToolData(
            data=to_jsonable(response.result),
            provenance=to_jsonable(response.provenance),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.ai.propose_strategy_draft",
        arguments={"prompt": prompt},
        handler=_do,
        sensitive=("prompt",),
    )


async def propose_strategy_diff(app: McpAppContext, prompt: str) -> ToolEnvelope:
    async def _do() -> ToolData:
        response = await asyncio.to_thread(
            app.research_assistant.propose_strategy_diff, prompt
        )
        return ToolData(
            data=to_jsonable(response.result),
            provenance=to_jsonable(response.provenance),
        )

    return await run_tool(
        audit=app.audit,
        tool_name="finboard.ai.propose_strategy_diff",
        arguments={"prompt": prompt},
        handler=_do,
        sensitive=("prompt",),
    )


def register(mcp: MCPServer) -> None:
    """把 AI 工具注册到 MCP server。"""

    @mcp.tool(name="finboard_ai_ask", description=_AI_INSTRUCTIONS)
    async def _ask(prompt: str, ctx: Context) -> ToolEnvelope:
        return await ask(app_context(ctx), prompt)

    @mcp.tool(
        name="finboard_ai_propose_hypothesis",
        description="生成因子假设草案(需人工审批后登记)。",
    )
    async def _hypo(prompt: str, ctx: Context) -> ToolEnvelope:
        return await propose_hypothesis(app_context(ctx), prompt)

    @mcp.tool(
        name="finboard_ai_propose_strategy_draft",
        description="生成无代码策略组件草案(受白名单约束,需人工审批)。",
    )
    async def _draft(prompt: str, ctx: Context) -> ToolEnvelope:
        return await propose_strategy_draft(app_context(ctx), prompt)

    @mcp.tool(
        name="finboard_ai_propose_strategy_diff",
        description="生成策略版本 diff 草案(需人工审批后通过策略规格 API 正式化)。",
    )
    async def _diff(prompt: str, ctx: Context) -> ToolEnvelope:
        return await propose_strategy_diff(app_context(ctx), prompt)


__all__ = [
    "ask",
    "propose_hypothesis",
    "propose_strategy_diff",
    "propose_strategy_draft",
    "register",
]
