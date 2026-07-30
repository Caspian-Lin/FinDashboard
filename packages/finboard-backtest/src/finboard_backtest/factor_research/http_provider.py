"""OpenAI 兼容 HTTP LLM Provider(Issue #84)。

通过 ``httpx`` 调用 OpenAI 兼容的 ``/v1/chat/completions`` 接口
(支持 OpenAI / DeepSeek / 通义 / 智谱 GLM / 本地 vLLM 等),
强制 ``response_format=json_object`` 结构化输出。

安全约束(红线):
* Provider 只返回受白名单 / schema 校验的结构化对象,解析失败即降级
* 不向模型暴露 Broker / 账户 / 凭证 / 实盘环境配置
* API key 属于敏感字段,不进入日志 / 审计 / :class:`Provenance`
* 超时 / 限流(429) / 5xx 在有限退避后降级为 :class:`LLMUnavailableError`
* 不自动重试下单类操作,不错误晋级研究状态

测试通过注入 ``httpx.Client``(自定义 transport)实现,不依赖公网。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from finboard_backtest.factor_research.ai_contracts import (
    ALLOWED_STRATEGY_COMPONENTS,
    AnswerResult,
    Citation,
    CitationSource,
    StrategyDiffItem,
    StrategyDiffPayload,
    StrategyDraftPayload,
    UncertaintyLevel,
)
from finboard_backtest.factor_research.hypothesis import (
    FactorHypothesis,
    HypothesisStatus,
    ParameterSpec,
    Reference,
)
from finboard_backtest.factor_research.provider import (
    PROMPT_VERSION,
    LLMUnavailableError,
)
from finboard_backtest.factor_research.sanitizer import sanitize_prompt
from finboard_backtest.factor_research.whitelist import (
    ALLOWED_FIELDS,
    ALLOWED_OPERATORS,
)

_SYSTEM_PREAMBLE = (
    "你是 FinDashboard 的研究教育助手,只服务量化研究与金融知识科普。\n"
    "你不是投资顾问,不保证收益,不能下单,不连接实盘账户。\n"
    "你的建议必须经过人工审批和正式研究流水线验证后才可使用。\n"
    "禁止生成或执行 Python 代码、模块路径、shell 命令或任意可执行表达式。\n"
    "数据不足或无法确认时必须明确说明,严禁编造市场数据、收益或监管结论。\n"
    "所有回答必须引用项目内可定位的来源,或标注为外部参考文献。\n"
)

_HYPOTHESIS_SCHEMA_HINT = (
    "\n现在请基于用户问题,提出一个可检验的因子假设,严格输出如下 JSON:\n"
    "{\n"
    '  "name": "因子名称",\n'
    '  "economic_mechanism": "经济机制(为什么有效)",\n'
    '  "input_fields": ["仅允许的白名单字段"],\n'
    '  "decision_timing": "close|open|vwap|twap",\n'
    '  "formula": "仅使用白名单算子的人类可读公式,不含可执行代码",\n'
    '  "direction": "long_high|long_low|neutral",\n'
    '  "applicable_assets": ["适用资产"],\n'
    '  "expected_failure_scenarios": ["预期失效场景"],\n'
    '  "parameters": [{"name":"参数名","min_value":0,"max_value":0,"grid_size":1}],\n'
    '  "references": [{"title":"文献标题","authors":"作者","year":2020,"url":"链接"}]\n'
    "}\n"
    f"白名单字段: {sorted(ALLOWED_FIELDS)}\n"
    f"白名单算子: {sorted(ALLOWED_OPERATORS)}\n"
)

_DRAFT_SCHEMA_HINT = (
    "\n现在请基于用户问题,生成一个策略组件草案,严格输出如下 JSON:\n"
    "{\n"
    '  "component_kind": "universe|feature_graph|signal_rules|portfolio_policy|'
    'risk_exit_policy|execution_model|validation_plan",\n'
    '  "rationale": "理由",\n'
    '  "components": {"<允许的组件键>": <结构化配置>},\n'
    '  "failure_scenarios": ["预期失效场景"],\n'
    '  "data_requirements": ["数据需求"],\n'
    '  "risk_notes": ["风险提示"]\n'
    "}\n"
    f"允许的组件键: {sorted(ALLOWED_STRATEGY_COMPONENTS)}\n"
    "components 内不得包含任何可执行代码、模块路径或表达式。\n"
)

_DIFF_SCHEMA_HINT = (
    "\n现在请基于用户问题,生成对现有策略的结构化修改建议,严格输出如下 JSON:\n"
    "{\n"
    '  "target_strategy_id": "策略ID",\n'
    '  "target_version": 1,\n'
    '  "summary": "修改摘要",\n'
    '  "rationale": "理由",\n'
    '  "changes": [{"path":"配置路径","operation":"add|remove|replace",'
    '"old_value":null,"new_value":null,"explanation":"说明"}],\n'
    '  "assumptions": ["假设"],\n'
    '  "failure_scenarios": ["失效场景"],\n'
    '  "data_requirements": ["数据需求"],\n'
    '  "risk_notes": ["风险提示"]\n'
    "}\n"
    "changes 的值不得包含任何可执行代码或模块路径。\n"
)

_ANSWER_SCHEMA_HINT = (
    "\n现在请用通俗语言回答用户的研究问题(面向金融知识有限的用户),"
    "严格输出如下 JSON:\n"
    "{\n"
    '  "answer": "通俗解释",\n'
    '  "technical_detail": "可选的技术细节",\n'
    '  "citations": [{"source_type":"project_doc|data_dictionary|strategy_spec|'
    'research_run|factor_definition|validation_experiment|external_reference",'
    '"title":"标题","locator":"定位符","snippet":"摘录"}],\n'
    '  "uncertainty": "low|medium|high",\n'
    '  "data_sufficient": true,\n'
    '  "disclaimer": "数据不足或免责声明(若 data_sufficient=false 必填)"\n'
    "}\n"
    "citations 必须非空。无法确认或数据不足时 data_sufficient=false 并给出 disclaimer。\n"
)


def build_hypothesis_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE + _HYPOTHESIS_SCHEMA_HINT},
        {"role": "user", "content": sanitize_prompt(prompt)},
    ]


def build_draft_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE + _DRAFT_SCHEMA_HINT},
        {"role": "user", "content": sanitize_prompt(prompt)},
    ]


def build_diff_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE + _DIFF_SCHEMA_HINT},
        {"role": "user", "content": sanitize_prompt(prompt)},
    ]


def build_answer_messages(prompt: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _SYSTEM_PREAMBLE + _ANSWER_SCHEMA_HINT},
        {"role": "user", "content": sanitize_prompt(prompt)},
    ]


@dataclass
class OpenAICompatibleConfig:
    """OpenAI 兼容 provider 配置。

    ``api_key`` 属于敏感字段,不进入日志 / 审计 / :class:`Provenance`。
    """

    base_url: str
    api_key: str = field(repr=False)
    model: str = "gpt-4o-mini"
    timeout_seconds: float = 30.0
    max_retries: int = 3
    # 可选:透传给 chat completions 的额外字段(如 temperature)。
    extra_body: dict[str, Any] = field(default_factory=dict)


class OpenAICompatibleLLMProvider:
    """OpenAI 兼容 HTTP LLM Provider。

    用法::

        config = OpenAICompatibleConfig(
            base_url="https://api.openai.com/v1",
            api_key="sk-...",
            model="gpt-4o-mini",
        )
        provider = OpenAICompatibleLLMProvider(config)
        hypothesis = provider.generate_hypothesis("动量因子假设")

    测试中可注入自定义 ``httpx.Client``(携带 mock transport),
    完全不依赖公网。
    """

    def __init__(
        self,
        config: OpenAICompatibleConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url.rstrip("/"),
            timeout=httpx.Timeout(config.timeout_seconds),
            headers={
                "Authorization": f"Bearer {config.api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> OpenAICompatibleLLMProvider:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # LLMProvider 接口
    # ------------------------------------------------------------------

    def generate_hypothesis(self, prompt: str) -> FactorHypothesis:
        data = self._complete_json(build_hypothesis_messages(prompt))
        return _parse_hypothesis(data)

    def generate_strategy_draft(self, prompt: str) -> StrategyDraftPayload:
        data = self._complete_json(build_draft_messages(prompt))
        return _parse_strategy_draft(data)

    def generate_strategy_diff(self, prompt: str) -> StrategyDiffPayload:
        data = self._complete_json(build_diff_messages(prompt))
        return _parse_strategy_diff(data)

    def answer_question(self, prompt: str) -> AnswerResult:
        data = self._complete_json(build_answer_messages(prompt))
        return _parse_answer(data, question=prompt)

    def provider_name(self) -> str:
        return "openai-compatible"

    def model_version(self) -> str:
        return self._config.model

    def prompt_version(self) -> str:
        return PROMPT_VERSION

    # ------------------------------------------------------------------
    # HTTP 调用 + 退避 + 降级
    # ------------------------------------------------------------------

    def _complete_json(self, messages: list[dict[str, str]]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self._config.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
        }
        if self._config.extra_body:
            payload.update(self._config.extra_body)

        last_exc: Exception | None = None
        for attempt in range(self._config.max_retries + 1):
            try:
                response = self._client.post("/chat/completions", json=payload)
            except httpx.TimeoutException as exc:
                # 超时:不重试(下单类操作的"最危险场景"同源原则),
                # 直接降级,避免不可观测的重复请求。
                raise LLMUnavailableError(
                    f"LLM 请求超时({self._config.timeout_seconds}s)"
                ) from exc
            except httpx.HTTPError as exc:
                last_exc = exc
                self._backoff(attempt)
                continue

            if response.status_code == 429 or response.status_code >= 500:
                last_exc = LLMUnavailableError(
                    f"LLM 服务返回 {response.status_code}"
                )
                if attempt < self._config.max_retries:
                    self._backoff(attempt)
                    continue
                raise last_exc

            if response.status_code >= 400:
                raise LLMUnavailableError(
                    f"LLM 服务返回 {response.status_code}: "
                    f"{response.text[:200]}"
                )

            return self._extract_json(response)

        raise LLMUnavailableError(
            f"LLM 请求失败,已耗尽重试: {last_exc}"
        )

    def _extract_json(self, response: httpx.Response) -> dict[str, Any]:
        try:
            envelope = response.json()
        except ValueError as exc:
            raise LLMUnavailableError(
                "LLM 返回非 JSON 响应体"
            ) from exc
        try:
            content = envelope["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMUnavailableError(
                "LLM 响应缺少 choices[0].message.content"
            ) from exc
        if not isinstance(content, str):
            raise LLMUnavailableError("LLM message.content 不是字符串")
        return _parse_json_content(content)

    @staticmethod
    def _backoff(attempt: int) -> None:
        # 指数退避: 1s, 2s, 4s ... (上限 8s)
        delay = min(8.0, 2.0**attempt)
        time.sleep(delay)


def _parse_json_content(content: str) -> dict[str, Any]:
    """容错解析模型返回的 JSON 文本。

    优先 ``json.loads``;失败时尝试提取 ```json``` 代码块或首个 ``{...}`` ``。
    解析失败降级为 :class:`LLMUnavailableError`。
    """
    try:
        parsed = json.loads(content)
    except ValueError:
        stripped = content.strip()
        if stripped.startswith("```"):
            inner = stripped.strip("`")
            if inner.lower().startswith("json"):
                inner = inner[4:]
            try:
                parsed = json.loads(inner.strip())
            except ValueError as exc:
                raise LLMUnavailableError(
                    "LLM 返回的 JSON 代码块无法解析"
                ) from exc
        else:
            start = stripped.find("{")
            end = stripped.rfind("}")
            if start != -1 and end > start:
                try:
                    parsed = json.loads(stripped[start : end + 1])
                except ValueError as exc:
                    raise LLMUnavailableError(
                        "LLM 返回的内容无法解析为 JSON"
                    ) from exc
            else:
                raise LLMUnavailableError(
                    "LLM 返回的内容不包含 JSON 对象"
                ) from None
    if not isinstance(parsed, dict):
        raise LLMUnavailableError("LLM 返回的 JSON 不是对象")
    return parsed


def _require_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise LLMUnavailableError(f"LLM 输出缺少有效的字符串字段: {key}")
    return value


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(v) for v in value if isinstance(v, str) and v.strip())


def _parse_hypothesis(data: dict[str, Any]) -> FactorHypothesis:
    name = _require_str(data, "name")
    economic_mechanism = _require_str(data, "economic_mechanism")
    input_fields = _as_str_tuple(data.get("input_fields"))
    if not input_fields:
        raise LLMUnavailableError("LLM 输出缺少 input_fields")
    formula = _require_str(data, "formula")
    decision_timing = _require_str(data, "decision_timing")
    direction = _require_str(data, "direction")

    parameters: tuple[ParameterSpec, ...] = ()
    raw_params = data.get("parameters") or []
    if isinstance(raw_params, list):
        parsed_params: list[ParameterSpec] = []
        for item in raw_params:
            if not isinstance(item, dict):
                continue
            try:
                parsed_params.append(
                    ParameterSpec(
                        name=str(item["name"]),
                        min_value=float(item.get("min_value", 0)),
                        max_value=float(item.get("max_value", 0)),
                        grid_size=int(item.get("grid_size", 1)),
                    )
                )
            except (KeyError, ValueError, TypeError):
                continue
        parameters = tuple(parsed_params)

    references = _parse_references(data.get("references"))

    try:
        return FactorHypothesis(
            name=name,
            economic_mechanism=economic_mechanism,
            input_fields=input_fields,
            decision_timing=decision_timing,
            formula=formula,
            direction=direction,
            applicable_assets=_as_str_tuple(data.get("applicable_assets")),
            expected_failure_scenarios=_as_str_tuple(
                data.get("expected_failure_scenarios")
            ),
            parameters=parameters,
            references=references,
            status=HypothesisStatus.PROPOSED,
        )
    except ValueError as exc:
        raise LLMUnavailableError(
            f"LLM 输出的因子假设不合法: {exc}"
        ) from exc


def _parse_references(value: Any) -> tuple[Reference, ...]:
    if not isinstance(value, list):
        return ()
    refs: list[Reference] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        try:
            refs.append(
                Reference(
                    title=title,
                    authors=str(item.get("authors", "")),
                    year=int(item["year"]) if item.get("year") else None,
                    url=item.get("url") if isinstance(item.get("url"), str) else None,
                    doi=item.get("doi") if isinstance(item.get("doi"), str) else None,
                )
            )
        except (ValueError, TypeError):
            continue
    return tuple(refs)


def _parse_strategy_draft(data: dict[str, Any]) -> StrategyDraftPayload:
    component_kind = _require_str(data, "component_kind")
    rationale = _require_str(data, "rationale")
    components = data.get("components")
    if not isinstance(components, dict):
        raise LLMUnavailableError("LLM 输出缺少 components 对象")
    try:
        return StrategyDraftPayload(
            component_kind=component_kind,
            rationale=rationale,
            components=components,
            failure_scenarios=_as_str_tuple(data.get("failure_scenarios")),
            data_requirements=_as_str_tuple(data.get("data_requirements")),
            risk_notes=_as_str_tuple(data.get("risk_notes")),
        )
    except ValueError as exc:
        raise LLMUnavailableError(
            f"LLM 输出的策略草案不合法: {exc}"
        ) from exc


def _parse_strategy_diff(data: dict[str, Any]) -> StrategyDiffPayload:
    target_strategy_id = _require_str(data, "target_strategy_id")
    raw_version = data.get("target_version", 1)
    try:
        target_version = int(raw_version)
    except (ValueError, TypeError):
        target_version = 1
    summary = _require_str(data, "summary")
    rationale = _require_str(data, "rationale")
    raw_changes = data.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        raise LLMUnavailableError("LLM 输出缺少 changes 列表")
    changes: list[StrategyDiffItem] = []
    for item in raw_changes:
        if not isinstance(item, dict):
            continue
        try:
            changes.append(
                StrategyDiffItem(
                    path=str(item["path"]),
                    operation=str(item["operation"]),
                    old_value=item.get("old_value"),
                    new_value=item.get("new_value"),
                    explanation=str(item.get("explanation", "")),
                )
            )
        except (KeyError, ValueError):
            continue
    if not changes:
        raise LLMUnavailableError("LLM 输出的 changes 无法解析")
    try:
        return StrategyDiffPayload(
            target_strategy_id=target_strategy_id,
            target_version=target_version,
            summary=summary,
            rationale=rationale,
            changes=tuple(changes),
            assumptions=_as_str_tuple(data.get("assumptions")),
            failure_scenarios=_as_str_tuple(data.get("failure_scenarios")),
            data_requirements=_as_str_tuple(data.get("data_requirements")),
            risk_notes=_as_str_tuple(data.get("risk_notes")),
        )
    except ValueError as exc:
        raise LLMUnavailableError(
            f"LLM 输出的策略 diff 不合法: {exc}"
        ) from exc


def _parse_answer(data: dict[str, Any], *, question: str) -> AnswerResult:
    answer = _require_str(data, "answer")
    raw_citations = data.get("citations")
    if not isinstance(raw_citations, list) or not raw_citations:
        raise LLMUnavailableError("LLM 回答缺少 citations")
    citations: list[Citation] = []
    for item in raw_citations:
        if not isinstance(item, dict):
            continue
        title = item.get("title")
        if not isinstance(title, str) or not title.strip():
            continue
        source_raw = str(item.get("source_type", "external_reference"))
        try:
            source_type = CitationSource(source_raw)
        except ValueError:
            source_type = CitationSource.EXTERNAL_REFERENCE
        citations.append(
            Citation(
                source_type=source_type,
                title=title,
                locator=str(item.get("locator", "")),
                snippet=str(item.get("snippet", "")),
            )
        )
    if not citations:
        raise LLMUnavailableError("LLM 回答的 citations 无法解析")

    uncertainty_raw = str(data.get("uncertainty", "medium"))
    try:
        uncertainty = UncertaintyLevel(uncertainty_raw)
    except ValueError:
        uncertainty = UncertaintyLevel.MEDIUM

    data_sufficient = bool(data.get("data_sufficient", True))
    disclaimer = str(data.get("disclaimer", ""))
    if not data_sufficient and not disclaimer.strip():
        disclaimer = "数据不足,无法给出确定结论,请补充数据后再评估。"

    technical_detail = data.get("technical_detail")
    if not isinstance(technical_detail, str):
        technical_detail = ""

    try:
        return AnswerResult(
            answer=answer,
            citations=tuple(citations),
            uncertainty=uncertainty,
            technical_detail=technical_detail,
            data_sufficient=data_sufficient,
            disclaimer=disclaimer,
            question=question,
        )
    except ValueError as exc:
        raise LLMUnavailableError(
            f"LLM 回答结构不合法: {exc}"
        ) from exc


__all__ = [
    "OpenAICompatibleConfig",
    "OpenAICompatibleLLMProvider",
    "build_answer_messages",
    "build_diff_messages",
    "build_draft_messages",
    "build_hypothesis_messages",
]
