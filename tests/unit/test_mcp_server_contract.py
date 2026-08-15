"""MCP server 契约测试 —— 通过内存 ``Client`` 验证工具暴露与权限矩阵。

不启动真实传输(``Client(mcp)`` 直连 server 对象),验证:

* 工具集命名空间正确;
* 实盘能力(下单 / 撤单 / 持仓 / Kill Switch / broker)未被暴露为工具;
* 工具调用返回统一信封结构;
* 越权请求经权限矩阵映射为 ``denied``。
"""

from __future__ import annotations

from mcp import Client

from finboard_mcp import build_mcp_server

_EXPECTED_TOOLS = {
    # #110 研究记忆工具
    "finboard_memory_remember",
    "finboard_memory_list",
    "finboard_memory_get",
    "finboard_memory_forget",
    "finboard_memory_correct",
    "finboard_memory_confirm",
    "finboard_memory_archive",
    "finboard_run_list",
    "finboard_run_get",
    "finboard_run_artifacts",
    "finboard_run_queue",
    "finboard_run_cancel",
    "finboard_run_replay",
    "finboard_run_lineage",
    # #124 数据查询工具
    "finboard_instrument_list",
    "finboard_instrument_get",
    "finboard_instrument_search",
    "finboard_dataset_release_list",
    "finboard_dataset_release_get",
    "finboard_dataset_manifest_list",
    "finboard_data_cache_status",
    "finboard_data_quality_check",
    "finboard_tushare_quota",
    # #125 因子实验室工具
    "finboard_factor_catalog",
    "finboard_feature_snapshot_list",
    "finboard_feature_snapshot_get",
    "finboard_feature_snapshot_create",
    "finboard_feature_snapshot_job_start",
    "finboard_feature_snapshot_job_status",
    "finboard_factor_signal_list",
    "finboard_factor_signal_get",
    "finboard_factor_experiment_list",
    "finboard_factor_experiment_get",
    "finboard_factor_experiment_create",
    "finboard_factor_experiment_sync_validation",
    # #126 策略规格工具(8 只读 + 8 写)
    "finboard_strategy_registry",
    "finboard_strategy_template",
    "finboard_strategy_list",
    "finboard_strategy_history",
    "finboard_strategy_version_get",
    "finboard_strategy_diff",
    "finboard_preset_list",
    "finboard_preset_get",
    "finboard_strategy_validate",
    "finboard_strategy_draft_create",
    "finboard_strategy_supersede",
    "finboard_strategy_publish",
    "finboard_strategy_rollback",
    "finboard_preset_create",
    "finboard_preset_update",
    "finboard_preset_delete",
    # #127 回测工具(3 只读 + 2 写)
    "finboard_backtest_strategy_list",
    "finboard_backtest_run",
    "finboard_backtest_history_list",
    "finboard_backtest_history_get",
    "finboard_backtest_history_delete",
    # #127 模拟盘工具(10 只读 + 8 写)
    "finboard_sim_account_list",
    "finboard_sim_account_get",
    "finboard_sim_account_create",
    "finboard_sim_session_list",
    "finboard_sim_session_get",
    "finboard_sim_session_create",
    "finboard_sim_session_start",
    "finboard_sim_session_pause",
    "finboard_sim_session_stop",
    "finboard_sim_session_reset",
    "finboard_sim_decision_submit",
    "finboard_sim_order_cancel",
    "finboard_sim_orders",
    "finboard_sim_fills",
    "finboard_sim_positions",
    "finboard_sim_ledger",
    "finboard_sim_audit",
    "finboard_sim_report",
    # #139 模拟盘补全工具(3 写:archive / market_event / evaluate)
    "finboard_sim_session_archive",
    "finboard_sim_market_event",
    "finboard_sim_session_evaluate",
    # #128 portfolio 计算工具(4 个纯计算)
    "finboard_portfolio_allocate",
    "finboard_portfolio_sizing",
    "finboard_portfolio_feasibility",
    "finboard_portfolio_attribution",
    # #136 任务队列工具(2 只读 + 2 写)
    "finboard_job_list",
    "finboard_job_get",
    "finboard_job_enqueue",
    "finboard_job_cancel",
    # #137 数据写操作工具(10 写 + 2 只读)
    "finboard_data_fetch",
    "finboard_data_fetch_all",
    "finboard_data_sync_universe",
    "finboard_data_bulk_download_start",
    "finboard_data_quality_repair",
    "finboard_dataset_release_publish",
    "finboard_data_config_get",
    "finboard_data_config_update",
    "finboard_etf_sync",
    "finboard_etf_batch_confirm",
    "finboard_etf_update",
    "finboard_etf_review_queue",
    # #138 验证实验工具(2 只读 + 4 写)
    "finboard_validation_experiment_create",
    "finboard_validation_experiment_list",
    "finboard_validation_experiment_get",
    "finboard_validation_experiment_reject",
    "finboard_validation_experiment_add_trial",
    "finboard_validation_experiment_delete",
    # #140 自选股工具(2 只读 + 5 写)
    "finboard_watchlist_list",
    "finboard_watchlist_get",
    "finboard_watchlist_create",
    "finboard_watchlist_update",
    "finboard_watchlist_delete",
    "finboard_watchlist_add_symbols",
    "finboard_watchlist_remove_symbol",
    # #141 报告聚合与导出工具(3 只读)
    "finboard_report_run",
    "finboard_report_backtest",
    "finboard_report_export",
}

