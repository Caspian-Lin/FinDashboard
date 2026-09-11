# ruff: noqa: E702, RUF001, SIM115
"""生成 docs/research/predefined-factor-mapping.md(数据源 scripts/tushare_factor_list_20260911.json,抓取自 factor_list 文档页):tushare 202 因子逐项对账。

MAPPING: tushare 因子名 -> (status, 平台因子, 说明)
  status: "covered" ✅实现 / "approx" 🟡近似或口径变体 / "missing" ❌未实现
"""
import json
import sys
from collections import Counter
from pathlib import Path

C = "covered"; A = "approx"; MISS = "missing"
BADGE = {C: "✅", A: "🟡", MISS: "❌"}
TIER = {"P1": "P1 数据上游已有,低成本可补", "P2": "P2 需组合构造/长窗口/新列组合", "P3": "P3 冷门或上游覆盖存疑,不建议近期补"}

# 注册表驱动的自动翻转:清单名已入注册表的 ❌ 翻 ✅(批次 6/#429 起生效,
# 此后新增批次只需改 FLIP_NOTE 与注册表,映射表本体不再逐条手改)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packages" / "finboard-backtest" / "src"))
from finboard_backtest.factors.predefined import PREDEFINED_FACTORS  # noqa: E402

FLIP_NOTE = "批次 6(#429) 实现"

MAP: dict[str, tuple[str, str, str, str]] = {}  # name -> (status, our, note, tier_if_missing)
def m(name: str, status: str, our: str = "", note: str = "", tier: str = "") -> None:
    MAP[name] = (status, our, note, tier)

# ---------------- Alpha101 (31) ----------------
for i in [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,22,23,25,33,34,41,52,53,54,57,101]:
    m(f"alpha101_{i}", C, f"alpha101_{i}", "WorldQuant Alpha101 同号实现")

# ---------------- Growth (15) ----------------
m("peg_252d", MISS, "", "需 PE×EPS 增速组合", "P2")
m("np_ttm_qoq", A, "fin_netprofit_qoq", "平台为单季环比,Tushare 为 TTM 环比")
m("yoy_net_profit", C, "fin_netprofit_yoy", "上游同名字段")
m("yoy_ocf", C, "fin_ocf_yoy", "")
m("sa", A, "fin_revenue_yoy_accel", "营收增速加速度,未做每股化")
m("gross_margin_qoq", A, "fin_gross_margin_q", "平台为单季毛利率水平,非 TTM 环比")
m("pa", MISS, "", "ROA 增速加速度", "P1")
m("yoy_roa", MISS, "", "ROA 同比;上游有 fin_roa", "P1")
m("yoy_net_asset", MISS, "", "净资产同比;balance_sheets 已同步", "P1")
m("yoy_revenue", C, "fin_revenue_yoy", "")
m("yoy_roe", MISS, "", "ROE 同比;上游有 fin_roe", "P1")
m("yoy_total_asset", MISS, "", "总资产同比", "P1")
m("eaa", MISS, "", "EPS 增速加速度;上游有 eps", "P1")
m("eap", MISS, "", "EPS 增量/价格;需 bars 组合", "P1")
m("asset_growth_qoq", MISS, "", "总资产环比", "P1")

