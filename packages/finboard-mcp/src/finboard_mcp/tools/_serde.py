"""工具产物的 JSON 兼容序列化。

研究域对象(dataclass / datetime / Decimal / Enum)经 ``asdict`` + ``json.dumps``
归一为 JSON 兼容结构,避免 MCP 序列化层对复杂类型的兼容性问题。
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


def to_jsonable(obj: Any) -> Any:
    """把领域对象归一为 JSON 兼容结构(dict / list / str / number / bool / None)。"""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        obj = dataclasses.asdict(obj)
    return json.loads(json.dumps(obj, default=_default, ensure_ascii=False))


def _default(o: Any) -> Any:
    if isinstance(o, (datetime, date)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, Enum):
        return o.value
    raise TypeError(f"不可 JSON 序列化的类型: {type(o).__name__}")


__all__ = ["to_jsonable"]
