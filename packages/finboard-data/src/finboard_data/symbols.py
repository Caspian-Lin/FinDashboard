"""标的池配置 —— 用于定时批量拉取行情数据。

配置文件格式(YAML)::

    fetch_symbols:
      - code: "510300.SH"
        name: "沪深300ETF"
      - code: "510050.SH"
        name: "上证50ETF"
    fetch_period: "D1"
    fetch_lookback_days: 5
    fetch_adjust: "qfq"

也可以用 JSON 格式(扩展名 ``.json``)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class SymbolEntry:
    """标的池中的一条记录。"""

    code: str
    name: str = ""


@dataclass
class SymbolPoolConfig:
    """标的池配置。"""

    symbols: list[SymbolEntry] = field(default_factory=list)
    fetch_period: str = "D1"
    fetch_lookback_days: int = 5
    fetch_adjust: str = "qfq"


def load_symbol_pool(path: str | Path) -> SymbolPoolConfig:
    """从 YAML / JSON 文件加载标的池配置。

    文件不存在时返回空配置(便于首次运行)。
    """
    p = Path(path)
    if not p.exists():
        logger.warning("symbol_pool.not_found", path=str(p))
        return SymbolPoolConfig()

    text = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        import yaml

        data = yaml.safe_load(text)
    elif p.suffix == ".json":
        data = json.loads(text)
    else:
        raise ValueError(f"不支持的配置文件格式: {p.suffix}")

    if data is None:
        return SymbolPoolConfig()

    raw_symbols = data.get("fetch_symbols", [])
    symbols = [
        SymbolEntry(
            code=item["code"],
            name=item.get("name", ""),
        )
        for item in raw_symbols
    ]
    return SymbolPoolConfig(
        symbols=symbols,
        fetch_period=data.get("fetch_period", "D1"),
        fetch_lookback_days=data.get("fetch_lookback_days", 5),
        fetch_adjust=data.get("fetch_adjust", "qfq"),
    )


def save_symbol_pool(config: SymbolPoolConfig, path: str | Path) -> None:
    """保存标的池配置为 YAML / JSON(按扩展名判断)。"""
    p = Path(path)
    data = {
        "fetch_symbols": [
            {"code": s.code, "name": s.name} for s in config.symbols
        ],
        "fetch_period": config.fetch_period,
        "fetch_lookback_days": config.fetch_lookback_days,
        "fetch_adjust": config.fetch_adjust,
    }
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix in (".yaml", ".yml"):
        import yaml

        p.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")
    elif p.suffix == ".json":
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    else:
        raise ValueError(f"不支持的配置文件格式: {p.suffix}")
