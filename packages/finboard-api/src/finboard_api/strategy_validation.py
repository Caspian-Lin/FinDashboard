"""策略 schema 的 API 映射与统一校验。"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from finboard_api.schemas import StrategyInfoOut, StrategyParamInfo
from finboard_app.strategies import StrategyDefinition, get_strategy_definition


def _find_type_schema(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """从 Pydantic JSON Schema 中找出最适合表单控件的分支。"""
    if "$ref" in schema:
        ref = str(schema["$ref"]).removeprefix("#/$defs/")
        resolved = root.get("$defs", {}).get(ref, {})
        return {**resolved, **{k: v for k, v in schema.items() if k != "$ref"}}

    candidates = schema.get("anyOf")
    if isinstance(candidates, list):
        preferred = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and item.get("type") in {"integer", "number", "boolean"}
            ),
            None,
        )
        if preferred is None:
            preferred = next(
                (
                    item
                    for item in candidates
                    if isinstance(item, dict) and item.get("type") != "null"
                ),
                {},
            )
        return {**preferred, **{k: v for k, v in schema.items() if k != "anyOf"}}
    return schema


def strategy_info(definition: StrategyDefinition) -> StrategyInfoOut:
    """把策略 Pydantic schema 映射为稳定的 API 表单契约。"""
    schema = definition.params_model.model_json_schema()
    required = set(schema.get("required", []))
    params: list[StrategyParamInfo] = []

    for name, raw_value in schema.get("properties", {}).items():
        raw = dict(raw_value)
        resolved = _find_type_schema(raw, schema)
        nullable = any(
            isinstance(item, dict) and item.get("type") == "null"
            for item in raw.get("anyOf", [])
        )
        params.append(
            StrategyParamInfo(
                name=name,
                label=str(raw.get("title", name)),
                type=str(resolved.get("type", "string")),
                default=raw.get("default"),
                required=name in required,
                description=str(raw.get("description", "")),
                enum=resolved.get("enum"),
                minimum=resolved.get("minimum"),
                maximum=resolved.get("maximum"),
                exclusive_minimum=resolved.get("exclusiveMinimum"),
                exclusive_maximum=resolved.get("exclusiveMaximum"),
                min_length=resolved.get("minLength"),
                max_length=resolved.get("maxLength"),
                nullable=nullable,
                ui_hidden=bool(raw.get("ui_hidden", False)),
            )
        )

    return StrategyInfoOut(
        kind=definition.kind,
        name=definition.name,
        description=definition.description,
        supports_backtest=definition.supports_backtest,
        params=params,
    )


def validate_strategy_params_for_api(
    kind: str,
    params: dict[str, Any],
    *,
    require_backtest: bool = False,
) -> dict[str, Any]:
    """校验参数并返回可安全写入 JSON 的规范化结果。"""
    try:
        definition = get_strategy_definition(kind)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["strategy"],
                    "msg": str(exc),
                    "type": "value_error.strategy",
                }
            ],
        ) from exc

    if require_backtest and not definition.supports_backtest:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["strategy"],
                    "msg": "该策略依赖实时时钟事件,当前回测引擎尚不支持",
                    "type": "value_error.unsupported_backtest",
                }
            ],
        )

    try:
        validated = definition.params_model.model_validate(params)
    except ValidationError as exc:
        detail = [
            {
                "loc": ["params", *error["loc"]],
                "msg": error["msg"],
                "type": error["type"],
            }
            for error in exc.errors(include_url=False)
        ]
        raise HTTPException(status_code=422, detail=detail) from exc

    return validated.model_dump(mode="json")
