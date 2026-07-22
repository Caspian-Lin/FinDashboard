"""策略 YAML 配置加载。

格式::

    strategies:
      - kind: periodic_query
        id: query-01
      - kind: etf_dca
        id: dca-510300
        params:
          symbol_code: "510300.SH"
          quantity: 100
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class StrategyConfig:
    """单个策略配置条目。"""

    kind: str
    id: str
    params: dict[str, Any]


def load_strategy_configs(path: str | Path) -> list[StrategyConfig]:
    """从 YAML 文件加载策略配置列表。"""
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if data is None:
        return []
    raw = data.get("strategies", [])
    return [
        StrategyConfig(
            kind=item["kind"],
            id=item["id"],
            params=item.get("params", {}),
        )
        for item in raw
    ]