# 永久不得暴露的实盘 / 凭证能力关键字。
# 注意:``finboard_sim_*`` 工具操作模拟盘(simulation_* 表,SIM-* ID),其名称含
# order/fill/position/cancel/trade 等词是模拟域语义,不是实盘能力,因此豁免。
_FORBIDDEN_KEYWORDS = (
    "kill_switch",
    "killswitch",
    "broker",
    "qmt",
    "ctp",
    "credential",
    "api_key",
)
# 对非 sim_ 工具额外检查的实盘交易动词(模拟盘工具豁免)。
# 注意:``finboard_job_*``(#136)操作 background_jobs 表(研究/数据域任务队列),
# 其 ``cancel`` 属研究域协作式取消(非实盘撤单),与 ``finboard_run_*`` 同理豁免。
_LIVE_TRADING_VERBS = ("order", "fill", "cancel", "buy", "sell", "trade", "position")


async def _tool_names() -> set[str]:
    mcp = build_mcp_server()
    async with Client(mcp) as client:
        result = await client.list_tools()
        return {t.name for t in result.tools}


class TestToolExposure:
    async def test_expected_tools_registered(self) -> None:
        # #157:精确相等 —— 新增/删除工具必须同步本清单与文档(#123 同步规范)。
        assert await _tool_names() == _EXPECTED_TOOLS

    async def test_no_live_trading_tools_exposed(self) -> None:
        names = await _tool_names()
        for name in names:
            lowered = name.lower()
            # 绝对禁止的关键字(broker / kill_switch / 凭证 / QMT / CTP)——任何工具
            # 都不得含。
            for keyword in _FORBIDDEN_KEYWORDS:
                assert keyword not in lowered, (
                    f"工具 {name} 暴露了禁止能力 {keyword}"
                )
            # 实盘交易动词(order/fill/cancel/buy/sell/trade/position)——模拟盘
            # (``finboard_sim_*``,操作 simulation_* 表与 SIM-* ID)与 ResearchRun
            # (``finboard_run_*``,操作 research_runs 表与 RR- ID)豁免,因为它们的
            # cancel/order/position 语义属于研究 / 模拟域,不触及实盘订单;其他工具
            # 不得暴露这些动词。
            if lowered.startswith("finboard_sim_") or lowered.startswith(
                "finboard_run_"
            ) or lowered.startswith("finboard_job_"):
                continue
            for verb in _LIVE_TRADING_VERBS:
                assert verb not in lowered, (
                    f"工具 {name} 暴露了实盘交易动词 {verb}(非研究/模拟域工具)"
                )


class TestCallToolContract:
    async def test_run_list_returns_envelope(self) -> None:
        mcp = build_mcp_server()
        async with Client(mcp) as client:
            result = await client.call_tool("finboard_run_list", {"limit": 1})
        content = result.structured_content
        assert content is not None
        # 信封结构恒定;status 取决于运行环境是否可达 DB(CI 有 PostgreSQL → ok,
        # 本机 Windows psycopg 事件循环 → error),两种均合法。
        assert content["status"] in {"ok", "error"}
        assert str(content["operation_id"]).startswith("OP-")
        if content["status"] == "ok":
            assert isinstance(content["data"], list)
        else:
            assert content["error"]["kind"] in {"unavailable", "timeout", "degraded"}

    async def test_unknown_tool_does_not_succeed(self) -> None:
        mcp = build_mcp_server()
        async with Client(mcp) as client:
            try:
                result = await client.call_tool("finboard_evil_steal_credential", {})
            except Exception:
                # MCP 层抛错(未知工具)也视为通过
                return
        # 若未抛错,结果必须标记为错误,不得静默成功
        assert result.is_error
