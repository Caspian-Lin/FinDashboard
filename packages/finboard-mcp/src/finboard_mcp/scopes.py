"""MCP 工具作用域模型与强制策略(issue #517)。

作用域只有五个,刻意保持粗粒度(按能力而非按资源):

* ``research.read`` —— 研究域只读(数据 / 因子 / 运行 / 策略 / 模拟盘查询等);
* ``research.write`` —— 研究域结构化写入(课题 / 记忆 / 策略规格 / 因子与验证
  实验元数据 / 自选股等),不启动运行;
* ``research.run`` —— 启动 / 取消 / 重放研究运行、回测、模拟盘会话与沙箱执行;
* ``data.write`` —— 全局数据操作(拉取 / 批量下载 / 发布 / 修复 / ETF / 补数);
* ``audit.read`` —— 审计读取(``finboard_audit_query``,#518),只发给运维 /
  审计客户端。

**默认拒绝**:``TOOL_SCOPES`` 必须覆盖全部已注册工具;``run_tool`` 查不到
条目时直接 ``denied`` 并记录契约错误,不允许「未声明 = 放行」。``"*"`` 只由
``FINBOARD_MCP_AUTH_TOKEN`` 旧共享 token 合成,CLI 签发的客户端不能获得。

``FINBOARD_MCP_READONLY_ONLY=true`` 保留一个版本:等价于把所有写工具降级为
只允许 ``research.read``(启动时打印 deprecation warning)。

``FINBOARD_MCP_STDIO_SCOPES``(默认 ``research.read``)是本机 stdio 子进程的
默认作用域。此前 stdio 等价于全权;收紧后需要写操作必须显式配置——这是
**行为变更**,PR 中显式声明。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import Literal, cast, get_args

from finboard_mcp.identity import McpClientIdentity

logger = logging.getLogger("finboard.mcp.scopes")

Scope = Literal[
    "research.read",
    "research.write",
    "research.run",
    "data.write",
    "audit.read",
]

#: 全部合法作用域(供 CLI / 配置解析校验)。
VALID_SCOPES: frozenset[Scope] = frozenset(cast(tuple[Scope, ...], get_args(Scope)))

#: 预设:readonly = 只读;topic-writer = 课题内研究写 + 启动运行;
#: data-operator = 再加全局数据操作。``audit.read`` 不进常规研究预设。
SCOPE_PRESETS: dict[str, frozenset[Scope]] = {
    "readonly": frozenset({"research.read"}),
    "topic-writer": frozenset({"research.read", "research.write", "research.run"}),
    "data-operator": frozenset(
        {"research.read", "research.write", "research.run", "data.write"}
    ),
}

_READ: frozenset[Scope] = frozenset({"research.read"})
_WRITE: frozenset[Scope] = frozenset({"research.write"})
_RUN: frozenset[Scope] = frozenset({"research.run"})
_DATA_WRITE: frozenset[Scope] = frozenset({"data.write"})
_READ_RUN: frozenset[Scope] = frozenset({"research.read", "research.run"})
#: 通用后台任务队列同时服务研究 / 数据两类 kind;入队/取消需要两域权限。
_RUN_DATA_WRITE: frozenset[Scope] = frozenset({"research.run", "data.write"})
#: #518 研究操作审计读取:只发给运维 / 审计客户端,不进常规研究预设。
_AUDIT_READ: frozenset[Scope] = frozenset({"audit.read"})

#: 工具名 → 所需作用域。键是 ``@mcp.tool(name=...)`` 的注册名,与注册表**集合相等**
#: (不锁数量;#516 等新增工具只改本表,不改文档里的数字)。
#: 混合工具(如 ``finboard_research_stress`` 的 plan/get 与 queue)按最高权限
#: 取并集,只读客户端不能调用其只读 operation —— 工具级强制点不接受参数级
#: 绕过。新增工具必须同时登记本表,否则 ``run_tool`` fail-closed。
TOOL_SCOPES: dict[str, frozenset[Scope]] = {
    # ---- backtest ----
    "finboard_backtest_strategy_list": _READ,
    "finboard_backtest_run": _RUN,
    "finboard_backtest_history_list": _READ,
    "finboard_backtest_history_get": _READ,
    "finboard_backtest_history_delete": _WRITE,
    # ---- data 查询 ----
    "finboard_instrument_list": _READ,
    "finboard_instrument_get": _READ,
    "finboard_instrument_search": _READ,
    "finboard_dataset_release_list": _READ,
    "finboard_dataset_release_get": _READ,
    "finboard_dataset_release_diff": _READ,
    "finboard_dataset_manifest_list": _READ,
    "finboard_data_cache_status": _READ,
    "finboard_data_quality_check": _READ,
    "finboard_tushare_quota": _READ,
    # ---- 补数请求(#511) ----
    "finboard_data_backfill_preview": _READ,
    "finboard_data_backfill_submit": _DATA_WRITE,
    "finboard_data_backfill_list": _READ,
    "finboard_data_backfill_cancel": _DATA_WRITE,
    # ---- 全局数据目录(#509) ----
    "finboard_data_catalog_list": _READ,
    "finboard_data_catalog_get": _READ,
    "finboard_data_coverage_get": _READ,
    "finboard_dataset_release_instruments": _READ,
    "finboard_dataset_release_references": _READ,
    # ---- 数据范围预览(#510) ----
    "finboard_data_scope_preview": _READ,
    # ---- 数据写操作(#137) ----
    "finboard_data_fetch": _DATA_WRITE,
    "finboard_data_sync_universe": _DATA_WRITE,
    "finboard_data_bulk_download_start": _DATA_WRITE,
    "finboard_data_quality_repair": _DATA_WRITE,
    "finboard_dataset_release_publish": _DATA_WRITE,
    "finboard_data_config_get": _READ,
    "finboard_data_config_update": _DATA_WRITE,
    "finboard_etf_sync": _DATA_WRITE,
    "finboard_etf_batch_confirm": _DATA_WRITE,
    "finboard_etf_update": _DATA_WRITE,
    "finboard_etf_review_queue": _READ,
    # ---- 研究诊断(#501-#504) ----
    # stress 的 queue operation 会启动压力运行;工具级取并集。
    "finboard_research_stress": _READ_RUN,
    "finboard_run_diagnostics": _READ,
    "finboard_run_compare": _READ,
    "finboard_decision_projection": _READ,
    # ---- 研究操作审计(#518) ----
    # 只读查询;只有显式授予 audit.read 的审计/运维客户端可用。
    "finboard_audit_query": _AUDIT_READ,
    # ---- 因子序列(#360) ----
    # build 会入队沙箱因子构建任务(启动运行),不是纯研究元数据写入。
    "finboard_factor_series_build": _RUN,
    "finboard_factor_series_get": _READ,
    # ---- 因子实验室(#125) ----
    "finboard_factor_catalog": _READ,
    "finboard_feature_snapshot_list": _READ,
    "finboard_feature_snapshot_get": _READ,
    "finboard_feature_snapshot_create": _WRITE,
    "finboard_feature_snapshot_job_start": _RUN,
    "finboard_feature_snapshot_job_status": _READ,
    "finboard_factor_signal_list": _READ,
    "finboard_factor_signal_get": _READ,
    "finboard_factor_experiment_list": _READ,
    "finboard_factor_experiment_get": _READ,
    "finboard_factor_experiment_create": _WRITE,
    "finboard_factor_experiment_sync_validation": _WRITE,
    # ---- 批量参数网格(#175) ----
    "finboard_backtest_grid_submit": _RUN,
    "finboard_backtest_grid_get": _READ,
    # ---- 后台任务队列(#136/#221/#443) ----
    "finboard_job_list": _READ,
    "finboard_job_get": _READ,
    "finboard_job_wait": _READ,
    "finboard_job_enqueue": _RUN_DATA_WRITE,
    "finboard_job_cancel": _RUN_DATA_WRITE,
    "finboard_job_archive": _WRITE,
    "finboard_job_unarchive": _WRITE,
    # ---- 研究记忆(#110) ----
    "finboard_memory_remember": _WRITE,
    "finboard_memory_list": _READ,
    "finboard_memory_get": _READ,
    "finboard_memory_forget": _WRITE,
    "finboard_memory_correct": _WRITE,
    "finboard_memory_confirm": _WRITE,
    "finboard_memory_archive": _WRITE,
    # ---- 组合计算(#128,纯计算) ----
    "finboard_portfolio_allocate": _READ,
    "finboard_portfolio_sizing": _READ,
    "finboard_portfolio_feasibility": _READ,
    "finboard_portfolio_attribution": _READ,
    # ---- 报告(#141) ----
    "finboard_report_run": _READ,
    "finboard_report_backtest": _READ,
    # export 会写 CSV / Markdown 文件(落盘副作用),只读客户端不得调用。
    "finboard_report_export": _WRITE,
    # ---- 研究代码仓库(#215/#219;只存储与版本化) ----
    "finboard_research_code_submit": _WRITE,
    "finboard_research_code_list": _READ,
    "finboard_research_code_get": _READ,
    "finboard_research_code_rollback": _WRITE,
    "finboard_research_code_promote": _WRITE,
    # ---- 研究代码沙箱执行(#216) ----
    "finboard_research_code_run": _RUN,
    "finboard_research_code_run_get": _READ,
    # ---- ResearchRun ----
    "finboard_run_list": _READ,
    "finboard_run_get": _READ,
    "finboard_run_artifacts": _READ,
    "finboard_run_queue": _RUN,
    "finboard_run_cancel": _RUN,
    "finboard_run_replay": _RUN,
    "finboard_run_lineage": _READ,
    # ---- 课题发起运行 / 关联(#515) ----
    "finboard_topic_run_start": _RUN,
    "finboard_topic_run_list": _READ,
    "finboard_topic_run_get": _READ,
    "finboard_topic_run_retry": _RUN,
    "finboard_topic_run_cancel": _RUN,
    "finboard_run_topics": _READ,
    # ---- 模拟盘(#127/#139;simulation_* 隔离表) ----
    "finboard_sim_account_list": _READ,
    "finboard_sim_account_get": _READ,
    "finboard_sim_account_create": _WRITE,
    "finboard_sim_session_list": _READ,
    "finboard_sim_session_get": _READ,
    "finboard_sim_session_create": _RUN,
    "finboard_sim_session_start": _RUN,
    "finboard_sim_session_pause": _RUN,
    "finboard_sim_session_stop": _RUN,
    "finboard_sim_session_archive": _RUN,
    "finboard_sim_session_reset": _RUN,
    "finboard_sim_decision_submit": _RUN,
    "finboard_sim_market_event": _RUN,
    "finboard_sim_session_evaluate": _RUN,
    "finboard_sim_order_cancel": _RUN,
    "finboard_sim_orders": _READ,
    "finboard_sim_fills": _READ,
    "finboard_sim_positions": _READ,
    "finboard_sim_ledger": _READ,
    "finboard_sim_audit": _READ,
    "finboard_sim_report": _READ,
    # ---- 策略规格 / 预设(#126) ----
    "finboard_strategy_registry": _READ,
    "finboard_strategy_template": _READ,
    "finboard_strategy_list": _READ,
    "finboard_strategy_history": _READ,
    "finboard_strategy_version_get": _READ,
    "finboard_strategy_diff": _READ,
    "finboard_preset_list": _READ,
    "finboard_preset_get": _READ,
    "finboard_strategy_validate": _READ,
    "finboard_strategy_draft_create": _WRITE,
    "finboard_strategy_supersede": _WRITE,
    "finboard_strategy_publish": _WRITE,
    "finboard_strategy_rollback": _WRITE,
    "finboard_preset_create": _WRITE,
    "finboard_preset_update": _WRITE,
    "finboard_preset_delete": _WRITE,
    # ---- 验证实验(#138/#233) ----
    "finboard_validation_experiment_create": _WRITE,
    "finboard_validation_experiment_list": _READ,
    "finboard_validation_experiment_get": _READ,
    "finboard_validation_experiment_reject": _WRITE,
    "finboard_validation_experiment_add_trial": _WRITE,
    "finboard_validation_experiment_delete": _WRITE,
    "finboard_validation_experiment_run": _RUN,
    # ---- 自选股(#140) ----
    "finboard_watchlist_list": _READ,
    "finboard_watchlist_get": _READ,
    "finboard_watchlist_create": _WRITE,
    "finboard_watchlist_update": _WRITE,
    "finboard_watchlist_delete": _WRITE,
    "finboard_watchlist_add_symbols": _WRITE,
    "finboard_watchlist_remove_symbol": _WRITE,
    # ---- 研究读侧 / 课题归档(#498-#500) ----
    "finboard_source_check": _READ,
    "finboard_topic_read": _READ,
    "finboard_topic_write": _WRITE,
    "finboard_memory_page": _READ,
    "finboard_strategy_explain": _READ,
    "finboard_decision_explain": _READ,
}

#: 历史内部工具名(``run_tool(tool_name=...)`` 的 dotted 形式)→ 注册名。
#: 绝大多数可经 ``.`` → ``_`` 约定直接映射;这里只列不满足约定的特例
#: (动态 operation 名与两处历史命名)。未命中任何映射 = 契约错误 = 拒绝。
_TOOL_NAME_ALIASES: dict[str, str] = {
    "finboard.data_catalog.coverage": "finboard_data_coverage_get",
    "finboard.research_code_run.enqueue": "finboard_research_code_run",
    "finboard.diagnostics.stress": "finboard_research_stress",
    "finboard.diagnostics.interval": "finboard_run_diagnostics",
    "finboard.diagnostics.compare": "finboard_run_compare",
    "finboard.diagnostics.projection": "finboard_decision_projection",
    "finboard.workspace.source": "finboard_source_check",
    "finboard.workspace.read": "finboard_topic_read",
    "finboard.workspace.write": "finboard_topic_write",
    "finboard.workspace.memories": "finboard_memory_page",
    "finboard.workspace.explain": "finboard_strategy_explain",
    "finboard.workspace.decision": "finboard_decision_explain",
    "finboard.sim.session_running": "finboard_sim_session_start",
    "finboard.sim.session_paused": "finboard_sim_session_pause",
    "finboard.sim.session_stopped": "finboard_sim_session_stop",
}


def preset_scopes(name: str) -> frozenset[Scope]:
    """解析预设名;未知预设抛 ``KeyError``(CLI 负责转成用户可读错误)。"""
    try:
        return SCOPE_PRESETS[name]
    except KeyError:
        raise KeyError(
            f"未知 MCP 客户端预设 {name!r};可选:{', '.join(sorted(SCOPE_PRESETS))}"
        ) from None


def parse_scopes(values: object, *, allow_wildcard: bool = False) -> frozenset[Scope]:
    """校验并归一化作用域集合(CLI / 测试用)。

    ``values`` 接受逗号分隔字符串或字符串序列;空值 / 未知 scope 抛
    ``ValueError``。``"*"`` 仅在 ``allow_wildcard=True`` 时接受。
    """
    if isinstance(values, str):
        raw_items: list[str] = values.split(",")
    else:
        raw_items = [str(item) for item in cast(list[object], values)]
    result: set[Scope] = set()
    for raw in raw_items:
        item = raw.strip()
        if not item:
            continue
        if item == "*":
            if not allow_wildcard:
                raise ValueError("通配作用域 '*' 只用于 FINBOARD_MCP_AUTH_TOKEN 兼容")
            continue
        if item not in VALID_SCOPES:
            raise ValueError(
                f"未知 MCP 作用域 {item!r};可选:{', '.join(sorted(VALID_SCOPES))}"
            )
        result.add(item)
    if not result:
        raise ValueError("至少需要一个 MCP 作用域")
    return frozenset(result)


def required_scopes_for(tool_name: str) -> frozenset[Scope] | None:
    """解析 ``run_tool`` 传入的工具名所需作用域;未知返回 ``None``(fail-closed)。"""
    direct = TOOL_SCOPES.get(tool_name)
    if direct is not None:
        return direct
    alias = _TOOL_NAME_ALIASES.get(tool_name)
    if alias is not None:
        return TOOL_SCOPES.get(alias)
    return TOOL_SCOPES.get(tool_name.replace(".", "_"))


def identity_has_scopes(
    identity: McpClientIdentity | None, required: frozenset[Scope]
) -> bool:
    """身份是否满足所需作用域;``None``(未解析)一律拒绝。"""
    if identity is None:
        return False
    if identity.has_wildcard():
        return True
    return required <= identity.scopes


@dataclass(slots=True)
class ScopePolicy:
    """进程级作用域策略(由 server lifespan / ``__main__`` 装配)。"""

    #: 是否启用强制。未装配(直接调用 helper 的单元测试)保持旧行为。
    enforce: bool = False
    #: 只读兼容开关(下一版本删除):任何写作用域工具都拒绝。
    readonly_only: bool = False
    #: stdio / 进程内调用的默认身份(HTTP 每请求由中间件覆盖)。
    default_identity: McpClientIdentity | None = None


_policy = ScopePolicy()


def scope_policy() -> ScopePolicy:
    return _policy


def arm_scope_enforcement(
    *, readonly_only: bool, default_identity: McpClientIdentity | None = None
) -> None:
    """装配强制策略(server lifespan / ``__main__`` 启动时调用)。

    ``default_identity`` 是 stdio / 进程内调用的默认身份;HTTP 每请求由中间件
    覆盖。未装配(直接调用 helper 的单元测试)不设边界,仅测试使用。
    """
    global _policy
    _policy = replace(
        _policy,
        enforce=True,
        readonly_only=readonly_only,
        default_identity=default_identity,
    )


def arm_scope_enforcement_preserving_identity(*, readonly_only: bool) -> None:
    """装配强制策略但保留已设置的默认身份(server lifespan 使用)。

    ``__main__`` 在 stdio 启动前已装配 ``FINBOARD_MCP_STDIO_SCOPES`` 身份;
    lifespan 只补 ``readonly_only``,不得把 stdio 默认身份覆盖成 ``None``,也
    不得把 stdio 身份带进 HTTP(HTTP 无默认身份,未解析即拒绝)。
    """
    global _policy
    _policy = replace(_policy, enforce=True, readonly_only=readonly_only)


def require_scope_enforcement_armed() -> None:
    """启动自检:作用域强制未装配即视为配置错误(具名失败,不静默 fail-open)。"""
    if not _policy.enforce:
        raise RuntimeError(
            "MCP 作用域强制未装配:启动路径必须调用 arm_scope_enforcement / "
            "arm_scope_enforcement_preserving_identity"
        )


def set_default_identity(identity: McpClientIdentity | None) -> None:
    """覆盖进程级默认身份(stdio / 测试夹具)。"""
    global _policy
    _policy = replace(_policy, default_identity=identity)


def reset_scope_policy() -> None:
    """恢复未装配状态(测试与 lifespan 收尾)。"""
    global _policy
    _policy = ScopePolicy()


__all__ = [
    "SCOPE_PRESETS",
    "TOOL_SCOPES",
    "VALID_SCOPES",
    "Scope",
    "ScopePolicy",
    "arm_scope_enforcement",
    "arm_scope_enforcement_preserving_identity",
    "identity_has_scopes",
    "parse_scopes",
    "preset_scopes",
    "require_scope_enforcement_armed",
    "required_scopes_for",
    "reset_scope_policy",
    "scope_policy",
    "set_default_identity",
]
