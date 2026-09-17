# 预置因子对账:Tushare 202 因子清单 × 平台预置因子

> 来源:Tushare `factor_list` 文档页(https://tushare.pro/document/2?doc_id=486,公开,抓取于 2026-09-11)。
> 平台侧:`finboard_backtest/factors/predefined/registry.py` 共 **260** 个 `p_` 预置因子(#398 通道,批次 #399/#400/#401/#402/#415/#429 交付)。

## 结论摘要

| 口径 | 数量 |
|---|---|
| Tushare 清单 | 202 |
| ✅ 严格实现(同名/同式) | 156 |
| 🟡 近似/口径变体(已实现,窗口或构造有差) | 36 |
| **清单覆盖合计(✅+🟡)** | **192 / 202** |
| ❌ 未实现 | 10(P3 冷门不建议 10) |

说明:

- **窗口口径约定**:平台统一用 20/60/120/250(≈自然月/季/半年/年取整),Tushare 用 21/42/63/126/252(精确月×21)。🟡 中大量条目仅差在此,语义同族。
- **注册表 260 ≠ 清单覆盖 192**:注册表另含 ~68 个平台自研因子(QMJ 四支柱、resid_momentum、corr_market、amihud、turnover_z、单季财务族等),不在 Tushare 清单内。
- 🟡/❌ 的逐项原因见下表;❌ 的 [P1]/[P2]/[P3] 为补齐优先级评估(见文末)。
- 预置因子消费路径:MCP `finboard_factor_series_build`(kind=predefined_factor)、research_run 引用门;目录可见性见 #427。

各分类覆盖:

| 类别 | ✅ | 🟡 | ❌ | 覆盖率 |
|---|---|---|---|---|
| Alpha101(31) | 31 | 0 | 0 | 100% |
| Growth(15) | 12 | 3 | 0 | 100% |
| Liquidity(35) | 23 | 12 | 0 | 100% |
| Momentum(20) | 16 | 2 | 2 | 90% |
| Quality(59) | 48 | 8 | 3 | 94% |
| Reversal(3) | 2 | 0 | 1 | 66% |
| Risk(25) | 15 | 7 | 3 | 88% |
| Size(3) | 1 | 2 | 0 | 100% |
| Value(11) | 8 | 2 | 1 | 90% |

## Alpha101（31 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `alpha101_1` | ✅ | `alpha101_1` | WorldQuant Alpha101 同号实现 |
| 2 | `alpha101_2` | ✅ | `alpha101_2` | WorldQuant Alpha101 同号实现 |
| 3 | `alpha101_3` | ✅ | `alpha101_3` | WorldQuant Alpha101 同号实现 |
| 4 | `alpha101_4` | ✅ | `alpha101_4` | WorldQuant Alpha101 同号实现 |
| 5 | `alpha101_5` | ✅ | `alpha101_5` | WorldQuant Alpha101 同号实现 |
| 6 | `alpha101_6` | ✅ | `alpha101_6` | WorldQuant Alpha101 同号实现 |
| 7 | `alpha101_7` | ✅ | `alpha101_7` | WorldQuant Alpha101 同号实现 |
| 8 | `alpha101_8` | ✅ | `alpha101_8` | WorldQuant Alpha101 同号实现 |
| 9 | `alpha101_9` | ✅ | `alpha101_9` | WorldQuant Alpha101 同号实现 |
| 10 | `alpha101_10` | ✅ | `alpha101_10` | WorldQuant Alpha101 同号实现 |
| 11 | `alpha101_11` | ✅ | `alpha101_11` | WorldQuant Alpha101 同号实现 |
| 12 | `alpha101_12` | ✅ | `alpha101_12` | WorldQuant Alpha101 同号实现 |
| 13 | `alpha101_13` | ✅ | `alpha101_13` | WorldQuant Alpha101 同号实现 |
| 14 | `alpha101_14` | ✅ | `alpha101_14` | WorldQuant Alpha101 同号实现 |
| 15 | `alpha101_15` | ✅ | `alpha101_15` | WorldQuant Alpha101 同号实现 |
| 16 | `alpha101_16` | ✅ | `alpha101_16` | WorldQuant Alpha101 同号实现 |
| 17 | `alpha101_17` | ✅ | `alpha101_17` | WorldQuant Alpha101 同号实现 |
| 18 | `alpha101_18` | ✅ | `alpha101_18` | WorldQuant Alpha101 同号实现 |
| 19 | `alpha101_19` | ✅ | `alpha101_19` | WorldQuant Alpha101 同号实现 |
| 20 | `alpha101_20` | ✅ | `alpha101_20` | WorldQuant Alpha101 同号实现 |
| 21 | `alpha101_22` | ✅ | `alpha101_22` | WorldQuant Alpha101 同号实现 |
| 22 | `alpha101_23` | ✅ | `alpha101_23` | WorldQuant Alpha101 同号实现 |
| 23 | `alpha101_25` | ✅ | `alpha101_25` | WorldQuant Alpha101 同号实现 |
| 24 | `alpha101_33` | ✅ | `alpha101_33` | WorldQuant Alpha101 同号实现 |
| 25 | `alpha101_34` | ✅ | `alpha101_34` | WorldQuant Alpha101 同号实现 |
| 26 | `alpha101_41` | ✅ | `alpha101_41` | WorldQuant Alpha101 同号实现 |
| 27 | `alpha101_52` | ✅ | `alpha101_52` | WorldQuant Alpha101 同号实现 |
| 28 | `alpha101_53` | ✅ | `alpha101_53` | WorldQuant Alpha101 同号实现 |
| 29 | `alpha101_54` | ✅ | `alpha101_54` | WorldQuant Alpha101 同号实现 |
| 30 | `alpha101_57` | ✅ | `alpha101_57` | WorldQuant Alpha101 同号实现 |
| 31 | `alpha101_101` | ✅ | `alpha101_101` | WorldQuant Alpha101 同号实现 |

小计:✅ 31 · 🟡 0 · ❌ 0


## Growth（15 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `peg_252d` | ✅ | `peg_252d` | 批次 6(#429) 实现 |
| 2 | `np_ttm_qoq` | 🟡 | `fin_netprofit_qoq` | 平台为单季环比,Tushare 为 TTM 环比 |
| 3 | `yoy_net_profit` | ✅ | `fin_netprofit_yoy` | 上游同名字段 |
| 4 | `yoy_ocf` | ✅ | `fin_ocf_yoy` |  |
| 5 | `sa` | 🟡 | `fin_revenue_yoy_accel` | 营收增速加速度,未做每股化 |
| 6 | `gross_margin_qoq` | 🟡 | `fin_gross_margin_q` | 平台为单季毛利率水平,非 TTM 环比 |
| 7 | `pa` | ✅ | `pa` | 批次 6(#429) 实现 |
| 8 | `yoy_roa` | ✅ | `yoy_roa` | 批次 6(#429) 实现 |
| 9 | `yoy_net_asset` | ✅ | `yoy_net_asset` | 批次 6(#429) 实现 |
| 10 | `yoy_revenue` | ✅ | `fin_revenue_yoy` |  |
| 11 | `yoy_roe` | ✅ | `yoy_roe` | 批次 6(#429) 实现 |
| 12 | `yoy_total_asset` | ✅ | `yoy_total_asset` | 批次 6(#429) 实现 |
| 13 | `eaa` | ✅ | `eaa` | 批次 6(#429) 实现 |
| 14 | `eap` | ✅ | `eap` | 批次 6(#429) 实现 |
| 15 | `asset_growth_qoq` | ✅ | `asset_growth_qoq` | 批次 6(#429) 实现 |

小计:✅ 12 · 🟡 3 · ❌ 0


## Liquidity（35 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `avg_turnover_5d` | ✅ | `turnover_ma_5d` |  |
| 2 | `avg_turnover_10d` | ✅ | `turnover_ma_10d` |  |
| 3 | `avg_turnover_20d` | ✅ | `turnover_ma_20d` |  |
| 4 | `amount_ma_20d` | ✅ | `amount_ma_20d` |  |
| 5 | `turnover_ma_20d` | 🟡 | `turnover_ma_20d` | Tushare 版本结果取负(原始因子设计),符号约定相反 |
| 6 | `sum_abs_rtn_amount_20d` | ✅ | `sum_abs_rtn_amount_20d` | 批次 6(#429) 实现 |
| 7 | `turnover_ma_20d_120d` | 🟡 | `turnover_ratio_20_60d` | 短/长换手比;窗口 20/120 vs 平台 20/60 |
| 8 | `avg_turnover_21d` | 🟡 | `turnover_ma_20d` | 21 vs 20(月度窗口口径) |
| 9 | `std_turnover_21d` | 🟡 | `turnover_std_20d` | 21 vs 20 |
| 10 | `bias_std_turn_21d_252d` | ✅ | `bias_std_turn_21d_252d` | 批次 6(#429) 实现 |
| 11 | `bias_turn_21d_252d` | ✅ | `bias_turn_21d_252d` | 批次 6(#429) 实现 |
| 12 | `bias_std_turn_21d_504d` | ✅ | `bias_std_turn_21d_504d` | 批次 6(#429) 实现 |
| 13 | `bias_turn_21d_504d` | ✅ | `bias_turn_21d_504d` | 批次 6(#429) 实现 |
| 14 | `std_turnover_42d` | 🟡 | `turnover_std_60d` | 42(2月) vs 平台 60 档 |
| 15 | `avg_turnover_42d` | 🟡 | `turnover_ma_60d` | 42 vs 60 |
| 16 | `bias_turn_42d_252d` | ✅ | `bias_turn_42d_252d` | 批次 6(#429) 实现 |
| 17 | `bias_std_turn_42d_252d` | ✅ | `bias_std_turn_42d_252d` | 批次 6(#429) 实现 |
| 18 | `bias_std_turn_42d_504d` | ✅ | `bias_std_turn_42d_504d` | 批次 6(#429) 实现 |
| 19 | `bias_turn_42d_504d` | ✅ | `bias_turn_42d_504d` | 批次 6(#429) 实现 |
| 20 | `std_turnover_63d` | 🟡 | `turnover_std_60d` | 63 vs 60 |
| 21 | `avg_turnover_63d` | 🟡 | `turnover_ma_60d` | 63 vs 60 |
| 22 | `bias_turn_63d_252d` | ✅ | `bias_turn_63d_252d` | 批次 6(#429) 实现 |
| 23 | `bias_std_turn_63d_252d` | ✅ | `bias_std_turn_63d_252d` | 批次 6(#429) 实现 |
| 24 | `bias_std_turn_63d_504d` | ✅ | `bias_std_turn_63d_504d` | 批次 6(#429) 实现 |
| 25 | `bias_turn_63d_504d` | ✅ | `bias_turn_63d_504d` | 批次 6(#429) 实现 |
| 26 | `avg_turnover_126d` | 🟡 | `turnover_ma_120d` | 126 vs 120 |
| 27 | `std_turnover_126d` | 🟡 | `turnover_std_120d` | 126 vs 120 |
| 28 | `bias_turn_126d_252d` | ✅ | `bias_turn_126d_252d` | 批次 6(#429) 实现 |
| 29 | `bias_std_turn_126d_252d` | ✅ | `bias_std_turn_126d_252d` | 批次 6(#429) 实现 |
| 30 | `bias_turn_126d_504d` | ✅ | `bias_turn_126d_504d` | 批次 6(#429) 实现 |
| 31 | `bias_std_turn_126d_504d` | ✅ | `bias_std_turn_126d_504d` | 批次 6(#429) 实现 |
| 32 | `std_turnover_252d` | 🟡 | `turnover_std_250d` | 252 vs 250(年窗口径) |
| 33 | `avg_turnover_252d` | 🟡 | `turnover_ma_250d` | 252 vs 250 |
| 34 | `volume_alpha_300d_000001` | ✅ | `volume_alpha_300d_000001` | 批次 6(#429) 实现 |
| 35 | `volume_alpha_300d_000300` | ✅ | `volume_alpha_300d_000300` | 批次 6(#429) 实现 |

小计:✅ 23 · 🟡 12 · ❌ 0


## Momentum（20 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `return_5d` | ✅ | `return_5d` | 批次 6(#429) 实现 |
| 2 | `ma_20d` | ❌ | — | [P3] 平凡均线;调研排除项 |
| 3 | `return_21d` | ✅ | `return_21d` |  |
| 4 | `return_42d` | ✅ | `return_42d` | 批次 6(#429) 实现 |
| 5 | `price_position_ir_60d` | ✅ | `price_position_ir_60d` | 批次 6(#429) 实现 |
| 6 | `return_63d` | ✅ | `return_63d` |  |
| 7 | `alpha_125d_000300` | 🟡 | `reg_alpha_120d` | 125 vs 120 |
| 8 | `return_126d` | ✅ | `return_126d` |  |
| 9 | `alpha_250d_000300` | ✅ | `reg_alpha_250d` |  |
| 10 | `return_252d` | ✅ | `return_252d` |  |
| 11 | `alpha_500d_000300` | ✅ | `alpha_500d_000300` | 批次 6(#429) 实现 |
| 12 | `alpha_528d_000001` | ✅ | `alpha_528d_000001` | 批次 6(#429) 实现 |
| 13 | `alpha_792d_000001` | ✅ | `alpha_792d_000001` | 批次 6(#429) 实现 |
| 14 | `alpha_1000d_000300` | ✅ | `alpha_1000d_000300` | 批次 6(#429) 实现 |
| 15 | `alpha_1320d_000001` | ✅ | `alpha_1320d_000001` | 批次 6(#429) 实现 |
| 16 | `rsrs` | 🟡 | `rsrs_beta_600d / rsrs_r2_600d` | 平台为 600 日斜率 + R² 加权标准版 |
| 17 | `MACD` | ✅ | `macd_hist_norm` | 平台为柱值/收盘价归一化 |
| 18 | `dif` | ✅ | `dif` | 批次 6(#429) 实现 |
| 19 | `dea` | ✅ | `dea` | 批次 6(#429) 实现 |
| 20 | `days_down_up` | ❌ | — | [P3] 连续涨跌天数;调研排除项 |

小计:✅ 16 · 🟡 2 · ❌ 2


## Quality（59 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `roe_ttm_lag63d` | 🟡 | `fin_roe` | Tushare 为 lag63d 变体 |
| 2 | `debt_asset_ratio` | ✅ | `fin_debt_to_assets` |  |
| 3 | `eps_ttm` | ✅ | `eps_ttm` | 批次 6(#429) 实现 |
| 4 | `financial_leverage` | ✅ | `fin_equity_multiplier` |  |
| 5 | `gpm_ttm` | ✅ | `fin_gross_margin` |  |
| 6 | `icr` | ✅ | `fin_interest_coverage` |  |
| 7 | `income_tax_yoy` | ✅ | `income_tax_yoy` | 批次 6(#429) 实现 |
| 8 | `np_to_inventory_yoy` | ✅ | `np_to_inventory_yoy` | 批次 6(#429) 实现 |
| 9 | `npm_q` | ✅ | `fin_net_margin_q` |  |
| 10 | `quality_composite` | 🟡 | `qmj_profitability` | AQR 6 项 vs 平台 QMJ 盈利支柱(ROE/ROA/毛利率/OCF 收入比) |
| 11 | `ar_ap_to_revenue` | 🟡 | `qlt_advance_receipts_ratio` | (预收-预付)/营收 vs 平台预收(含合同负债)/营收 |
| 12 | `asset_turnover` | ✅ | `fin_total_assets_turnover` |  |
| 13 | `delta_current_ratio` | ✅ | `delta_current_ratio` | 批次 6(#429) 实现 |
| 14 | `delta_de` | ✅ | `delta_de` | 批次 6(#429) 实现 |
| 15 | `gpm_q` | ✅ | `fin_gross_margin_q` |  |
| 16 | `cash_profit_ratio` | 🟡 | `qlt_ocf_to_profit` | (OCF-NP)/NP vs 平台 OCF/NP |
| 17 | `delta_opm` | ✅ | `delta_opm` | 批次 6(#429) 实现 |
| 18 | `eps_q` | ✅ | `eps_q` | 批次 6(#429) 实现 |
| 19 | `eps_y` | ✅ | `eps_y` | 批次 6(#429) 实现 |
| 20 | `delta_npm` | ✅ | `delta_npm` | 批次 6(#429) 实现 |
| 21 | `market_value_leverage` | ✅ | `market_value_leverage` | 批次 6(#429) 实现 |
| 22 | `npm_y` | 🟡 | `fin_net_margin` | 年度 vs 平台 TTM/最新公告口径 |
| 23 | `opm_y` | ✅ | `opm_y` | 批次 6(#429) 实现 |
| 24 | `quick_ratio` | ✅ | `fin_quick_ratio` |  |
| 25 | `delta_gpm` | ✅ | `delta_gpm` | 批次 6(#429) 实现 |
| 26 | `gpm_y` | 🟡 | `fin_gross_margin` | 年度口径 |
| 27 | `np_to_total_expenses_yoy` | ✅ | `np_to_total_expenses_yoy` | 批次 6(#429) 实现 |
| 28 | `np_to_deferred_tax_yoy` | ❌ | — | [P3] 递延所得税资产列覆盖存疑 |
| 29 | `roe_y` | 🟡 | `fin_roe` | 年度口径 |
| 30 | `roa_q` | ✅ | `fin_roa_q` |  |
| 31 | `gpm_qoq` | ✅ | `gpm_qoq` | 批次 6(#429) 实现 |
| 32 | `delta_inventory_turnover` | ✅ | `delta_inventory_turnover` | 批次 6(#429) 实现 |
| 33 | `delta_roa` | ✅ | `delta_roa` | 批次 6(#429) 实现 |
| 34 | `fixed_asset_turnover` | ✅ | `fin_fixed_assets_turnover` |  |
| 35 | `npm_ttm` | ✅ | `fin_net_margin` |  |
| 36 | `opm_ttm` | ✅ | `opm_ttm` | 批次 6(#429) 实现 |
| 37 | `receivable_turnover` | ✅ | `fin_receivables_turnover` |  |
| 38 | `cfcr` | ✅ | `cfcr` | 批次 6(#429) 实现 |
| 39 | `delta_cash_ratio` | ✅ | `delta_cash_ratio` | 批次 6(#429) 实现 |
| 40 | `lra_yoy` | ❌ | — | [P3] 长期应收列覆盖存疑 |
| 41 | `np_to_fixed_assets_yoy` | ✅ | `np_to_fixed_assets_yoy` | 批次 6(#429) 实现 |
| 42 | `np_to_salary_yoy` | ✅ | `np_to_salary_yoy` | 批次 6(#429) 实现 |
| 43 | `npm_q_qoq` | ✅ | `npm_q_qoq` | 批次 6(#429) 实现 |
| 44 | `npm_tsh` | ❌ | — | [P3] 归母净利/平均总股本 |
| 45 | `npm_ttm_qoq` | ✅ | `npm_ttm_qoq` | 批次 6(#429) 实现 |
| 46 | `cash_ratio` | ✅ | `cash_ratio` | 批次 6(#429) 实现 |
| 47 | `current_ratio` | ✅ | `fin_current_ratio` |  |
| 48 | `de` | ✅ | `fin_debt_to_equity` |  |
| 49 | `delta_asset_turnover` | ✅ | `delta_asset_turnover` | 批次 6(#429) 实现 |
| 50 | `delta_quick_ratio` | ✅ | `delta_quick_ratio` | 批次 6(#429) 实现 |
| 51 | `delta_roe` | ✅ | `delta_roe` | 批次 6(#429) 实现 |
| 52 | `inventory_turnover` | ✅ | `fin_inventory_turnover` |  |
| 53 | `roa_ttm` | ✅ | `fin_roa` |  |
| 54 | `roe_ttm` | ✅ | `fin_roe` |  |
| 55 | `tax_surcharge_yoy` | ✅ | `tax_surcharge_yoy` | 批次 6(#429) 实现 |
| 56 | `equity_turnover` | ✅ | `equity_turnover` | 批次 6(#429) 实现 |
| 57 | `expenses_to_equity_yoy` | ✅ | `expenses_to_equity_yoy` | 批次 6(#429) 实现 |
| 58 | `roa_y` | 🟡 | `fin_roa` | 年度口径 |
| 59 | `opt_tpro` | ✅ | `opt_tpro` | 批次 6(#429) 实现 |

小计:✅ 48 · 🟡 8 · ❌ 3


## Reversal（3 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `small_cap_reversal_21d` | ✅ | `small_cap_reversal_21d` | 批次 6(#429) 实现 |
| 2 | `rsi` | ✅ | `rsi_14d` |  |
| 3 | `price_dist` | ❌ | — | [P3] 整数关口距离;调研排除项 |

小计:✅ 2 · 🟡 0 · ❌ 1


## Risk（25 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `days_beyond_upper_lower_21d` | ❌ | — | [P3] 异常波动统计,冷门 |
| 2 | `return_std_21d` | 🟡 | `vol_20d` | 21 vs 20 |
| 3 | `high_low_21d` | ✅ | `high_low_21d` | 批次 6(#429) 实现 |
| 4 | `high_low_42d` | ✅ | `high_low_42d` | 批次 6(#429) 实现 |
| 5 | `return_std_42d` | ✅ | `return_std_42d` | 批次 6(#429) 实现 |
| 6 | `sharpe_60d` | ✅ | `sharpe_60d` |  |
| 7 | `beta_60d_000300` | ✅ | `beta_60d` |  |
| 8 | `return_std_63d` | 🟡 | `vol_60d` | 63 vs 60 |
| 9 | `high_low_63d` | ✅ | `high_low_63d` | 批次 6(#429) 实现 |
| 10 | `volume_beta_120d_000300` | ✅ | `volume_beta_120d_000300` | 批次 6(#429) 实现 |
| 11 | `beta_125d_000300` | 🟡 | `beta_120d` | 125 vs 120 |
| 12 | `high_low_126d` | ✅ | `high_low_126d` | 批次 6(#429) 实现 |
| 13 | `return_std_126d` | 🟡 | `vol_120d` | 126 vs 120 |
| 14 | `beta_250d_000300` | ✅ | `beta_250d` |  |
| 15 | `return_std_252d` | 🟡 | `vol_250d` | 252 vs 250 |
| 16 | `high_low_252d` | ✅ | `high_low_252d` | 批次 6(#429) 实现 |
| 17 | `beta_500d_000300` | ✅ | `beta_500d_000300` | 批次 6(#429) 实现 |
| 18 | `sharpe_750d` | 🟡 | `sharpe_1320d` | 平台有 60/120/250/1320 档,无 750 |
| 19 | `adjusted_sharpe_750d` | ❌ | — | [P3] mean/std^4,冷门 |
| 20 | `beta_1000d_000300` | ✅ | `beta_1000d_000300` | 批次 6(#429) 实现 |
| 21 | `sigma_1320d_000001` | ✅ | `sigma_1320d_000001` | 批次 6(#429) 实现 |
| 22 | `beta_1320d_000001` | 🟡 | `beta_1320d` | 平台同名但基准为 000300 |
| 23 | `beta_consistency_1320d_000300` | ✅ | `beta_consistency_1320d_000300` | 批次 6(#429) 实现 |
| 24 | `sigma_1320d_000300` | ✅ | `sigma_1320d_000300` | 批次 6(#429) 实现 |
| 25 | `log_price` | ❌ | — | [P3] log(收盘价);调研排除项 |

小计:✅ 15 · 🟡 7 · ❌ 3


## Size（3 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `float_size` | 🟡 | `log_circulating_market_cap` | Tushare 为负对数,符号约定相反 |
| 2 | `nl_size` | ✅ | `nl_size` | 批次 6(#429) 实现 |
| 3 | `size` | 🟡 | `log_total_market_cap` | 负对数,符号约定相反 |

小计:✅ 1 · 🟡 2 · ❌ 0


## Value（11 个）

| # | Tushare 因子 | 状态 | 平台因子 | 说明 |
|---|---|---|---|---|
| 1 | `dividend_yield_3y_avg` | 🟡 | `val_dividend_yield` | Tushare 为 3 年平均,平台为 TTM |
| 2 | `pegh5` | ❌ | — | [P3] 5 年 EPS 复合增长 PEG |
| 3 | `etp5` | ✅ | `etp5` | 批次 6(#429) 实现 |
| 4 | `ncf_to_market` | ✅ | `ncf_to_market` | 批次 6(#429) 实现 |
| 5 | `fcf_to_market` | ✅ | `val_fcf_to_market` |  |
| 6 | `ebitda_to_market` | ✅ | `val_ebitda_to_market` |  |
| 7 | `earnings_to_price` | ✅ | `val_earnings_to_price` |  |
| 8 | `book_to_market` | 🟡 | `val_bm` | Tushare 含递延税调整,平台为精确归母权益/总市值 |
| 9 | `earnings_cut_to_market` | ✅ | `earnings_cut_to_market` | 批次 6(#429) 实现 |
| 10 | `ocf_to_market` | ✅ | `val_ocf_to_market` |  |
| 11 | `sales_to_market` | ✅ | `val_sales_to_price` |  |

小计:✅ 8 · 🟡 2 · ❌ 1

## 补齐评估(#426 第 3 项交付)

批次 6(#429,2026-09)已补齐原评估的 P1/P2 共 86 项——P1 量价 28(#431)+ P1 财务 29(#432)+ P2 市场回归/规模非线性 14(#433)+ P2 三表/其余 15(#434),全部经 #415 质量评估闭环入册。
剩余 ❌ 共 10 项:

### P3 冷门或上游存疑,不建议近期补(10 项)

`log_price`、`ma_20d`、`price_dist`、`days_down_up` 为调研即列为排除候选的平凡因子;其余为上游列覆盖存疑(递延所得税/长期应收/平均总股本)或冷门统计量(异常波动天数/调整夏普):
`adjusted_sharpe_750d`、`days_beyond_upper_lower_21d`、`days_down_up`、`log_price`、`lra_yoy`、`ma_20d`、`np_to_deferred_tax_yoy`、`npm_tsh`、`pegh5`、`price_dist`


## 维护
- 新增预置因子时同步更新本表(状态列与注册表一致;生成器按注册表自动翻转 ❌→✅);
- 表由 `scripts/gen_factor_mapping.py` 生成(清单 JSON 快照 `scripts/tushare_factor_list_20260911.json` 抓取自源页),手工编辑请改生成器避免漂移。
