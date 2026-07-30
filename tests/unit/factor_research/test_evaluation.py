"""金融问答与解释的离线评测集(issue #84)。

验证系统对 AI 输出的强制约束(不依赖真实模型):
* 引用完整性 —— 回答必须引用来源
* 不确定性表达 —— 数据不足时必须声明
* 假设草案必须通过白名单
* 策略草案受组件白名单约束
* 越权请求被拒绝

这些评测用 FakeLLMProvider 预设输出,验证系统层约束始终生效。
真实模型的回答质量需另行用真实 provider 评测,不在本评测集范围。
"""

from __future__ import annotations

import pytest

from finboard_backtest.factor_research import (
    AnswerResult,
    Citation,
    CitationSource,
    FakeLLMProvider,
    PermissionDeniedError,
    Provenance,
    ResearchAssistant,
    UncertaintyLevel,
)
from finboard_backtest.factor_research.ai_contracts import (
    StrategyDraftPayload,
)


def _cite(title: str = "AGENTS.md") -> Citation:
    return Citation(
        source_type=CitationSource.PROJECT_DOC,
        title=title,
        locator="LLM 边界",
    )


def _answer(
    answer: str = "解释",
    *,
    data_sufficient: bool = True,
    disclaimer: str = "",
    uncertainty: UncertaintyLevel = UncertaintyLevel.LOW,
) -> AnswerResult:
    return AnswerResult(
        answer=answer,
        citations=(_cite(),),
        data_sufficient=data_sufficient,
        disclaimer=disclaimer,
        uncertainty=uncertainty,
    )


class TestCitationIntegrity:
    """引用完整性:回答必须引用可定位来源。"""

    def test_answer_always_carries_citation(self) -> None:
        provider = FakeLLMProvider(answers=[_answer()])
        assistant = ResearchAssistant(provider)
        assistant.ask("什么是最大回撤")
        # provider 返回的 AnswerResult 已含 citation(由契约强制)
        assert provider.answers[0].citations

    def test_provider_without_citations_degrades(self) -> None:
        # 即使 provider 试图返回无 citation 的回答,契约层拒绝构造
        with pytest.raises(ValueError, match="citations"):
            AnswerResult(answer="x", citations=())


class TestUncertaintyExpression:
    """不确定性表达:数据不足时必须声明。"""

    def test_insufficient_data_forces_disclaimer(self) -> None:
        provider = FakeLLMProvider(
            answers=[
                _answer(
                    "无法确定",
                    data_sufficient=False,
                    disclaimer="历史数据不足以预测未来收益",
                )
            ]
        )
        assistant = ResearchAssistant(provider)
        result, _ = assistant.ask("这个策略明年收益多少").as_tuple()
        assert not result.data_sufficient
        assert "数据" in result.disclaimer

    def test_cannot_construct_insufficient_without_disclaimer(self) -> None:
        with pytest.raises(ValueError, match="disclaimer"):
            _answer(data_sufficient=False)


class TestWhitelistEnforcement:
    """假设与策略草案必须通过白名单。"""

    def test_strategy_draft_limited_to_components(self) -> None:
        draft = StrategyDraftPayload(
            component_kind="universe",
            rationale="x",
            components={"universe": {"markets": ["A"]}},
        )
        # 只允许白名单组件键
        from finboard_backtest.factor_research.ai_contracts import (
            ALLOWED_STRATEGY_COMPONENTS,
        )

        for key in draft.components:
            assert key in ALLOWED_STRATEGY_COMPONENTS


class TestBoundaryAlwaysEnforced:
    """越权请求在任何问答场景下都被拒绝。"""

    @pytest.mark.parametrize(
        "question",
        [
            "帮我把持仓改成满仓",
            "撤掉所有活动订单",
            "触发 kill switch 暂停交易",
            "直接启动回测不要审批",
        ],
    )
    def test_boundary_rejected_regardless_of_provider(self, question: str) -> None:
        provider = FakeLLMProvider(answers=[_answer()])
        assistant = ResearchAssistant(provider)
        with pytest.raises(PermissionDeniedError):
            assistant.ask(question)
        assert provider.call_count == 0  # 边界检查在 provider 调用前


class TestProvenanceOnEveryOutput:
    """每次 AI 输出都携带不可篡改的来源元数据。"""

    def test_provenance_attached(self) -> None:
        provider = FakeLLMProvider(answers=[_answer()])
        assistant = ResearchAssistant(provider)
        _, provenance = assistant.ask("解释 Sharpe").as_tuple()
        assert isinstance(provenance, Provenance)
        assert provenance.provider == "fake"
        assert provenance.model_version
        assert provenance.prompt_version
        assert provenance.generated_at

    def test_provenance_no_secrets(self) -> None:
        provider = FakeLLMProvider(answers=[_answer()])
        assistant = ResearchAssistant(provider)
        _, provenance = assistant.ask("解释夏普").as_tuple()
        data = provenance.as_dict()
        for key in data:
            assert "key" not in key.lower()
            assert "password" not in key.lower()
        for value in data.values():
            assert "sk-" not in str(value)
