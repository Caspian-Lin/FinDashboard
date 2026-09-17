# Issue #205:事件驱动回测 fills.date 取实际交易日

- 日期:2026-08-19
- PR:#210(分支 `feat/fills-date-trading-day-205`,里程碑 `m/research-backtest`)

## 主题

事件驱动回测所有 `fills[].date`(REST / MCP 响应与 `backtest_runs.fills`
落库 JSON)曾是任务运行日而非成交实际交易日;#205 起改为实际交易日。

## 结论 / 事实

- 根因:`BacktestBroker._execute_fill` 构造 `Fill` 未传 `filled_at`,落到
  `finboard_shared.Fill.filled_at` 默认 `_utcnow()`;API/MCP 落库
  `str(f.filled_at.date())` 即运行日。equity_curve 一直按 business_date
  正确,所以此前未暴露。
- 修复:`filled_at = market_close(self._current_date)`。`market_close`
  (Asia/Shanghai 17:00)从 engine.py 私有 `_market_close` 提升到
  `clock.py`,engine 的 selection `decision_at` 与 broker 的
  `Fill.filled_at` 共用同一实现——engine import broker,broker 反向 import
  engine 会循环依赖,`clock.py` 是双方无环的公共下游。
- 回测域只有 broker `_execute_fill` 一处 `Fill(` 构造点(grep 验证);
  research_run 管线的 `ResearchFill` 是另一个类型(portfolio_pipeline 以
  `execution_at` 构造),日期本来就正确,不受影响。
- 旧落库记录不回填:历史 run 无撮合 Bar 日期归档,无法可靠反推;新旧差异
  以 `backtest_runs.created_at` 区分(README / backtest README / Skill
  tools.md 已注明)。

## Why

下游(含 OpenCode agent)按 `fills.date` 分析成交节奏/持仓周期时,运行日
日期会产生完全错误的结论;而 equity 曲线正确导致问题长期不可见——这是
「同一结果对象里两个字段时间语义不一致」的隐性陷阱。

## How to apply

- 回测域任何新代码构造 `Fill` 都必须显式传 `filled_at`(默认值是
  `_utcnow()`,静默落回运行日);需要"某交易日时点"时用
  `finboard_backtest.clock.market_close`,不要新造时点约定。
- 给 `run_backtest_and_persist` 写无网络集成测试:monkeypatch
  `finboard_data.AkShareProvider`(service 函数体内延迟 import,patch
  源模块属性即生效)注入内存行情源即可走真实落库链路。
- 分析旧回测 fills 时先看 `created_at`:#205 之前(2026-08-19 前)的
  `fills[].date` 是运行日,勿与新记录混排。
