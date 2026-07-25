"""标的池配置加载/保存测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from finboard_data.symbols import (
    SymbolEntry,
    SymbolPoolConfig,
    load_symbol_pool,
    save_symbol_pool,
)


class TestSymbolPoolConfig:
    def test_load_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "symbols.yaml"
        f.write_text(
            """
fetch_symbols:
  - code: "510300.SH"
    name: "沪深300ETF"
  - code: "510050.SH"
    name: "上证50ETF"
fetch_period: "D1"
fetch_lookback_days: 10
fetch_adjust: "hqfq"
""",
            encoding="utf-8",
        )
        config = load_symbol_pool(f)
        assert len(config.symbols) == 2
        assert config.symbols[0].code == "510300.SH"
        assert config.symbols[0].name == "沪深300ETF"
        assert config.fetch_period == "D1"
        assert config.fetch_lookback_days == 10
        assert config.fetch_adjust == "hqfq"

    def test_load_json(self, tmp_path: Path) -> None:
        f = tmp_path / "symbols.json"
        f.write_text(
            json.dumps(
                {
                    "fetch_symbols": [
                        {"code": "510300.SH", "name": "沪深300ETF"},
                    ],
                    "fetch_period": "D1",
                }
            ),
            encoding="utf-8",
        )
        config = load_symbol_pool(f)
        assert len(config.symbols) == 1
        assert config.symbols[0].code == "510300.SH"

    def test_load_nonexistent_returns_empty(self, tmp_path: Path) -> None:
        config = load_symbol_pool(tmp_path / "nonexistent.yaml")
        assert config.symbols == []
        assert config.fetch_period == "D1"

    def test_load_empty_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "empty.yaml"
        f.write_text("", encoding="utf-8")
        config = load_symbol_pool(f)
        assert config.symbols == []

    def test_load_unsupported_format(self, tmp_path: Path) -> None:
        f = tmp_path / "symbols.txt"
        f.write_text("dummy", encoding="utf-8")
        with pytest.raises(ValueError, match="不支持的配置文件格式"):
            load_symbol_pool(f)

    def test_save_and_reload_yaml(self, tmp_path: Path) -> None:
        f = tmp_path / "symbols.yaml"
        config = SymbolPoolConfig(
            symbols=[
                SymbolEntry(code="510300.SH", name="沪深300ETF"),
                SymbolEntry(code="510050.SH", name="上证50ETF"),
            ],
            fetch_period="D1",
            fetch_lookback_days=7,
            fetch_adjust="qfq",
        )
        save_symbol_pool(config, f)
        loaded = load_symbol_pool(f)
        assert len(loaded.symbols) == 2
        assert loaded.symbols[0].code == "510300.SH"
        assert loaded.fetch_lookback_days == 7

    def test_save_json(self, tmp_path: Path) -> None:
        f = tmp_path / "symbols.json"
        config = SymbolPoolConfig(
            symbols=[SymbolEntry(code="510300.SH")],
        )
        save_symbol_pool(config, f)
        loaded = load_symbol_pool(f)
        assert len(loaded.symbols) == 1
