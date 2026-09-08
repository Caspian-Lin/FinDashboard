# Issue #385/#386:publish full_market 板块过滤 + 发布质量门异常口径拆分(2026-09-08)

PR #387(分支 `feat/publish-filter-quality-gate-385-386`,base `m/research-backtest`)。起因:`a-share-full-20260908-v4`(v4a,full_market 5556 只)发布被质量门拦截,失败全部集中在北交所 920 系,且 `data_quality_check` 与发布门计数互相矛盾。

## 取证结论(排障时直接复用)

- **920 系「64.8% 异常率」不是坏数据**:是 `_audit_bars` 的 lifecycle 计数——bar 早于 `instruments.list_date` 每根 +1。北交所 2024-2025 代码切换(老 8xx/43x → 920 新代码)后,数据源在新代码下返回**含新三板/精选层时代的全量合法历史**,而 list_date 是北交所/精选层挂牌日。逐只精确对上:920023.BJ 1606/2479=64.78%、920017.BJ 960/1754=54.73%、920985.BJ 569、920992.BJ 4、920000.BJ 23(920000 的 2017-2020 段 bar 稀疏,新三板时代只有成交日有 bar)。
- **「零成交/停牌日算异常」猜测不成立**:`BarQualityChecker._check_bar` 对零成交且 OHLC 全等本就豁免;这些标的 bar 里甚至没有零成交日(新三板段只落成交日)。
- **两套口径矛盾的根源**:发布门 `_audit_bars` 内部也调 `BarQualityChecker`,但把 `anomaly_count = OHLCV + lifecycle + duplicates` 混成一体;`data_quality_check` 只用 `BarQualityChecker`。修复后 anomaly_count 两处同义,残留差异只有「冻结窗口 vs 全量缓存」。
- **publish 无过滤参数的不对称**:bulk_download 有 `exchange`/`listing_boards`(且持久层 `InstrumentRepository.list_codes` 本就支持两参),publish 三选一来源没有——缺口只在 schema 与 `resolve_release_symbols` 透传。

## 修复语义

- #385:`listing_boards`/`exchange` **仅 full_market 生效**,与其他来源混用具名拒绝(`symbol_filter_requires_full_market`,schema+repo 双层);boards 词表小写(`sse_main/szse_main/star/chinext/bse/cdr`),ETF/指数/转债恒为 `unknown`(mixed 发布过滤后想保留须显式含 unknown);exchange 词表大写。
- #386:`anomaly_count`=OHLCV only;`duplicate_count` 独立**仍阻断**;`pre_list_bars`/`post_delist_bars` 可见**不阻断**(issues + quality_report 聚合 + ReleasedInstrument 序列化);OHLCV 异常带 `anomaly_reasons:` 明细;as_dict **零值省略键**保旧 manifest 逐字节兼容。

## 坑位

- `DatasetReleaseSpec.required_capabilities` **默认含四类 ETF 能力**——单股票标 的 builder 冒烟测试必须显式 `required_capabilities=("stock",)`,否则 `capability:etf:*:no_instruments` 秒拒。
- instruments.listing_board 与 exchange 大小写不一致(board 小写/exchange 大写),schema 层归一化别漏。
- `_coverage_summary` 断言在 `test_releases.py:253` 是整字典相等,加聚合键必须同步该测试。
- full_market 股票发布剔除北交所的验收口径:`listing_boards=[sse_main,szse_main,star,chinext,cdr]`(不含 bse)。

测试:`tests/unit/test_issue_385_publish_board_filter.py`、`tests/unit/data/test_issue_386_quality_gate_breakdown.py` + 路由/序列化既有文件补用例;回归 381 passed。