# ---------------- Liquidity (35) ----------------
m("avg_turnover_5d", C, "turnover_ma_5d", "")
m("avg_turnover_10d", C, "turnover_ma_10d", "")
m("avg_turnover_20d", C, "turnover_ma_20d", "")
m("amount_ma_20d", C, "amount_ma_20d", "")
m("turnover_ma_20d", A, "turnover_ma_20d", "Tushare 版本结果取负(原始因子设计),符号约定相反")
m("sum_abs_rtn_amount_20d", MISS, "", "|收益|和/成交额和;bars+amount 有", "P1")
m("turnover_ma_20d_120d", A, "turnover_ratio_20_60d", "短/长换手比;窗口 20/120 vs 平台 20/60")
m("avg_turnover_21d", A, "turnover_ma_20d", "21 vs 20(月度窗口口径)")
m("std_turnover_21d", A, "turnover_std_20d", "21 vs 20")
m("bias_std_turn_21d_252d", MISS, "", "短窗std/长窗std;成分算子已有", "P1")
m("bias_turn_21d_252d", MISS, "", "短/长均值乖离;ratio 族可参数化", "P1")
m("bias_std_turn_21d_504d", MISS, "", "同上,504 窗", "P1")
m("bias_turn_21d_504d", MISS, "", "同上,504 窗", "P1")
m("std_turnover_42d", A, "turnover_std_60d", "42(2月) vs 平台 60 档")
m("avg_turnover_42d", A, "turnover_ma_60d", "42 vs 60")
m("bias_turn_42d_252d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_42d_252d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_42d_504d", MISS, "", "长短乖离族", "P1")
m("bias_turn_42d_504d", MISS, "", "长短乖离族", "P1")
m("std_turnover_63d", A, "turnover_std_60d", "63 vs 60")
m("avg_turnover_63d", A, "turnover_ma_60d", "63 vs 60")
m("bias_turn_63d_252d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_63d_252d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_63d_504d", MISS, "", "长短乖离族", "P1")
m("bias_turn_63d_504d", MISS, "", "长短乖离族", "P1")
m("avg_turnover_126d", A, "turnover_ma_120d", "126 vs 120")
m("std_turnover_126d", A, "turnover_std_120d", "126 vs 120")
m("bias_turn_126d_252d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_126d_252d", MISS, "", "长短乖离族", "P1")
m("bias_turn_126d_504d", MISS, "", "长短乖离族", "P1")
m("bias_std_turn_126d_504d", MISS, "", "长短乖离族", "P1")
m("std_turnover_252d", A, "turnover_std_250d", "252 vs 250(年窗口径)")
m("avg_turnover_252d", A, "turnover_ma_250d", "252 vs 250")
m("volume_alpha_300d_000001", MISS, "", "成交量 Alpha;需指数成交量,指数 bars 已同步", "P2")
m("volume_alpha_300d_000300", MISS, "", "成交量 Alpha;需指数成交量", "P2")

# ---------------- Momentum (20) ----------------
m("return_5d", MISS, "", "窗口变体;return_N 算子已有", "P1")
m("ma_20d", MISS, "", "平凡均线;调研排除项", "P3")
m("return_21d", C, "return_21d", "")
m("return_42d", MISS, "", "窗口变体", "P1")
m("price_position_ir_60d", MISS, "", "(C-O)/(H-L) 的 60 日 IR;bars 有", "P1")
m("return_63d", C, "return_63d", "")
m("alpha_125d_000300", A, "reg_alpha_120d", "125 vs 120")
m("return_126d", C, "return_126d", "")
m("alpha_250d_000300", C, "reg_alpha_250d", "")
m("return_252d", C, "return_252d", "")
m("alpha_500d_000300", MISS, "", "长窗 500;rolling_ols 算子有", "P2")
m("alpha_528d_000001", MISS, "", "长窗+上证基准;平台基准固定 000300", "P2")
m("alpha_792d_000001", MISS, "", "同上", "P2")
m("alpha_1000d_000300", MISS, "", "长窗 1000", "P2")
m("alpha_1320d_000001", MISS, "", "长窗 1320+上证基准", "P2")
m("rsrs", A, "rsrs_beta_600d / rsrs_r2_600d", "平台为 600 日斜率 + R² 加权标准版")
m("MACD", C, "macd_hist_norm", "平台为柱值/收盘价归一化")
m("dif", MISS, "", "MACD 分量;EMA 算子已有", "P1")
m("dea", MISS, "", "MACD 分量", "P1")
m("days_down_up", MISS, "", "连续涨跌天数;调研排除项", "P3")

