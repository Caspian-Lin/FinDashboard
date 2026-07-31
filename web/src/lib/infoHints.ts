export interface InfoHintDefinition {
  title: string;
  description: string;
  detail?: string;
}

export const INFO_HINTS = {
  backtest: {
    strategy: {
      title: "回测策略",
      description: "决定如何根据历史行情生成交易信号。这里只会运行后端登记且支持回测的内置策略。",
      detail: "切换策略会同时切换下方参数，不会启动实盘策略。",
    },
    startDate: {
      title: "回测开始日期",
      description: "历史行情回放的起点，开始日与结束日均计入回测区间。",
      detail: "区间应覆盖足够多的交易日，均线等指标还需要预热数据。",
    },
    endDate: {
      title: "回测结束日期",
      description: "历史行情回放的终点，不能早于开始日期，也不能晚于已有行情数据。",
    },
    initialCapital: {
      title: "初始资金",
      description: "纸面账户在回测开始时可使用的现金，单位为人民币元。",
      detail: "它会影响可买数量和收益率计算，但不会读取或改变实盘账户资金。",
    },
    strategyParams: {
      title: "策略参数",
      description: "控制信号生成与仓位上限。字段由后端策略 schema 生成，并在运行回测前校验。",
    },
    commissionRate: {
      title: "佣金率",
      description: "按成交金额收取的交易佣金，输入十进制费率。",
      detail: "例如 0.0003 表示万分之三（万 3），买入和卖出都会计收。",
    },
    minimumCommission: {
      title: "最低佣金",
      description: "单笔成交佣金的最低金额，单位为人民币元。",
      detail: "实际佣金取“成交额 × 佣金率”和该金额中的较大值。",
    },
    stampTax: {
      title: "印花税",
      description: "卖出成交时按成交金额计收的税费，输入十进制费率。",
      detail: "A 股股票常见示例为 0.0005（万 5）；ETF 通常填 0。请按回测假设核实。",
    },
    slippage: {
      title: "滑点",
      description: "模拟信号价格与实际成交价格之间的不利偏差，单位为 bps。",
      detail: "1 bps = 0.01%。数值越大，回测中的模拟成交成本越高。",
    },
    symbols: {
      title: "回测标的",
      description: "选择参与同一次历史回放的证券代码。列表来自已同步的标的池。",
      detail: "标的必须已有对应区间的缓存行情，否则回测可能失败或缺少数据。",
    },
  },
  data: {
    databaseUniverse: {
      title: "数据库标的元数据",
      description: "已同步到 PostgreSQL 且当前状态为 active 的标的数量。",
      detail: "它与行情缓存数量不同；运行集成测试、同步失败或尚未同步时，可能只有少量测试/历史记录。",
    },
    universeSync: {
      title: "标的池同步",
      description: "从 akshare 获取 A 股和 ETF 的代码、名称、市场、类型与交易所，并更新数据库。",
      detail: "这里只同步元数据，不拉取历史行情。上游异常时原有标的不会被空列表覆盖。",
    },
    bulkDownload: {
      title: "批量行情拉取",
      description: "按数据库中当前活跃标的批量下载日线，并写入本地 Parquet 缓存。",
      detail: "任务依赖标的池元数据；如果数据库只有两只标的，批量任务也只会处理这两只。",
    },
    instrumentList: {
      title: "活跃标的列表",
      description: "来自 PostgreSQL 标的元数据表，点击一行可带入单标的拉取。",
      detail: "这里不是缓存文件列表；缓存覆盖请查看页面底部“已缓存数据”。",
    },
    symbol: {
      title: "证券标的代码",
      description: "采用“代码.交易所”格式，例如 510300.SH 或 000001.SZ。",
      detail: "点击下方标的列表中的一行，可以把代码带入单标的拉取表单。",
    },
    dateRange: {
      title: "行情日期范围",
      description: "请求并缓存该区间内的日线数据，开始日和结束日均包含在范围内。",
    },
    market: {
      title: "市场",
      description: "限定批量任务处理的证券市场。当前阶段主要使用 A 股数据。",
    },
    instrumentType: {
      title: "标的类型",
      description: "限定批量任务处理的证券类别；留空表示该市场下的全部支持类型。",
    },
    bulkStartDate: {
      title: "批量起始日期",
      description: "每个标的首次拉取时使用的最早日期；已有缓存会按增量方式补齐。",
      detail: "更早的日期会增加下载时间和本地缓存体积。",
    },
    cachedData: {
      title: "缓存行情",
      description: "本地 Parquet 行情缓存，按标的、周期和复权方式分别保存。",
      detail: "当前页面拉取默认使用 qfq（前复权）；Bar 数表示已缓存的日线记录数。",
    },
  },
  settings: {
    dataProvider: {
      title: "行情数据源",
      description: "负责获取历史行情和标的池的外部数据提供方。",
      detail: "通过 FINBOARD_DATA_PROVIDER 环境变量切换；保存本页不会改变数据源。",
    },
    syncTime: {
      title: "标的池同步时间",
      description: "在交易日按 Asia/Shanghai 时区触发标的元数据同步。",
      detail: "该任务只更新标的资料，不拉取历史行情，也不执行交易。",
    },
    downloadTime: {
      title: "增量拉取时间",
      description: "在交易日盘后触发行情缓存增量更新，时区为 Asia/Shanghai。",
    },
    lookbackDays: {
      title: "回溯天数",
      description: "每次增量任务向前重复请求的自然日数量，用于补齐修订或漏失数据。",
      detail: "常见值为 3–10 天；增大后请求量也会增加。",
    },
    downloadMarkets: {
      title: "拉取市场",
      description: "选择定时增量任务覆盖的市场，可同时选择多个。",
    },
    downloadTypes: {
      title: "拉取类型",
      description: "选择定时增量任务覆盖的证券类别，可同时选择多个。",
    },
  },
  strategies: {
    builtinStrategies: {
      title: "内置策略",
      description: "这里只列出后端已登记、参数 schema 明确的策略，不接收或执行网页输入的代码。",
    },
    backtestCapability: {
      title: "回测能力",
      description: "“可用于回测”表示策略能消费历史行情事件；实时时钟策略只能在实时运行环境中工作。",
    },
    presetName: {
      title: "策略预设",
      description: "为一组策略参数保存便于识别的名称，最长 100 个字符。",
      detail: "保存或更新预设只写入研究配置，不会启动策略、下单或改变实盘配置。",
    },
    savedPresets: {
      title: "已保存预设",
      description: "可重复载入、修改或带入回测的参数快照。",
      detail: "删除预设不会删除回测历史，也不会影响正在运行的策略。",
    },
  },
} as const satisfies Record<string, Record<string, InfoHintDefinition>>;
