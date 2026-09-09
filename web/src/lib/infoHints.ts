import type { LocalizedText } from "@/i18n";

export interface InfoHintDefinition {
  title: LocalizedText;
  description: LocalizedText;
  detail?: LocalizedText;
}

export const INFO_HINTS = {
  backtest: {
    strategy: {
      title: { zh: "回测策略", en: "Backtest strategy" },
      description: {
        zh: "决定如何根据历史行情生成交易信号。这里只会运行后端登记且支持回测的内置策略。",
        en: "Determines how trading signals are generated from historical bars. Only built-in strategies registered on the backend with backtest support are run here.",
      },
      detail: {
        zh: "切换策略会同时切换下方参数，不会启动实盘策略。",
        en: "Switching the strategy also switches the parameters below; it never starts a live strategy.",
      },
    },
    startDate: {
      title: { zh: "回测开始日期", en: "Backtest start date" },
      description: {
        zh: "历史行情回放的起点，开始日与结束日均计入回测区间。",
        en: "The starting point of historical bar replay; both start and end dates are included in the backtest window.",
      },
      detail: {
        zh: "区间应覆盖足够多的交易日，均线等指标还需要预热数据。",
        en: "The window should cover enough trading days; indicators such as moving averages also need warm-up data.",
      },
    },
    endDate: {
      title: { zh: "回测结束日期", en: "Backtest end date" },
      description: {
        zh: "历史行情回放的终点，不能早于开始日期，也不能晚于已有行情数据。",
        en: "The end point of historical bar replay; it cannot be earlier than the start date or later than the available bar data.",
      },
    },
    initialCapital: {
      title: { zh: "初始资金", en: "Initial capital" },
      description: {
        zh: "纸面账户在回测开始时可使用的现金，单位为人民币元。",
        en: "Cash available to the paper account at the start of the backtest, in CNY.",
      },
      detail: {
        zh: "它会影响可买数量和收益率计算，但不会读取或改变实盘账户资金。",
        en: "It affects buyable quantities and return calculations, but never reads or changes live account funds.",
      },
    },
    strategyParams: {
      title: { zh: "策略参数", en: "Strategy parameters" },
      description: {
        zh: "控制信号生成与仓位上限。字段由后端策略 schema 生成，并在运行回测前校验。",
        en: "Controls signal generation and position limits. Fields are generated from the backend strategy schema and validated before the backtest runs.",
      },
    },
    commissionRate: {
      title: { zh: "佣金率", en: "Commission rate" },
      description: {
        zh: "按成交金额收取的交易佣金，输入十进制费率。",
        en: "Trading commission charged on turnover, entered as a decimal rate.",
      },
      detail: {
        zh: "例如 0.0003 表示万分之三（万 3），买入和卖出都会计收。",
        en: "For example 0.0003 means 0.03% (3 bp); charged on both buys and sells.",
      },
    },
    minimumCommission: {
      title: { zh: "最低佣金", en: "Minimum commission" },
      description: {
        zh: "单笔成交佣金的最低金额，单位为人民币元。",
        en: "The minimum commission per fill, in CNY.",
      },
      detail: {
        zh: "实际佣金取“成交额 × 佣金率”和该金额中的较大值。",
        en: "The actual commission is the larger of turnover × rate and this amount.",
      },
    },
    stampTax: {
      title: { zh: "印花税", en: "Stamp tax" },
      description: {
        zh: "卖出成交时按成交金额计收的税费，输入十进制费率。",
        en: "Tax charged on sell fills based on turnover, entered as a decimal rate.",
      },
      detail: {
        zh: "A 股股票常见示例为 0.0005（万 5）；ETF 通常填 0。请按回测假设核实。",
        en: "A common example for A-share stocks is 0.0005 (5 bp); ETFs are usually 0. Verify against your backtest assumptions.",
      },
    },
    slippage: {
      title: { zh: "滑点", en: "Slippage" },
      description: {
        zh: "模拟信号价格与实际成交价格之间的不利偏差，单位为 bps。",
        en: "Adverse deviation between simulated signal price and actual fill price, in bps.",
      },
      detail: {
        zh: "1 bps = 0.01%。数值越大，回测中的模拟成交成本越高。",
        en: "1 bps = 0.01%. Higher values make simulated fill costs in the backtest more expensive.",
      },
    },
    symbols: {
      title: { zh: "回测标的", en: "Backtest symbols" },
      description: {
        zh: "选择参与同一次历史回放的证券代码。列表来自已同步的标的池。",
        en: "Select the securities that participate in the same historical replay. The list comes from the synced universe.",
      },
      detail: {
        zh: "标的必须已有对应区间的缓存行情，否则回测可能失败或缺少数据。",
        en: "Symbols must already have cached bars for the window, otherwise the backtest may fail or miss data.",
      },
    },
  },
  data: {
    databaseUniverse: {
      title: { zh: "数据库标的元数据", en: "Database instrument metadata" },
      description: {
        zh: "已同步到 PostgreSQL 且当前状态为 active 的标的数量。",
        en: "Number of instruments synced to PostgreSQL whose current status is active.",
      },
      detail: {
        zh: "它与行情缓存数量不同；运行集成测试、同步失败或尚未同步时，可能只有少量测试/历史记录。",
        en: "It differs from the bar cache count; after integration tests, sync failures, or before the first sync there may only be a few test/legacy records.",
      },
    },
    universeSync: {
      title: { zh: "标的池同步", en: "Universe sync" },
      description: {
        zh: "从 akshare 获取 A 股和 ETF 的代码、名称、市场、类型与交易所，并更新数据库。",
        en: "Fetches codes, names, markets, types and exchanges for A-shares and ETFs from akshare and updates the database.",
      },
      detail: {
        zh: "这里只同步元数据，不拉取历史行情。上游异常时原有标的不会被空列表覆盖。",
        en: "Only metadata is synced; no historical bars are pulled. If the upstream fails, existing instruments are never overwritten with an empty list.",
      },
    },
    bulkDownload: {
      title: { zh: "批量行情拉取", en: "Bulk bar download" },
      description: {
        zh: "按数据库中当前活跃标的批量下载日线，并写入本地 Parquet 缓存。",
        en: "Bulk-downloads daily bars for currently active instruments in the database and writes them to the local Parquet cache.",
      },
      detail: {
        zh: "任务依赖标的池元数据；如果数据库只有两只标的，批量任务也只会处理这两只。",
        en: "The job depends on universe metadata; if the database only has two instruments, the bulk job only processes those two.",
      },
    },
    instrumentList: {
      title: { zh: "活跃标的列表", en: "Active instrument list" },
      description: {
        zh: "来自 PostgreSQL 标的元数据表，点击一行可带入单标的拉取。",
        en: "Comes from the PostgreSQL instrument metadata table; clicking a row fills it into the single-symbol fetch form.",
      },
      detail: {
        zh: "这里不是缓存文件列表；缓存覆盖请查看页面底部“已缓存数据”。",
        en: "This is not the cache file list; see “Cached Data” at the bottom of the page for cache coverage.",
      },
    },
    symbol: {
      title: { zh: "证券标的代码", en: "Security symbol" },
      description: {
        zh: "采用“代码.交易所”格式，例如 510300.SH 或 000001.SZ。",
        en: "Uses the CODE.EXCHANGE format, e.g. 510300.SH or 000001.SZ.",
      },
      detail: {
        zh: "点击下方标的列表中的一行，可以把代码带入单标的拉取表单。",
        en: "Click a row in the instrument list below to carry the code into the single-symbol fetch form.",
      },
    },
    dateRange: {
      title: { zh: "行情日期范围", en: "Bar date range" },
      description: {
        zh: "请求并缓存该区间内的日线数据，开始日和结束日均包含在范围内。",
        en: "Requests and caches daily bars within this window; both start and end dates are included.",
      },
    },
    market: {
      title: { zh: "市场", en: "Market" },
      description: {
        zh: "限定批量任务处理的证券市场。当前阶段主要使用 A 股数据。",
        en: "Limits the markets the bulk job processes. At this stage mainly A-share data is used.",
      },
    },
    instrumentType: {
      title: { zh: "标的类型", en: "Instrument type" },
      description: {
        zh: "限定批量任务处理的证券类别；留空表示该市场下的全部支持类型。",
        en: "Limits the security classes the bulk job processes; leave empty for all supported types in the market.",
      },
    },
    bulkStartDate: {
      title: { zh: "批量起始日期", en: "Bulk start date" },
      description: {
        zh: "每个标的首次拉取时使用的最早日期；已有缓存会按增量方式补齐。",
        en: "The earliest date used on first fetch per symbol; existing caches are topped up incrementally.",
      },
      detail: {
        zh: "更早的日期会增加下载时间和本地缓存体积。",
        en: "Earlier dates increase download time and local cache size.",
      },
    },
    bulkSource: {
      title: { zh: "批量行情数据源", en: "Bulk bar data source" },
      description: {
        zh: "选择本次批量任务使用的行情提供方；留空时使用系统默认配置。",
        en: "Chooses the bar provider for this bulk job; leave empty to use the system default.",
      },
      detail: {
        zh: "Tushare 批量任务支持 A 股股票与指数（缓存保持单一来源），指数走 index_daily 专属接口、2000 积分档实测可调；ETF 暂仅支持 akshare/yfinance（复权口径对齐设计中，#341）。",
        en: "Tushare bulk jobs cover A-share stocks and indices (single-source cache); indices use the dedicated index_daily endpoint, verified callable at the 2000-point tier. ETF is still akshare/yfinance-only while adjustment semantics are being aligned (#341).",
      },
    },
    cachedData: {
      title: { zh: "缓存行情", en: "Cached bars" },
      description: {
        zh: "本地 Parquet 行情缓存，按标的、周期和复权方式分别保存。",
        en: "Local Parquet bar cache, stored separately per symbol, interval and adjustment mode.",
      },
      detail: {
        zh: "当前页面拉取默认使用 qfq（前复权）；Bar 数表示已缓存的日线记录数。",
        en: "Fetches from this page default to qfq (forward-adjusted); the Bar count is the number of cached daily records.",
      },
    },
  },
  settings: {
    dataProvider: {
      title: { zh: "行情数据源", en: "Bar data provider" },
      description: {
        zh: "负责获取历史行情和标的池的外部数据提供方。",
        en: "The external provider responsible for historical bars and the instrument universe.",
      },
      detail: {
        zh: "通过 FINBOARD_DATA_PROVIDER 环境变量切换；保存本页不会改变数据源。",
        en: "Switched via the FINBOARD_DATA_PROVIDER environment variable; saving this page does not change the provider.",
      },
    },
    syncTime: {
      title: { zh: "标的池同步时间", en: "Universe sync time" },
      description: {
        zh: "在交易日按 Asia/Shanghai 时区触发标的元数据同步。",
        en: "Triggers instrument metadata sync on trading days in the Asia/Shanghai timezone.",
      },
      detail: {
        zh: "该任务只更新标的资料，不拉取历史行情，也不执行交易。",
        en: "This job only updates instrument metadata; it pulls no historical bars and never trades.",
      },
    },
    downloadTime: {
      title: { zh: "增量拉取时间", en: "Incremental download time" },
      description: {
        zh: "在交易日盘后触发行情缓存增量更新，时区为 Asia/Shanghai。",
        en: "Triggers incremental bar cache updates after market close on trading days, in Asia/Shanghai.",
      },
    },
    lookbackDays: {
      title: { zh: "回溯天数", en: "Lookback days" },
      description: {
        zh: "每次增量任务向前重复请求的自然日数量，用于补齐修订或漏失数据。",
        en: "How many calendar days each incremental job re-requests backwards, to catch revisions or missing data.",
      },
      detail: {
        zh: "常见值为 3–10 天；增大后请求量也会增加。",
        en: "Common values are 3–10 days; larger values increase request volume.",
      },
    },
    downloadMarkets: {
      title: { zh: "拉取市场", en: "Download markets" },
      description: {
        zh: "选择定时增量任务覆盖的市场，可同时选择多个。",
        en: "Markets covered by the scheduled incremental job; multiple selections allowed.",
      },
    },
    downloadTypes: {
      title: { zh: "拉取类型", en: "Download types" },
      description: {
        zh: "选择定时增量任务覆盖的证券类别，可同时选择多个。",
        en: "Security classes covered by the scheduled incremental job; multiple selections allowed.",
      },
    },
  },
  strategies: {
    builtinStrategies: {
      title: { zh: "内置策略", en: "Built-in strategies" },
      description: {
        zh: "这里只列出后端已登记、参数 schema 明确的策略，不接收或执行网页输入的代码。",
        en: "Only strategies registered on the backend with an explicit parameter schema are listed here; no code submitted from the web is accepted or executed.",
      },
    },
    backtestCapability: {
      title: { zh: "回测能力", en: "Backtest capability" },
      description: {
        zh: "“可用于回测”表示策略能消费历史行情事件；实时时钟策略只能在实时运行环境中工作。",
        en: "“Backtest-capable” means the strategy consumes historical bar events; realtime-clock strategies only work in live runtimes.",
      },
    },
    presetName: {
      title: { zh: "策略预设", en: "Strategy preset" },
      description: {
        zh: "为一组策略参数保存便于识别的名称，最长 100 个字符。",
        en: "Save a recognizable name for a set of strategy parameters, up to 100 characters.",
      },
      detail: {
        zh: "保存或更新预设只写入研究配置，不会启动策略、下单或改变实盘配置。",
        en: "Saving or updating a preset only writes research configuration; it never starts strategies, places orders, or changes live configuration.",
      },
    },
    savedPresets: {
      title: { zh: "已保存预设", en: "Saved presets" },
      description: {
        zh: "可重复载入、修改或带入回测的参数快照。",
        en: "Parameter snapshots that can be reloaded, modified, or carried into backtests.",
      },
      detail: {
        zh: "删除预设不会删除回测历史，也不会影响正在运行的策略。",
        en: "Deleting a preset never deletes backtest history nor affects running strategies.",
      },
    },
  },
  jobs: {
    status: {
      title: { zh: "任务状态", en: "Job status" },
      description: {
        zh: "queued=排队；running=执行中；retry_waiting=等待自动重试；succeeded/failed/cancelled/interrupted=终态。",
        en: "queued=waiting; running=executing; retry_waiting=awaiting automatic retry; succeeded/failed/cancelled/interrupted=terminal.",
      },
      detail: {
        zh: "失败任务的 error_summary 头部带 [stage=...; decision=...] 定位信息；终态任务可归档隐藏但不会删除。",
        en: "A failed job's error_summary starts with [stage=...; decision=...] locating context; archived terminal jobs are hidden but never deleted.",
      },
    },
    kind: {
      title: { zh: "任务类型", en: "Job kind" },
      description: {
        zh: "data_sync/bulk_download=行情数据；dataset_publish/dataset_sync=研究数据；research_run/backtest_run/validation_experiment/research_code_run=研究与回测。",
        en: "data_sync/bulk_download=market data; dataset_publish/dataset_sync=research data; research_run/backtest_run/validation_experiment/research_code_run=research & backtesting.",
      },
    },
  },
} satisfies Record<string, Record<string, InfoHintDefinition>>;
