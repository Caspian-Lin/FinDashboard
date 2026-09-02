import type { InfoHintDefinition } from "./infoHints";

export const RESEARCH_HINTS = {
  data: {
    releases: {
      title: { zh: "研究数据发布", en: "Research data releases" },
      description: {
        zh: "版本化的研究数据包。每次发布都包含特定时间范围、调整方式和质量检查结果。",
        en: "Versioned research data packages. Each release includes a specific time range, adjustment mode and quality check results.",
      },
      detail: {
        zh: "发布是 immutable 的（不可修改），确保研究可复现。覆盖率低于 95% 的数据集会有质量告警。",
        en: "Releases are immutable, ensuring reproducible research. Datasets with coverage below 95% raise quality warnings.",
      },
    },
    fetch: {
      title: { zh: "行情数据拉取", en: "Bar data fetch" },
      description: {
        zh: "从已配置的数据源拉取历史 K 线并缓存到本地。这是研究流程的第一步。",
        en: "Pulls historical bars from the configured data source and caches them locally. This is the first step of the research workflow.",
      },
      detail: {
        zh: "支持单标的拉取和按标的池批量下载。缓存数据用于回测、因子计算和策略验证。",
        en: "Supports single-symbol fetch and bulk download by universe. Cached data feeds backtests, factor computation and strategy validation.",
      },
    },
    datasetName: {
      title: { zh: "数据集名称", en: "Dataset name" },
      description: {
        zh: "用于归类同一条数据产品线，例如 multi_asset_daily_bars。",
        en: "Groups one data product line, e.g. multi_asset_daily_bars.",
      },
      detail: {
        zh: "同一数据集可以连续发布多个不可变版本。",
        en: "One dataset can publish multiple immutable versions over time.",
      },
    },
    version: {
      title: { zh: "数据版本", en: "Data version" },
      description: {
        zh: "面向研究者识别的数据版本；在同一数据集和来源下必须唯一。",
        en: "The researcher-facing data version; must be unique within the same dataset and source.",
      },
      detail: {
        zh: "建议包含日期和递增序号，例如 2026-07-31-v1。",
        en: "Recommended to include a date and incrementing sequence, e.g. 2026-07-31-v1.",
      },
    },
    releaseId: {
      title: { zh: "发布 ID", en: "Release ID" },
      description: {
        zh: "系统引用这个不可变发布的唯一标识，创建后不能换内容复用。",
        en: "The unique identifier the system uses to reference this immutable release; it cannot be reused with different content.",
      },
      detail: {
        zh: "重复提交完全相同的发布可幂等读取；同 ID 内容变化会被拒绝。",
        en: "Re-submitting an identical release reads idempotently; the same ID with different content is rejected.",
      },
    },
    source: {
      title: { zh: "数据来源", en: "Data source" },
      description: {
        zh: "声明本地缓存最初来自哪个数据提供方，用于血缘和许可审计。",
        en: "Declares which provider the local cache originally came from, for lineage and license audit.",
      },
      detail: {
        zh: "它不会触发联网下载；请选择与缓存实际来源一致的值。",
        en: "It never triggers a network download; choose the value that matches the cache's actual origin.",
      },
    },
    adjustment: {
      title: { zh: "复权方式", en: "Adjustment mode" },
      description: {
        zh: "决定发布读取哪组本地缓存：前复权、后复权或不复权。",
        en: "Determines which local cache the release reads: forward-adjusted, backward-adjusted, or unadjusted.",
      },
      detail: {
        zh: "不同复权方式是不同数据，不会在发布时临时换算。",
        en: "Different adjustment modes are different data; no on-the-fly conversion happens at publish time.",
      },
    },
    releaseScope: {
      title: { zh: "质量门范围", en: "Quality gate scope" },
      description: {
        zh: "选定标的研究只检查所选数据；正式多资产研究还要求完整资产能力覆盖。",
        en: "Selected-symbol research only checks the chosen data; formal multi-asset research additionally requires full asset capability coverage.",
      },
      detail: {
        zh: "正式多资产质量门缺任一股票/宽基/跨境/商品/债券 ETF 能力都会失败关闭。",
        en: "The formal multi-asset quality gate fails closed if any stock/broad-index/cross-border/commodity/bond ETF capability is missing.",
      },
    },
    cachedSymbols: {
      title: { zh: "可发布标的", en: "Publishable symbols" },
      description: {
        zh: "这里只列出本地已有对应周期和复权缓存的标的。",
        en: "Only symbols that already have local caches for the selected interval and adjustment are listed.",
      },
      detail: {
        zh: "可一键选择当前筛选下的全部缓存；覆盖率按标的上市至退市的有效生命周期计算。",
        en: "All caches under the current filter can be selected at once; coverage is computed over each symbol's listed-to-delisted lifecycle.",
      },
    },
    releaseList: {
      title: { zh: "发布记录", en: "Release history" },
      description: {
        zh: "已通过质量门并登记到数据库的不可变数据版本。",
        en: "Immutable data versions that passed the quality gate and are registered in the database.",
      },
      detail: {
        zh: "列表为空不代表没有行情缓存；缓存必须先通过“创建数据发布”才能成为研究输入。",
        en: "An empty list does not mean there is no bar cache; caches must pass “Create Data Release” before becoming research inputs.",
      },
    },
    manifests: {
      title: { zh: "数据集清单", en: "Dataset manifests" },
      description: {
        zh: "数据同步管线登记的数据集级摘要，包含行数、覆盖率、缺口和质量状态。",
        en: "Dataset-level summaries registered by the data sync pipeline, including row counts, coverage, gaps and quality status.",
      },
      detail: {
        zh: "它与不可变研究数据发布是不同层级：清单描述数据，发布冻结可复现输入。",
        en: "It is a different layer from immutable research releases: manifests describe data, releases freeze reproducible inputs.",
      },
    },
    instrumentMetadata: {
      title: { zh: "标的元数据", en: "Instrument metadata" },
      description: {
        zh: "数据库中的名称、市场、类型、上市状态和生命周期资料。",
        en: "Names, markets, types, listing status and lifecycle information in the database.",
      },
      detail: {
        zh: "行情缓存与标的元数据独立存储；已有缓存不会自动补齐元数据，需要执行标的池同步。",
        en: "Bar caches and instrument metadata are stored independently; existing caches do not backfill metadata automatically — run universe sync.",
      },
    },
    instrumentCounts: {
      title: { zh: "为什么 ETF 数量不一致？", en: "Why do ETF counts differ?" },
      description: {
        zh: "标的字典统计 instruments 表，ETF 分类统计 etf_metadata 表，两者不是同一份清单。",
        en: "The instrument dictionary counts the instruments table while ETF categories count the etf_metadata table — they are not the same list.",
      },
      detail: {
        zh: "分类目录会保留历史或暂未进入当前标的池的基金记录；发布和行情拉取应以当前标的字典中的活跃标的为准。",
        en: "Category directories keep historical funds not yet in the current universe; publishing and bar fetches should follow active instruments in the current dictionary.",
      },
    },
    lifecycle: {
      title: { zh: "标的生命周期", en: "Instrument lifecycle" },
      description: {
        zh: "标的的上市/退市、分红送股、ST 摘帽、停牌等事件。影响回测的可用于性。",
        en: "Events such as listing/delisting, dividends and bonus shares, ST cap removal, and suspensions. Affects backtest tradability.",
      },
      detail: {
        zh: "历史发布应保留中途上市、退市和停牌的真实状态；策略必须按决策时点过滤可交易性，不能用今天的状态回填历史。",
        en: "Historical releases should preserve the true listing/delisting/suspension states; strategies must filter tradability as of each decision point, never backfilling history with today's state.",
      },
    },
  },
  factors: {
    catalog: {
      title: { zh: "因子目录", en: "Factor catalog" },
      description: {
        zh: "系统中已实现的因子列表。每个因子有明确的计算公式和经济含义。",
        en: "Factors implemented in the system. Each has an explicit formula and economic meaning.",
      },
      detail: {
        zh: "因子的 role 分为：alpha（超额收益因子）、risk（风险因子）、market（行情字段）。只有有真实实现的因子才会出现在目录中。",
        en: "Factor roles are: alpha (excess-return factors), risk (risk factors), market (bar fields). Only factors with real implementations appear in the catalog.",
      },
    },
    features: {
      title: { zh: "特征快照", en: "Feature snapshots" },
      description: {
        zh: "因子计算结果的不可变快照。包含特定数据版本下所有标的的因子值。",
        en: "Immutable snapshots of factor computation results, containing factor values for all symbols under a specific data version.",
      },
      detail: {
        zh: "特征快照是研究可复现的基础 —— 相同的快照 + 相同的策略代码 = 相同的结果。",
        en: "Feature snapshots are the foundation of reproducible research — same snapshot + same strategy code = same results.",
      },
    },
    signals: {
      title: { zh: "因子信号", en: "Factor signals" },
      description: {
        zh: "将因子值转化为买卖信号的规则产物。例如：动量因子排名前 20% → 买入信号。",
        en: "Rule outputs that turn factor values into buy/sell signals. For example: top 20% momentum ranking → buy signal.",
      },
      detail: {
        zh: "信号连接了因子（数值）和策略（决策）。一个因子可以生成多种信号。",
        en: "Signals connect factors (numbers) and strategies (decisions). One factor can generate multiple signals.",
      },
    },
    experiments: {
      title: { zh: "因子实验", en: "Factor experiments" },
      description: {
        zh: "对因子预测能力的正式验证。冻结假设、数据版本和评估计划后执行。",
        en: "Formal validation of a factor's predictive power, executed after freezing the hypothesis, data version and evaluation plan.",
      },
      detail: {
        zh: "因子实验回答：这个因子真的能预测未来收益吗？通过 IC 分析、分层回测等方法评估。",
        en: "A factor experiment answers: does this factor truly predict future returns? Evaluated via IC analysis, layered backtests and more.",
      },
    },
  },
  strategy: {
    studio: {
      title: { zh: "策略 Studio", en: "Strategy Studio" },
      description: {
        zh: "使用结构化无代码组件配置研究策略。不写一行 Python 代码即可定义完整的策略逻辑。",
        en: "Configure research strategies with structured no-code components. Define complete strategy logic without writing a line of Python.",
      },
      detail: {
        zh: "策略由 8 个部分组成：标的域 → 特征图 → 信号规则 → 组合策略 → 风险退出 → 执行模型 → 验证计划 → 兼容性。每个部分都有白名单组件可选。",
        en: "A strategy has 8 parts: universe → feature graph → signal rules → portfolio policy → risk exits → execution model → validation plan → compatibility. Each part offers whitelisted components.",
      },
    },
    presets: {
      title: { zh: "策略预设 vs 策略 Studio", en: "Strategy Presets vs Strategy Studio" },
      description: {
        zh: "预设用于快速回测（简单的策略名 + 参数 + 因子筛选），适合探索性测试。",
        en: "Presets are for quick backtests (simple strategy name + params + factor screen), suited to exploratory testing.",
      },
      detail: {
        zh: "策略 Studio 用于正式研究规格（完整的特征图/信号规则/组合策略/风险退出/验证计划），适合需要可复现和正式验证的研究。简单说：预设是草稿纸，Studio 是正式文档。",
        en: "Strategy Studio is for formal research specs (full feature graph / signal rules / portfolio policy / risk exits / validation plan), suited to reproducible, formally validated research. In short: presets are scratch paper, Studio is the formal document.",
      },
    },
    validate: {
      title: { zh: "即时校验", en: "Instant validation" },
      description: {
        zh: "校验策略规格是否满足后端 schema 约束。只有校验通过的规格才能保存和发布。",
        en: "Validates the strategy spec against backend schema constraints. Only specs that pass validation can be saved and published.",
      },
      detail: {
        zh: "校验包括：必填字段完整性、枚举值合法性、特征依赖一致性、信号规则与特征匹配等。",
        en: "Validation covers required-field completeness, enum legality, feature dependency consistency, and signal-rule/feature matching.",
      },
    },
    publish: {
      title: { zh: "发布策略", en: "Publish strategy" },
      description: {
        zh: "将策略版本标记为已发布。发布不会自动启动回测或研究运行。",
        en: "Marks a strategy version as published. Publishing never auto-starts backtests or research runs.",
      },
      detail: {
        zh: "已发布的策略版本可以被研究运行引用。未发布的版本仅作为草稿存在。",
        en: "Published strategy versions can be referenced by research runs; unpublished versions exist only as drafts.",
      },
    },
  },
  experiments: {
    oos: {
      title: { zh: "什么是 OOS（样本外验证）？", en: "What is OOS (out-of-sample validation)?" },
      description: {
        zh: "OOS = Out-of-Sample。用策略参数优化时未使用过的数据来检验策略的真实表现。",
        en: "OOS = Out-of-Sample. Tests a strategy's true performance on data never used during parameter optimization.",
      },
      detail: {
        zh: "如果在 2020-2022 年的数据上优化策略参数，然后用 2023 年的数据检验 —— 2023 年就是 OOS 数据。如果策略在 OOS 上也表现良好，说明它不是过拟合。",
        en: "If you optimize parameters on 2020-2022 data and then check on 2023 data, 2023 is the OOS data. If the strategy also performs well there, it is not overfitted.",
      },
    },
    validation: {
      title: { zh: "为什么要做机器验证？", en: "Why machine validation?" },
      description: {
        zh: "回测结果好不代表策略有效。机器验证通过多维度检验排除过拟合和运气。",
        en: "A good backtest does not mean an effective strategy. Machine validation rules out overfitting and luck via multi-dimensional checks.",
      },
      detail: {
        zh: "验证包括：OOS 夏普比率、最大回撤、PBO（回测过拟合概率）、Deflated Sharpe、参数敏感性等。只有通过所有阈值才认为策略有效。",
        en: "Validation includes OOS Sharpe, max drawdown, PBO (probability of backtest overfitting), Deflated Sharpe, parameter sensitivity and more. A strategy is considered valid only after passing all thresholds.",
      },
    },
    difference: {
      title: { zh: "验证实验 vs 回测 vs 模拟盘", en: "Validation experiments vs backtests vs simulation" },
      description: {
        zh: "回测 = 单次运行看结果；验证实验 = 多维度系统性检验防过拟合；模拟盘 = 用纸面资金持续跟踪。",
        en: "Backtest = one run to see results; validation experiment = systematic multi-dimensional checks against overfitting; simulation = continuous tracking with paper money.",
      },
      detail: {
        zh: "回测回答 '赚不赚钱'，验证实验回答 '是不是运气好'，模拟盘回答 '在真实交易环境下还赚不赚钱'。三者递进，缺一不可。",
        en: "A backtest answers “is it profitable”, a validation experiment answers “was it luck”, and simulation answers “does it stay profitable in a realistic trading environment”. All three build on each other; none can be skipped.",
      },
    },
    thresholds: {
      title: { zh: "验收阈值", en: "Acceptance thresholds" },
      description: {
        zh: "策略必须达到的最低标准。包括夏普比率、回撤、PBO 等指标。",
        en: "Minimum standards a strategy must meet, including Sharpe ratio, drawdown, PBO and more.",
      },
      detail: {
        zh: "默认阈值：OOS 夏普 ≥ 0.5、最大回撤 ≤ 25%、PBO ≤ 0.5、Deflated Sharpe ≥ 0。可根据策略类型调整。",
        en: "Defaults: OOS Sharpe ≥ 0.5, max drawdown ≤ 25%, PBO ≤ 0.5, Deflated Sharpe ≥ 0. Adjustable per strategy type.",
      },
    },
  },
  runs: {
    lineage: {
      title: { zh: "血缘追踪", en: "Lineage tracking" },
      description: {
        zh: "记录研究运行中每一步的输入输出和依赖关系。可以从最终结果追溯回原始数据。",
        en: "Records inputs, outputs and dependencies of every step in a research run; final results trace back to raw data.",
      },
      detail: {
        zh: "血缘保证可复现性：知道结果是怎么来的、用了什么数据、什么参数、什么代码版本。",
        en: "Lineage guarantees reproducibility: you know how results were produced, which data, which parameters, which code version.",
      },
    },
    freeze: {
      title: { zh: "冻结输入", en: "Frozen inputs" },
      description: {
        zh: "运行创建时锁定所有输入（数据版本、因子快照、策略版本、参数），运行期间不可修改。",
        en: "All inputs (data versions, factor snapshots, strategy versions, parameters) are locked at run creation and cannot change during the run.",
      },
      detail: {
        zh: "冻结是可复现的基础。即使数据源更新了，已冻结的运行仍使用旧版本数据重算。",
        en: "Freezing is the foundation of reproducibility. Even after the data source updates, a frozen run recomputes with the old versions.",
      },
    },
  },
  portfolio: {
    allocate: {
      title: { zh: "目标权重分配", en: "Target weight allocation" },
      description: {
        zh: "将信号转化为每个标的的目标持仓权重。支持等权、反波动率、ERC 等方法。",
        en: "Turns signals into target position weights per symbol. Supports equal-weight, inverse-volatility, ERC and more.",
      },
      detail: {
        zh: "分配考虑约束：单标的权重上限、行业集中度上限、最小现金缓冲、最大杠杆等。",
        en: "Allocation respects constraints: per-symbol weight caps, industry concentration caps, minimum cash buffer, maximum leverage and more.",
      },
    },
    sizing: {
      title: { zh: "离散交易求解", en: "Discrete trade solving" },
      description: {
        zh: "将连续权重转化为实际交易手数。A股最小交易单位是 100 股（1 手）。",
        en: "Converts continuous weights into actual trade lots. The minimum A-share trading unit is 100 shares (1 lot).",
      },
      detail: {
        zh: "离散化会产生跟踪误差。求解器在最小化跟踪误差的同时遵守手数取整和资金约束。",
        en: "Discretization introduces tracking error. The solver minimizes tracking error while respecting lot rounding and cash constraints.",
      },
    },
    feasibility: {
      title: { zh: "资金可行性", en: "Capital feasibility" },
      description: {
        zh: "检验在不同资金规模（10万/20万/50万）下策略是否可行。",
        en: "Checks whether the strategy is feasible at different capital sizes (100k/200k/500k).",
      },
      detail: {
        zh: "小资金可能因为手数取整导致无法建仓某些标的（特别是高价股），影响分散度。",
        en: "Small capital may fail to build positions in some symbols (especially high-priced ones) due to lot rounding, hurting diversification.",
      },
    },
  },
  simulation: {
    isolation: {
      title: { zh: "模拟盘与实盘隔离", en: "Simulation/live isolation" },
      description: {
        zh: "模拟盘使用独立的账户表、撮合引擎和审计。不连接真实券商，不影响实盘持仓。",
        en: "Simulation uses separate account tables, a separate matching engine and audit. It connects to no real broker and cannot affect live positions.",
      },
      detail: {
        zh: "模拟盘的订单只能由结构化目标仓位决策生成，不能手工下单。晋级实盘需要人工确认。",
        en: "Simulation orders can only be generated from structured target-position decisions, never placed manually. Promotion to live requires human confirmation.",
      },
    },
    decision: {
      title: { zh: "目标仓位决策", en: "Target position decision" },
      description: {
        zh: "提交策略计算的目标权重作为决策，由模拟撮合引擎生成订单并执行。",
        en: "Submits strategy-computed target weights as a decision; the simulated matching engine generates and executes orders.",
      },
      detail: {
        zh: "决策包含每个标的的目标数量。撮合引擎根据当前持仓计算需要买入/卖出多少。",
        en: "A decision includes target quantities per symbol; the matching engine computes how much to buy/sell from current positions.",
      },
    },
  },
  ai: {
    boundary: {
      title: { zh: "AI 能做什么", en: "What AI can do" },
      description: {
        zh: "AI 可以生成因子假设草案、策略修改建议和金融问答。但不能直接下单、修改持仓或执行回测。",
        en: "AI can draft factor hypotheses, suggest strategy changes and answer finance questions. It cannot place orders, modify positions, or execute backtests.",
      },
      detail: {
        zh: "所有 AI 输出都必须经过人工审批后才可消费。AI 的回答必须引用项目来源并声明不确定性。",
        en: "All AI output requires human approval before consumption. AI answers must cite project sources and declare uncertainty.",
      },
    },
    orchestration: {
      title: { zh: "研究流程编排", en: "Research workflow orchestration" },
      description: {
        zh: "描述你想要的研究目标，AI 将逐步引导你完成数据准备→因子选择→策略配置→实验验证的完整流程。",
        en: "Describe your research goal and AI guides you step by step through data prep → factor selection → strategy config → experimental validation.",
      },
      detail: {
        zh: "AI 会基于你的问题生成结构化的因子假设和策略草案，你可以审批后将其导入到对应的研究面板。",
        en: "AI generates structured factor hypotheses and strategy drafts from your questions; after approval you can import them into the corresponding research panels.",
      },
    },
  },
} satisfies Record<string, Record<string, InfoHintDefinition>>;
