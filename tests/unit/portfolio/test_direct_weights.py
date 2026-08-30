"""``direct_weights`` 分配器与 build_portfolio 直权重路径(issue #218)。

验收点:decide 权重不被再缩放(和 < 1 = 持现金)、负权重/超上限由
约束投影逐项截断并记审计、equal_weight 兜底对 direct_weights 禁用。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_backtest.portfolio import (
    PortfolioBuildInput,
    PortfolioConstraints,
    Signal,
    build_portfolio,
)
from finboard_backtest.portfolio.allocators import (
    AllocationError,
    DirectWeightsAllocator,
)


def _signal(symbol: str, score: float) -> Signal:
    return Signal(
        symbol=symbol,
        score=score,
        timestamp=date(2024, 6, 3),
        strategy_id="user_code_test",
    )


class TestDirectWeightsAllocator:
    def test_scores_used_as_weights_no_rescale(self) -> None:
        raw = DirectWeightsAllocator().allocate(
            [_signal("a", 0.3), _signal("b", 0.2)],
            None,
            PortfolioConstraints(),
        )
        assert raw.weights == {"a": 0.3, "b": 0.2}
        # 不归一:权重和可以 < 1(现金),gross 按实际值申报
        assert raw.cash_buffer == pytest.approx(0.5)

    def test_negative_weights_pass_through_long_short_view(self) -> None:
        raw = DirectWeightsAllocator().allocate(
            [_signal("a", -0.2), _signal("b", 0.5)],
            None,
            PortfolioConstraints(long_only=False),
        )
        assert raw.weights == {"a": -0.2, "b": 0.5}

    def test_disabled_symbols_zeroed(self) -> None:
        from finboard_backtest.portfolio.allocators import AllocationContext

        raw = DirectWeightsAllocator().allocate(
            [_signal("a", 0.3), _signal("b", 0.2)],
            None,
            PortfolioConstraints(),
            context=AllocationContext(disabled_symbols=frozenset({"a"})),
        )
        assert raw.weights == {"b": 0.2}


class TestBuildPortfolioDirectWeights:
    def _build(
        self,
        scores: dict[str, float],
        constraints: PortfolioConstraints | None = None,
    ):
        signals = tuple(
            _signal(symbol, score) for symbol, score in scores.items()
        )
        return build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method="direct_weights",
                constraints=constraints or PortfolioConstraints(
                    max_weight_per_asset=0.25, long_only=True
                ),
            )
        )

    def test_weights_not_rescaled_below_one(self) -> None:
        result = self._build({"a": 0.3, "b": 0.2})
        before = result.target_before_constraints.weights
        # 约束层截断(0.3 > 0.25 → 0.25)发生在 apply_portfolio_constraints,
        # before 阶段保持 decide 原始权重(不再被 desired_gross 重缩放)。
        assert before == {"a": 0.3, "b": 0.2}
        after = result.target_after_constraints.weights
        assert after["a"] == pytest.approx(0.25)
        assert after["b"] == pytest.approx(0.2)
        # 审计:超上限截断逐项可见(附 reason)
        adjustments = {
            item.constraint: item for item in result.adjustments
        }
        assert "max_weight_per_asset" in adjustments
        assert any(
            item.symbol == "a" and not item.passed
            for item in result.adjustments
            if item.constraint == "max_weight_per_asset"
        )

    def test_negative_truncated_with_audit_in_long_only(self) -> None:
        result = self._build({"a": -0.1, "b": 0.4})
        assert result.target_after_constraints.weights.get("a", 0.0) == 0.0
        # 负权重在信号消解层截断(SignalResolution 审计),不在目标权重里出现
        resolution = {
            item.symbol: item for item in result.resolutions
        }.get("a")
        assert resolution is not None
        assert "long-only" in resolution.reason or "long_only" in resolution.reason

    def test_zero_sum_gives_empty_portfolio(self) -> None:
        """全 0 权重(空仓观望)在 _resolve_signals 后落到空组合分支。"""
        signals = tuple(_signal(symbol, 0.0) for symbol in ("a", "b"))
        # _resolve_signals 丢弃零分信号 → resolved 空 → build_portfolio
        # 返回空目标(而非报错)。
        result = build_portfolio(
            PortfolioBuildInput(
                signals=signals,
                method="direct_weights",
                constraints=PortfolioConstraints(),
            )
        )
        assert result.target_after_constraints.weights == {}

    def test_method_not_registered_in_spec_allocation(self) -> None:
        """direct_weights 不暴露给 spec 的 allocation_method(仅内部使用)。"""
        from finboard_backtest.strategy_spec.contracts import AllocationMethod

        assert "direct_weights" not in {item.value for item in AllocationMethod}

    def test_equal_weight_fallback_not_applied_to_direct(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """direct_weights 分配异常不得静默降级 equal_weight(会改写权重语义)。"""
        from finboard_backtest.portfolio import builder as builder_module
        from finboard_backtest.portfolio.allocators import make_allocator as real_make_allocator

        class _Boom:
            name = "direct_weights"

            def allocate(self, *args, **kwargs):  # pragma: no cover
                raise AllocationError("boom")

        requested: list[str] = []
        def _fake_make(method: str):
            requested.append(method)
            if method == "direct_weights":
                return _Boom()
            return real_make_allocator(method)

        monkeypatch.setattr(builder_module, "make_allocator", _fake_make)
        with pytest.raises(AllocationError, match="fail_closed"):
            build_portfolio(
                PortfolioBuildInput(
                    signals=(_signal("a", 0.2),),
                    method="direct_weights",
                    constraints=PortfolioConstraints(),
                )
            )
        assert "equal_weight" not in requested  # 未走等权兜底
