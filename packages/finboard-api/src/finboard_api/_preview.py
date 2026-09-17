"""只读数据预览辅助(数据页可观测性)。

数据缓存与冻结发布的 parquet 尾部行预览:纯读、无写路径、不连 broker。
预览直接用 pyarrow 读列式尾部行,不走 Bar 对象构造(预览只需要人看)。
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

# 标的代码形如 000001.SZ / 510300.SH / IF2506.CFFEX;同时挡住路径分隔符注入。
_SYMBOL_PATTERN = re.compile(r"^[0-9A-Za-z]{1,24}(\.[0-9A-Za-z]{1,12})?$")

MAX_PREVIEW_LIMIT = 200


def validate_preview_symbol(symbol: str) -> str:
    """校验预览标的代码格式,失败抛 ValueError。"""

    normalized = symbol.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError(f"非法标的代码: {symbol!r}(应为 形如 000001.SZ 的代码)")
    return normalized


def jsonify_preview_value(value: Any) -> Any:
    """把 pyarrow 行值转成 JSON 安全标量(Decimal→str、时间→ISO、NaN→null)。"""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (str, int, bool)):
        return value
    return str(value)


def read_parquet_tail(path: Path, limit: int) -> tuple[list[str], list[dict[str, Any]], int]:
    """读取 parquet 尾部 limit 行,返回 (columns, rows, total_rows)。同步阻塞,调用方须放线程。"""

    import pyarrow.parquet as pq

    table = pq.read_table(path)
    total = table.num_rows
    start = max(0, total - max(1, min(limit, MAX_PREVIEW_LIMIT)))
    tail = table.slice(start, total - start)
    columns = list(tail.column_names)
    rows = [
        {column: jsonify_preview_value(record.get(column)) for column in columns}
        for record in tail.to_pylist()
    ]
    return columns, rows, total