# ---------------- Quality (59) ----------------
m("roe_ttm_lag63d", A, "fin_roe", "Tushare 为 lag63d 变体")
m("debt_asset_ratio", C, "fin_debt_to_assets", "")
m("eps_ttm", MISS, "", "EPS 水平;上游有 eps", "P1")
m("financial_leverage", C, "fin_equity_multiplier", "")
m("gpm_ttm", C, "fin_gross_margin", "")
m("icr", C, "fin_interest_coverage", "")
m("income_tax_yoy", MISS, "", "所得税 TTM 同比;income_statements 已同步", "P2")
m("np_to_inventory_yoy", MISS, "", "单位存货净利同比;需 NP/存货组合", "P2")
m("npm_q", C, "fin_net_margin_q", "")
m("quality_composite", A, "qmj_profitability", "AQR 6 项 vs 平台 QMJ 盈利支柱(ROE/ROA/毛利率/OCF 收入比)")
m("ar_ap_to_revenue", A, "qlt_advance_receipts_ratio", "(预收-预付)/营收 vs 平台预收(含合同负债)/营收")
m("asset_turnover", C, "fin_total_assets_turnover", "")
m("delta_current_ratio", MISS, "", "同比变化族;上游有", "P1")
m("delta_de", MISS, "", "同比变化族", "P1")
m("gpm_q", C, "fin_gross_margin_q", "")
m("cash_profit_ratio", A, "qlt_ocf_to_profit", "(OCF-NP)/NP vs 平台 OCF/NP")
m("delta_opm", MISS, "", "营业利润率先缺水平值", "P2")
m("eps_q", MISS, "", "EPS 水平", "P1")
m("eps_y", MISS, "", "EPS 水平", "P1")
m("delta_npm", MISS, "", "同比变化族", "P1")
m("market_value_leverage", MISS, "", "(市值-非流动负债)/市值", "P2")
m("npm_y", A, "fin_net_margin", "年度 vs 平台 TTM/最新公告口径")
m("opm_y", MISS, "", "营业利润率水平;income_statements 已同步", "P1")
m("quick_ratio", C, "fin_quick_ratio", "")
m("delta_gpm", MISS, "", "同比变化族", "P1")
m("gpm_y", A, "fin_gross_margin", "年度口径")
m("np_to_total_expenses_yoy", MISS, "", "三费细分列", "P2")
m("np_to_deferred_tax_yoy", MISS, "", "递延所得税资产列覆盖存疑", "P3")
m("roe_y", A, "fin_roe", "年度口径")
m("roa_q", C, "fin_roa_q", "")
m("gpm_qoq", MISS, "", "毛利率 TTM 环比;序列已有可算", "P1")
m("delta_inventory_turnover", MISS, "", "同比变化族", "P1")
m("delta_roa", MISS, "", "同比变化族", "P1")
m("fixed_asset_turnover", C, "fin_fixed_assets_turnover", "")
m("npm_ttm", C, "fin_net_margin", "")
m("opm_ttm", MISS, "", "营业利润率水平", "P1")
m("receivable_turnover", C, "fin_receivables_turnover", "")
m("cfcr", MISS, "", "OCF/利息费用;上游有", "P1")
m("delta_cash_ratio", MISS, "", "需现金+交易性资产列", "P2")
m("lra_yoy", MISS, "", "长期应收列覆盖存疑", "P3")
m("np_to_fixed_assets_yoy", MISS, "", "NP/固定资产同比", "P2")
m("np_to_salary_yoy", MISS, "", "薪酬列", "P2")
m("npm_q_qoq", MISS, "", "单季净利率环比;序列已有", "P1")
m("npm_tsh", MISS, "", "归母净利/平均总股本", "P3")
m("npm_ttm_qoq", MISS, "", "TTM 净利率环比", "P1")
m("cash_ratio", MISS, "", "现金比率;需现金列", "P2")
m("current_ratio", C, "fin_current_ratio", "")
m("de", C, "fin_debt_to_equity", "")
m("delta_asset_turnover", MISS, "", "同比变化族", "P1")
m("delta_quick_ratio", MISS, "", "同比变化族", "P1")
m("delta_roe", MISS, "", "同比变化族", "P1")
m("inventory_turnover", C, "fin_inventory_turnover", "")
m("roa_ttm", C, "fin_roa", "")
m("roe_ttm", C, "fin_roe", "")
m("tax_surcharge_yoy", MISS, "", "税金及附加列", "P2")
m("equity_turnover", MISS, "", "权益周转率;equity 已同步", "P1")
m("expenses_to_equity_yoy", MISS, "", "三费/净资产同比", "P2")
m("roa_y", A, "fin_roa", "年度口径")
m("opt_tpro", MISS, "", "营业利润/利润总额;两列已同步", "P1")

# ---------------- Reversal (3) ----------------
m("small_cap_reversal_21d", MISS, "", "小市值交互反转;组合构造", "P2")
m("rsi", C, "rsi_14d", "")
m("price_dist", MISS, "", "整数关口距离;调研排除项", "P3")

