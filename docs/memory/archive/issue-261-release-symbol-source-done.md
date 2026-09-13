# Issue #261:发布标的集来源三选一(symbols_from_release / full_market)完成

- **日期**:2026-09-02(PR #282 待合并,目标 `m/research-backtest`)
- **主题**:dataset_release_publish 免内联全市场 symbols;消灭 #252 漏配缺口的人工根源

## 结论 / 事实

- REST 与 MCP 的发布入口在**入队期**把 `symbols_from_release` / `full_market`
  解析成具体 symbols 进任务 payload,`DatasetPublishExecutor` **零改动**
  (unknown_symbols / scope / 覆盖率门原样兜底)。
- 解析器 `resolve_release_symbols` 放 `finboard_persistence.dataset_release_repo`
  (REST/MCP 共同依赖),错误类 `ReleaseSymbolSourceError(code, summary)`,
  REST→422、MCP→invalid_argument。schema 层(`ResearchDatasetReleaseCreate`)
  做三选一互斥(全缺/多声明即拒),需查库的存在性/可用性检查由解析器兜底。
- `full_market` 按 kind 语义展开:股票单源 kind(a_share_tushare /
  daily_metrics / financial_indicators)只取 `market=a_share` +
  `instrument_type=stock`;multi_asset_mixed 取 stock+etf+index 并集
  (三者 `market` 都是 `a_share`,靠 instrument_type 区分,#256 起指数入库)。
- payload 新增 `symbols_source` 溯源键 `{mode, release_id?}`,执行器忽略未知键。
- `symbols_from_release` 复制语义 verbatim(来源清单冻结不可变),不做 kind
  兼容过滤;来源须可用(`is_usable` = quality passed/warnings)。

## Why(为什么)

- 2026-09-01 全市场发布实测:5534 只内联清单 ≈70KB 且有漏配 002889.SZ 前科
  (#252)——人工维护巨型清单既笨重又是缺口来源;从上一份发布复制标的集
  在源头消灭漏配。
- 入队期解析而非执行期解析:对齐 #186/#203/#253/#255/#260 的「入队期秒级
  失败」方向,来源缺失/不可用不等排队+开跑才暴露。

## How to apply(下次如何应用)

- 跨 kind 复制(如 mixed→a_share_tushare)不在入队期拦(避免静默丢标的),
  靠执行期 scope 门具名失败——不要给解析器加 kind 过滤「好意」。
- 执行器 mixed scope 门实现要求 stock+etf+index **三类型齐备**,与 #184
  注释「至少含三者之一」口径不符(#184 遗留);full_market 混合展开天然满足,
  但「纯 stock+etf」或「纯 index」混合发布会被拒——是否修正另行确认。
- 集成测试「复制后二次发布」必须用 bars dataset:服务层
  `_require_a_share_stock_scope` 对非 bars 发布要求 source=tushare 且
  dataset_kind 为 daily_metrics/financial_indicators,fixed_sample 源 +
  ETF 标的会被拒。
- MCP 工具 keyword-only 签名里必填参数可放在有默认值参数之后(合法);
  FastMCP 按注解生成 schema,`list[str] | None = None` 自动变可选。
- REST 路由单测的 fake job row 需要 JobOut 全部必填字段
  (progress_total/payload_checksum/lease_until 等),缺一个就 ValidationError;
  从既有 `test_create_release_enqueues_dataset_publish_job` 抄完整模板。
