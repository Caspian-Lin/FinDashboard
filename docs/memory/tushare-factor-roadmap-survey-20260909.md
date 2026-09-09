# tushare 因子清单调研:内部因子路线图(待用户拍板)

日期:2026-09-09。来源:tushare `factor_list` 接口文档页(https://tushare.pro/document/2?doc_id=486,公开可见,含全部 202 因子的算法逻辑;因子库数据本身是单独付费权限,个人 2000 元/年——**我们借清单与公式自研,不采购数值**)。

## 清单结构
202 因子 9 类:Alpha101 量价 31 / Growth 成长 15 / Liquidity 流动性 35 / Momentum 动量 20 / Quality 质量 59 / Reversal 反转 3 / Risk 风险 25 / Size 规模 3 / Value 价值 11。

## 关键洞察
- **202 因子 ≈ 60~70 个原子算子组合**:同族窗口变体极多(换手率 MA/STD/乖离率族约 30 个、return_N 族 6 个、std/high_low/beta 全是窗口变体)。先建算子库(ts_mean/std/rank/corr/cov/argmax/delta/delay/sign/rolling_ols/截面 rank/截面回归),因子=数据+公式组合。
- **数据依赖三层缺口**(对照 `releases.py` 的 DAILY_METRICS_FIELDS/FINANCIAL_INDICATORS_FIELDS):
  - A 零缺口(量价 ~75 个):bars(qfq 含 vol/amount)+ daily_metrics(turnover_rate/float_shares/total_market_cap)+ 指数 bars(#256/#341 已通)→ Momentum/Reversal/Risk/Size/Liquidity/Alpha101 全覆盖。VWAP≈amount/volume 近似。
  - B 扩 FINANCIAL_INDICATORS 白名单(fina_indicator 2000 积分已同步,只是字段未进白名单):ROA/周转率/流动速动比率/ICR/单季 QoQ 族等 → Growth+Quality 大部。
  - C 新数据集:三表 income/balance/cashflow(quality_composite AQR QMJ、fcf/ncf/ebitda_to_market、预收预付)+ dividend 明细(3 年股息率精确版;可用 dividend_yield_ttm 滚动均值先近似)。
- **平台现状对照**:v2 目录 signal-eligible alpha 仅 8 个(pb/earnings_yield/dividend_yield/roe/gpm/debt_to_assets/revenue_yoy/momentum)+ 波动率 3 档 + turnover_rate;量价、成长、质量绝大部分缺失。
- **载体缺口**:factor_series_build(#359-#361)目前只服务用户沙箱因子(research_sandbox,compute_series 在用户 git 仓库);**平台内置因子无 factor_series 构建入口**——方案核心工程项 = 新增「预置因子目录 + 平台内置 compute_series 实现」的构建通道,复用内容寻址/前缀不变性审计/覆盖检查。
- **口径边界**:基本面 PIT 锚点 announcement_date 已时点化(#212);截面因子(CrossSectionalRank/nl_size)须排除 benchmark-only(#380 教训);1320d 长窗口 2015 起 bars 约 2450 交易日、前 5.5 年无值(预热/覆盖检查须显式处理);复权用 qfq 与缓存键一致;**不承诺与 tushare 官方值逐值一致**(无权限对齐),验收=合成数据逐值单测+IC/分组 sanity。

## 分批方案(已给用户,确认后拆 issue)
批次 0 因子算子库+预置因子通道基座 → 批次 1 量价 ~75(Momentum/Reversal/Risk/Size/Liquidity)→ 批次 2 Alpha101 31 → 批次 3 财务白名单扩展+Growth/Quality 已有字段族 → 批次 4 三表同步+Value/Quality 补全 → 批次 5 因子质量评估(IC/分组)接入评分目录投影(#226)。

低价值缓做:log_price(变换非因子)、ma_20d(原料)、price_dist(整数关口,文献弱)、days_down_up(弱)。
