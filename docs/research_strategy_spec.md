# 无代码研究策略规格

`ResearchStrategySpec` 是研究/回测域的版本化配置契约。它让用户在不编写 Python
代码的情况下描述以下完整链路:

```text
UniverseSpec
→ FeatureGraph
→ SignalRules
→ PortfolioPolicy
→ RiskExitPolicy
→ ExecutionModel
→ ValidationPlan
```

规格保存、发布、替代或回滚都只操作 JSON 数据,不会启动回测、模拟盘或实盘策略。
解析结果固定为 `can_execute=false`。未来执行器消费目标仓位时仍须经过:

```text
Strategy / Research Plan
→ Target Position
→ Order Intent
→ Risk Manager
→ Order Manager
→ Broker Adapter
```

## 生命周期映射

| 业务阶段 | 规格/既有模块 | 输出 |
|---|---|---|
| 原始数据 | `ValidationPlan.dataset_release_ids` + #77 冻结发布 | 可复现历史输入 |
| 因子/特征 | `FeatureGraph` + #78 因子/风险/市场输入目录 | 有依赖顺序的特征节点 |
| 交易信号 | `SignalRules` | buy / sell / neutral、有效期、优先级和冲突策略 |
| 目标仓位 | `PortfolioPolicy` | 配权方法、敞口和组合约束引用 |
| 风险约束 | `RiskExitPolicy` | 止损、波动止损、止盈、持有期、回撤降险和冷却期 |
| 订单/执行 | `ExecutionModel` | 下一 Bar、费用、滑点、参与率、手数/涨跌停假设 |
| 持仓与盈亏 | 后续回测/模拟执行器 | 成交驱动的持仓、费用和绩效 |

`ExecutionModel` 只是研究假设,不包含 Broker、账户、订单 ID 或下单函数。

## 白名单

`GET /api/research/strategy-specs/registry` 返回前端可以生成控件的唯一能力目录:

- 因子源:价值、质量、增长、动量、波动率、流动性等已登记因子。
- 风险因子:beta、行业和规模暴露。
- 其他市场输入:基准收益、利率、期限利差、汇率和商品收益。
- 算子:`identity/sma/ema/return/volatility/zscore/cross_section_rank/`
  `winsorize/negate/subtract/ratio/weighted_sum`。
- 信号比较器:阈值、区间、交叉和横截面排名。
- 配权方法:等权、信号权重、逆波动率、ERC 和波动率缩放。

每个算子的输入数量和参数由强类型字段固定。没有通用表达式字段,也没有动态扩展
模块路径。未知因子、停用因子、未知依赖、循环依赖、非法算子和缺失数据发布均
fail closed。

API 和研究 runner 都调用 `compile_registered_strategy_spec()`,避免一端接受而
另一端产生不同解释。

## 候选池解释

`UniverseSpec` 支持市场/资产类别、显式代码、上市时间、平均成交额、价格、停牌、
退市、ST、事件风险、数据完整度、必需字段、排名、数量上限和缺失处理。

`explain_universe()` 为每个标的返回:

- `included`:是否纳入。
- `reasons`:如 `suspended`、`missing_required_field:market_cap`、
  `outside_selection_limit`。
- `rank`:最终入选排名。

候选池只能缩小冻结数据发布中的范围,不能添加发布外标的或触发数据下载。

## 风险退出规则

每种规则都要显式声明 `enabled` 和 `rationale`:

- `price_stop_loss`
- `volatility_stop`
- `take_profit`
- `max_holding_days`
- `portfolio_drawdown_derisk`
- `cooldown`

启用规则时必须提供对应阈值、窗口、天数或降险目标。关闭规则也必须保留理由,避免
“没有配置”和“经过决策后关闭”混淆。这里只定义研究约束,不替代实盘
`RiskManager` 或 Kill Switch。

## 版本与迁移

数据库按 `(strategy_id, version)` 追加保存。payload 和 checksum 创建后只读:

```text
draft → published → superseded
                    ↘ rollback 复制历史 payload 成为新的 published 版本
```

- 写入必须携带 `expected_version`,过期写入返回 HTTP 409。
- `supersede` 创建新 draft,不会覆盖旧版本。
- `rollback` 创建一个新版本并记录 `rollback_of_version`,不会删除历史。
- 发布在单一数据库事务内完成;中断后 rollback,不会留下半发布状态。
- 当前 schema 是 `v1`。已知 `v0` 草案仅把 `risk_policy` 固定重命名为
  `risk_exit_policy`;未知版本拒绝迁移。

## API

| 方法 | 路径 | 作用 |
|---|---|---|
| `GET` | `/api/research/strategy-specs/registry` | 策略、因子源和算子能力 |
| `GET` | `/api/research/strategy-specs/templates/{kind}` | #60-#64/均线兼容模板 |
| `POST` | `/api/research/strategy-specs/validate` | 与 runner 相同的解析校验 |
| `POST` | `/api/research/strategy-specs/drafts` | 创建 draft |
| `POST` | `/api/research/strategy-specs/{id}/supersede` | 创建替代 draft |
| `POST` | `/api/research/strategy-specs/{id}/publish` | 发布最新 draft |
| `POST` | `/api/research/strategy-specs/{id}/rollback` | 复制历史版本并发布 |
| `GET` | `/api/research/strategy-specs/{id}/history` | 只读版本历史 |
| `GET` | `/api/research/strategy-specs/{id}/diff` | 结构化版本差异 |

请求 schema 使用 `extra="forbid"`。`python_code/source_code/module_path/import/`
`expression/template/file_path` 等字段,以及 `eval/exec/__import__`、模板插值和
Python 文件路径都会返回 HTTP 422。

## 三个配置示例

完整且可校验的示例由统一注册表生成,不需要复制易漂移的 JSON:

### 多因子选股

```http
GET /api/research/strategy-specs/templates/multi_factor
    ?strategy_id=my_multi_factor
    &dataset_release_ids=cn-equity-2024-v1
```

模板把 `pb + momentum + volatility_20d` 组成复合得分,用排名信号选择标的,
再转为等权目标仓位并应用集中度、流动性、换手和组合回撤约束。

### 多资产 ETF 轮动

```http
GET /api/research/strategy-specs/templates/etf_rotation
    ?strategy_id=my_etf_rotation
    &dataset_release_ids=multi-asset-etf-2024-v1
```

模板使用 200 日绝对趋势和 3/6/12 月相对动量,以逆波动率生成目标权重,同时限制
单标的与单资产大类暴露。

### ETF 均值回归

```http
GET /api/research/strategy-specs/templates/mean_reversion
    ?strategy_id=my_mean_reversion
    &dataset_release_ids=liquid-etf-2024-v1
```

模板使用 20 日 Z 分数和长期趋势状态,配置最长持有 10 日与退出后 5 日冷却期。
信号在 T 日收盘形成,成交假设固定为下一 Bar,禁止同 Bar 成交。

另外还有 `convertible_double_low`、`futures_tsmom` 和 `ma_cross` 模板。模板只是可编辑
起点,不是收益保证;任何版本仍须完成样本外、稳健性、行情回放和模拟交易验证。

## 安全边界

- 不接受 Python 源码、模块路径、可执行表达式或任意文件加载。
- 保存/发布/回滚不启动回测,不自动启动模拟盘或实盘。
- 不导入 Broker、OrderManager、PositionManager 或实盘 RiskManager。
- 不修改账户、订单、成交或持仓。
- LLM 可以解释字段、提出因子和配置建议,但只能提交同一白名单规格,不能绕过验证。