# ---------------- Risk (25) ----------------
m("days_beyond_upper_lower_21d", MISS, "", "异常波动统计,冷门", "P3")
m("return_std_21d", A, "vol_20d", "21 vs 20")
m("high_low_21d", MISS, "", "净值高低比;bars 有,公式简单", "P1")
m("high_low_42d", MISS, "", "同上", "P1")
m("return_std_42d", MISS, "", "窗口变体", "P1")
m("sharpe_60d", C, "sharpe_60d", "")
m("beta_60d_000300", C, "beta_60d", "")
m("return_std_63d", A, "vol_60d", "63 vs 60")
m("high_low_63d", MISS, "", "净值高低比", "P1")
m("volume_beta_120d_000300", MISS, "", "成交量 Beta;需指数成交量", "P2")
m("beta_125d_000300", A, "beta_120d", "125 vs 120")
m("high_low_126d", MISS, "", "净值高低比", "P1")
m("return_std_126d", A, "vol_120d", "126 vs 120")
m("beta_250d_000300", C, "beta_250d", "")
m("return_std_252d", A, "vol_250d", "252 vs 250")
m("high_low_252d", MISS, "", "净值高低比", "P1")
m("beta_500d_000300", MISS, "", "长窗 500", "P2")
m("sharpe_750d", A, "sharpe_1320d", "平台有 60/120/250/1320 档,无 750")
m("adjusted_sharpe_750d", MISS, "", "mean/std^4,冷门", "P3")
m("beta_1000d_000300", MISS, "", "长窗 1000", "P2")
m("sigma_1320d_000001", MISS, "", "1320 特质波动+上证基准;平台 specific_vol 60/120/250", "P2")
m("beta_1320d_000001", A, "beta_1320d", "平台同名但基准为 000300")
m("beta_consistency_1320d_000300", MISS, "", "Beta 稳定性;rolling_ols 可做", "P2")
m("sigma_1320d_000300", MISS, "", "1320 特质波动", "P2")
m("log_price", MISS, "", "log(收盘价);调研排除项", "P3")

# ---------------- Size (3) ----------------
m("float_size", A, "log_circulating_market_cap", "Tushare 为负对数,符号约定相反")
m("nl_size", MISS, "", "size³ 截面回归残差;截面回归算子有", "P2")
m("size", A, "log_total_market_cap", "负对数,符号约定相反")

# ---------------- Value (11) ----------------
m("dividend_yield_3y_avg", A, "val_dividend_yield", "Tushare 为 3 年平均,平台为 TTM")
m("pegh5", MISS, "", "5 年 EPS 复合增长 PEG", "P3")
m("etp5", MISS, "", "5 年滚动均值构造", "P2")
m("ncf_to_market", MISS, "", "净现金流(三项和)/市值;cashflow_statements 已同步", "P1")
m("fcf_to_market", C, "val_fcf_to_market", "")
m("ebitda_to_market", C, "val_ebitda_to_market", "")
m("earnings_to_price", C, "val_earnings_to_price", "")
m("book_to_market", A, "val_bm", "Tushare 含递延税调整,平台为精确归母权益/总市值")
m("earnings_cut_to_market", MISS, "", "扣非净利 TTM 水平;上游扣非增速列存在,水平值可推", "P2")
m("ocf_to_market", C, "val_ocf_to_market", "")
m("sales_to_market", C, "val_sales_to_price", "")

# ================= 自动翻转(注册表为准) =================
flipped = [
    name
    for name, (status, our, note, tier) in MAP.items()
    if status == MISS and name in PREDEFINED_FACTORS
]
for name in flipped:
    MAP[name] = (C, name, FLIP_NOTE, "")

# ================= 生成 =================
data = json.load(open("scripts/tushare_factor_list_20260911.json", encoding="utf-8"))
for cat in data["order"]:
    for it in data["sections"][cat]["items"]:
        assert it["name"] in MAP, f"缺映射: {cat}/{it['name']}"

tally: Counter[str] = Counter()
per_cat: dict[str, Counter[str]] = {}
lines: list[str] = []
for cat in data["order"]:
    sec = data["sections"][cat]
    lines.append(f"\n## {cat}（{sec['declared']} 个）\n")
    lines.append("| # | Tushare 因子 | 状态 | 平台因子 | 说明 |")
    lines.append("|---|---|---|---|---|")
    c: Counter[str] = Counter()
    for it in sec["items"]:
        status, our, note, tier = MAP[it["name"]]
        c[status] += 1
        extra = note
        if status == MISS and tier:
            extra = f"[{tier.split(' ')[0]}] {note}"
        lines.append(f"| {it['n']} | `{it['name']}` | {BADGE[status]} | {('`'+our+'`') if our else '—'} | {extra} |")
    per_cat[cat] = c
    tally += c
    lines.append("")
    lines.append(f"小计:✅ {c[C]} · 🟡 {c[A]} · ❌ {c[MISS]}")
    lines.append("")

