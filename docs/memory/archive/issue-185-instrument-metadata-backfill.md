# Issue #185:instrument 元数据回填(list_date/industry)贯通发布链路

**主题**:`instruments.list_date/industry` 从「无写入者」状态打通为
`research_instrument_profiles`(tushare `stock_basic`)回填 + 发布兜底,缺失统计可见。

**结论 / 事实**(2026-08-18,PR #196 待合并,目标分支 `m/research-backtest`):

- akshare 发现链路(`UniverseDiscovery`)只写 `instruments` 的
  code/name/market/instrument_type/exchange/listing_board;`list_date/industry`
  曾有列无写入者;`sector` 至今无上游来源(tushare stock_basic 只有 industry),保持 null。
- 回填入口:`ProfileMetadataLookup`(最近一次已发布 profiles 批次,published_at desc)
  + `InstrumentRepository.backfill_metadata_from_profiles()`(只填 null 不覆盖主数据);
  data_sync 执行器 / CLI `data-sync` 按本次发现范围(symbols)作后置 enrichment。
- 发布构造器 `ReleaseInstrumentCatalogRepository.list_candidates` 在 instruments 字段
  null 时从 profiles 兜底;`ReleaseInstrumentSpec`/`ReleasedInstrument` 新增 `industry`
  字段(manifest as_dict/from_dict 往返,旧 manifest 无该 key 兼容)。
- 缺失统计:data_sync job phase + structlog;发布 `quality_report.instrument_metadata`
  (total/missing_list_date/missing_industry)。

**Why**:universe 的 `min_listing_days` 过滤、行业中性化、industry_exposure 全部依赖
这三列;0804 发布 5534 只 stock 的 list_date 全 null 导致候选池必空(issue #185 背景)。
发布侧 `_spec_universe_candidates`(research_run 预检)直接读
`release.instruments[].list_date`,因此发布兜底是让 #186 预检「真正生效」的前提。

**How to apply**:新加 instrument 元数据字段时先确认上游来源落在哪条链路
(akshare discovery / tushare profiles / ETF catalog),再决定回填 vs 兜底;
回填语义固定为「只填 null」,避免覆盖手工校准值。发布单资产测试须显式
`required_capabilities=("stock",)`(默认要求全部 5 类资产能力)。

相关:[[issue-173-bars-snapshot-selection]]、[[issue-183-multi-period-rebalance-done]]。