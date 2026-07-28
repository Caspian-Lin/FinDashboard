"""因子输入字段与算子白名单(Issue #65)。

LLM 输出的 :class:`FactorHypothesis` 必须通过白名单验证:
- 输入字段必须在 ``ALLOWED_FIELDS`` 中
- 公式中的函数调用必须映射到 ``ALLOWED_OPERATORS`` 或已知字段
- 禁止任意 Python / SQL / shell / 网络调用
- 参数搜索空间不能超过上限(防止数据挖掘)
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from finboard_backtest.factor_research.hypothesis import FactorHypothesis

ALLOWED_FIELDS: frozenset[str] = frozenset({
    "open", "high", "low", "close", "volume",
    "vwap", "amount", "turnover", "open_interest", "settlement",
    "adv5", "adv10", "adv20", "adv60",
    "returns_1d", "returns_5d", "returns_20d",
    "market_cap", "pe_ratio", "pb_ratio",
    "ps_ratio", "pcf_ratio", "dividend_yield",
    "debt_to_equity", "roe", "roa", "current_ratio",
    "rsi_14", "macd", "bollinger_upper", "bollinger_lower",
    "conversion_premium", "bond_value", "put_price", "call_price",
    "front_close", "next_close",
})

ALLOWED_OPERATORS: frozenset[str] = frozenset({
    "rank", "zscore", "quantile", "winsorize", "normalize",
    "delay", "delta", "ts_rank", "ts_zscore",
    "ts_mean", "ts_std", "ts_max", "ts_min", "ts_sum",
    "ts_corr", "ts_cov", "ts_regression",
    "ema", "sma", "wma",
    "abs", "log", "log_diff", "sigmoid", "sign", "sqrt",
    "max", "min", "mean", "std", "sum", "median", "var",
    "add", "subtract", "multiply", "divide",
    "less", "greater", "equal", "if_else",
    "scale", "clamp", "holdings_from_weights",
})

_DANGEROUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bimport\b\s"),
    re.compile(r"\bfrom\b\s+\w+\s+import\b"),
    re.compile(r"\bexec\b\s*\("),
    re.compile(r"\beval\b\s*\("),
    re.compile(r"\bcompile\b\s*\("),
    re.compile(r"\b__\w+__\b"),
    re.compile(r"\bopen\b\s*\("),
    re.compile(r"\bsubprocess\b"),
    re.compile(r"\bos\b\s*\."),
    re.compile(r"\bsys\b\s*\."),
    re.compile(r"\bsocket\b"),
    re.compile(r"\brequests\b"),
    re.compile(r"\bhttp\b", re.IGNORECASE),
    re.compile(r"\bSELECT\b.*\bFROM\b", re.IGNORECASE),
    re.compile(r"\bINSERT\b\s+INTO\b", re.IGNORECASE),
    re.compile(r"\bDROP\b\s+TABLE\b", re.IGNORECASE),
    re.compile(r"\bDELETE\b\s+FROM\b", re.IGNORECASE),
    re.compile(r"\bshell\b", re.IGNORECASE),
    re.compile(r"\bglobals\b\s*\("),
    re.compile(r"\blocals\b\s*\("),
    re.compile(r"\bgetattr\b\s*\("),
    re.compile(r"\bsetattr\b\s*\("),
    re.compile(r"\b__import__\b"),
    re.compile(r"\bpickle\b"),
    re.compile(r"\bmarshal\b"),
    re.compile(r"\bshutil\b"),
    re.compile(r"\bpathlib\b"),
    re.compile(r"\bthreading\b"),
    re.compile(r"\bmultiprocessing\b"),
    re.compile(r"\basyncio\b"),
    re.compile(r"\bbroker\b", re.IGNORECASE),
    re.compile(r"\border_manager\b", re.IGNORECASE),
    re.compile(r"\bkill_switch\b", re.IGNORECASE),
)

_FUNC_CALL_PATTERN = re.compile(r"([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")

_VALID_TIMINGS: frozenset[str] = frozenset({
    "close", "open", "vwap", "twap",
})

_VALID_DIRECTIONS: frozenset[str] = frozenset({
    "long_high", "long_low", "neutral",
})


@dataclass(frozen=True, slots=True)
class HypothesisValidationResult:
    """假设验证结果。"""

    is_valid: bool
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.is_valid and self.errors:
            raise ValueError("is_valid=True 但 errors 非空")

    @property
    def passed(self) -> bool:
        return self.is_valid


class HypothesisValidator:
    """验证 :class:`FactorHypothesis` 是否符合安全约束。

    检查项目:
    1. 输入字段是否在白名单中
    2. 公式是否包含危险关键字(Python/SQL/shell/网络/实盘)
    3. 公式中的函数调用是否映射到白名单算子
    4. 决策时点是否合法(防止未来函数)
    5. 参数搜索空间是否超限
    6. 是否有参考文献和失效场景描述(警告级)
    """

    def __init__(
        self,
        *,
        allowed_fields: frozenset[str] = ALLOWED_FIELDS,
        allowed_operators: frozenset[str] = ALLOWED_OPERATORS,
        max_parameter_budget: int = 1000,
    ) -> None:
        self._fields = allowed_fields
        self._operators = allowed_operators
        self._max_budget = max_parameter_budget

    def validate(self, hypothesis: FactorHypothesis) -> HypothesisValidationResult:
        errors: list[str] = []
        warnings: list[str] = []

        unknown_fields = [
            f for f in hypothesis.input_fields
            if f not in self._fields
        ]
        if unknown_fields:
            errors.append(
                f"未知输入字段: {unknown_fields}. "
                f"允许: {sorted(self._fields)}"
            )

        for pattern in _DANGEROUS_PATTERNS:
            m = pattern.search(hypothesis.formula)
            if m:
                errors.append(f"公式包含危险关键字: '{m.group().strip()}'")

        formula_lower = hypothesis.formula.lower()
        used_calls = set(_FUNC_CALL_PATTERN.findall(formula_lower))
        unknown_calls = used_calls - self._operators - self._fields
        if unknown_calls:
            errors.append(
                f"公式引用了未知算子: {sorted(unknown_calls)}. "
                f"允许的算子: {sorted(self._operators)}"
            )

        if hypothesis.decision_timing not in _VALID_TIMINGS:
            errors.append(
                f"决策时点 '{hypothesis.decision_timing}' 不合法. "
                f"允许: {sorted(_VALID_TIMINGS)}"
            )

        if hypothesis.direction not in _VALID_DIRECTIONS:
            errors.append(
                f"方向 '{hypothesis.direction}' 不合法. "
                f"允许: {sorted(_VALID_DIRECTIONS)}"
            )

        budget = hypothesis.parameter_budget
        if budget > self._max_budget:
            errors.append(
                f"参数预算 {budget} 超过上限 {self._max_budget}"
            )
        elif budget > 100:
            warnings.append(
                f"参数预算较高 ({budget}), 多重检验偏差风险"
            )

        if len(hypothesis.references) == 0:
            warnings.append("假设没有引用任何文献")

        if len(hypothesis.expected_failure_scenarios) == 0:
            warnings.append("假设没有描述预期失效场景")

        return HypothesisValidationResult(
            is_valid=len(errors) == 0,
            errors=tuple(errors),
            warnings=tuple(warnings),
        )