total_cov = tally[C] + tally[A]
tiers: Counter[str] = Counter(
    MAP[k][3].split(" ")[0] for k in MAP if MAP[k][0] == MISS
)
_TIER_LABELS = {
    "P1": "P1 可低成本补",
    "P2": "P2 需组合/长窗",
    "P3": "P3 冷门不建议",
}
tier_summary = " · ".join(
    f"{_TIER_LABELS[t]} {tiers[t]}" for t in ("P1", "P2", "P3") if tiers[t]
)
header = f"""# 预置因子对账:Tushare 202 因子清单 × 平台预置因子

> 来源:Tushare `factor_list` 文档页(https://tushare.pro/document/2?doc_id=486,公开,抓取于 2026-09-11)。
> 平台侧:`finboard_backtest/factors/predefined/registry.py` 共 **{len(PREDEFINED_FACTORS)}** 个 `p_` 预置因子(#398 通道,批次 #399/#400/#401/#402/#415/#429 交付)。

## 结论摘要

| 口径 | 数量 |
|---|---|
| Tushare 清单 | 202 |
| ✅ 严格实现(同名/同式) | {tally[C]} |
| 🟡 近似/口径变体(已实现,窗口或构造有差) | {tally[A]} |
| **清单覆盖合计(✅+🟡)** | **{total_cov} / 202** |
| ❌ 未实现 | {tally[MISS]}({tier_summary}) |

说明:

- **窗口口径约定**:平台统一用 20/60/120/250(≈自然月/季/半年/年取整),Tushare 用 21/42/63/126/252(精确月×21)。🟡 中大量条目仅差在此,语义同族。
- **注册表 {len(PREDEFINED_FACTORS)} ≠ 清单覆盖 {total_cov}**:注册表另含 ~{len(PREDEFINED_FACTORS) - total_cov} 个平台自研因子(QMJ 四支柱、resid_momentum、corr_market、amihud、turnover_z、单季财务族等),不在 Tushare 清单内。
- 🟡/❌ 的逐项原因见下表;❌ 的 [P1]/[P2]/[P3] 为补齐优先级评估(见文末)。
- 预置因子消费路径:MCP `finboard_factor_series_build`(kind=predefined_factor)、research_run 引用门;目录可见性见 #427。

各分类覆盖:

| 类别 | ✅ | 🟡 | ❌ | 覆盖率 |
|---|---|---|---|---|
"""
for cat in data["order"]:
    c = per_cat[cat]
    header += f"| {cat}({data['sections'][cat]['declared']}) | {c[C]} | {c[A]} | {c[MISS]} | {(c[C]+c[A])*100//data['sections'][cat]['declared']}% |\n"

tail = f"""
## 补齐评估(#426 第 3 项交付)

批次 6(#429,2026-09)已补齐原评估的 P1/P2 共 {len(flipped)} 项——P1 量价 28(#431)+ P1 财务 29(#432)+ P2 市场回归/规模非线性 14(#433)+ P2 三表/其余 15(#434),全部经 #415 质量评估闭环入册。
剩余 ❌ 共 {tally[MISS]} 项:

/*TIERS*/
## 维护
- 新增预置因子时同步更新本表(状态列与注册表一致;生成器按注册表自动翻转 ❌→✅);
- 表由 `scripts/gen_factor_mapping.py` 生成(清单 JSON 快照 `scripts/tushare_factor_list_20260911.json` 抓取自源页),手工编辑请改生成器避免漂移。
"""


# ---- tier 段自动枚举 ----
def tier_names(t: str) -> list[str]:
    return sorted(k for k, v in MAP.items() if v[0] == MISS and v[3].startswith(t))


def fmt(names: list[str]) -> str:
    return "、".join(f"`{n}`" for n in names)
t1, t2, t3 = tier_names("P1"), tier_names("P2"), tier_names("P3")
tiers_md = ""
if t1:
    tiers_md += f"""### P1 数据上游已有、低成本可补({len(t1)} 项)

多为参数化/同比变化族——同一公式换窗口或做 t vs t-252 差分,算子与数据全部就绪:
{fmt(t1)}

"""
if t2:
    tiers_md += f"""### P2 需组合构造/长窗口/多列({len(t2)} 项)

长窗回归族(alpha/beta/sigma 500-1320)、三表细分列族(#397 已同步但需覆盖率核查,银行/保险缺列多)、成交量回归族(需指数成交量进因子上下文)、交互构造(peg/small_cap_reversal/nl_size):
{fmt(t2)}

"""
if t3:
    tiers_md += f"""### P3 冷门或上游存疑,不建议近期补({len(t3)} 项)

`log_price`、`ma_20d`、`price_dist`、`days_down_up` 为调研即列为排除候选的平凡因子;其余为上游列覆盖存疑(递延所得税/长期应收/平均总股本)或冷门统计量(异常波动天数/调整夏普):
{fmt(t3)}

"""
tail = tail.replace('/*TIERS*/', tiers_md)

Path("docs/research/predefined-factor-mapping.md").write_text(header + "\n".join(lines) + tail, encoding="utf-8")
print(f"covered={tally[C]} approx={tally[A]} missing={tally[MISS]} total={sum(tally.values())}")
print("tiers:", dict(tiers))
