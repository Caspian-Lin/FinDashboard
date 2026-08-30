"""研究沙箱 Docker E2E —— 容器级加固验收(issue #216)。

默认跳过;显式开启::

    FINBOARD_SANDBOX_E2E=1 uv run pytest tests/integration/test_research_sandbox_docker_e2e.py -v

前置:Docker Desktop 运行 + 镜像已构建(仓库根)::

    docker build -f docker/research-sandbox/Dockerfile -t finboard-research-sandbox:0.1.0 .

镜像 tag 可经 ``FINBOARD_SANDBOX_IMAGE`` 覆盖。

验收(对齐 issue AC):样例因子与内置参照一致(数值容差)、断网
(socket 连接失败)、只读(写挂载路径失败、输出仅出现在 /out)、超时
kill、OOM kill、PIT(挂载清单不含 decision_at 之后的数据)。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_sandbox.data_mount import build_data_mount
from finboard_backtest.research_sandbox.runner import (
    ResearchSandboxRunner,
    SandboxRunSpec,
    SubprocessDockerDriver,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("FINBOARD_SANDBOX_E2E") != "1",
        reason="Docker 沙箱 E2E 默认跳过(FINBOARD_SANDBOX_E2E=1 开启)",
    ),
]

_IMAGE = os.getenv("FINBOARD_SANDBOX_IMAGE", "finboard-research-sandbox:0.1.0")
_DECISION_AT = datetime(2024, 6, 3, 7, 0, tzinfo=UTC)
_SYMBOLS = ["600000.SH", "000001.SZ"]


@dataclass
class _Kind:
    value: str


@dataclass
class _Inst:
    code: str
    market: str = "SH"


@dataclass
class _Sym:
    code: str


@dataclass
class _Bar:
    symbol: _Sym
    timestamp: datetime
    open: float = 9.0
    high: float = 11.0
    low: float = 8.0
    close: float = 10.0
    volume: float = 100.0
    amount: float = 1000.0


@dataclass
class _PITBar:
    code: str
    day: date
    close: float

    @property
    def bar(self) -> _Bar:
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=datetime(self.day.year, self.day.month, self.day.day),
            close=self.close,
        )


@dataclass
class _Release:
    release_id: str
    dataset_kind: _Kind
    instruments: list[_Inst]
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)


@dataclass
class _StubProvider:
    release: _Release
    bars: list[_PITBar]

    async def fetch_point_in_time_bars(
        self, symbol, period, start, end, *, decision_at, adjust="qfq"
    ):
        return [
            b for b in self.bars
            if b.code == symbol.code and b.day <= decision_at.date()
        ]


def _mount_provider() -> tuple[_StubProvider, pd.DataFrame]:
    closes = {
        "600000.SH": [10.0 + i * 0.25 for i in range(20)],
        "000001.SZ": [20.0 - i * 0.1 for i in range(20)],
    }
    days = [date(2024, 5, d) for d in range(1, 21)]
    bars = [
        _PITBar(code=s, day=d, close=c)
        for s, series in closes.items()
        for d, c in zip(days, series, strict=True)
    ]
    provider = _StubProvider(
        release=_Release(
            "DR-e2e", _Kind("bars"), [_Inst(s) for s in _SYMBOLS]
        ),
        bars=bars,
    )
    frame = pd.DataFrame(
        [
            {"symbol": b.code, "close": b.close}
            for b in sorted(bars, key=lambda x: (x.code, x.day))
        ]
    )
    return provider, frame


async def _build_mount(tmp_path: Path) -> pd.DataFrame:
    provider, frame = _mount_provider()
    await build_data_mount(
        providers=[provider],
        decision_at=_DECISION_AT,
        out_root=tmp_path / "data",
    )
    return frame


_MOMENTUM = '''
import pandas as pd


def compute(ctx):
    scores = {}
    for symbol in ctx.symbols:
        closes = ctx.bars_for(symbol)["close"].to_numpy()
        scores[symbol] = float(closes[-1] / closes[0] - 1.0)
    return pd.Series(scores)
'''


def _make_code(root: Path, source: str) -> Path:
    code = root / "code"
    code.mkdir(parents=True, exist_ok=True)
    (code / "factor.py").write_text(source, encoding="utf-8")
    (code / "manifest.toml").write_text(
        '[manifest]\nentry = "factor.compute"\n', encoding="utf-8"
    )
    return code


def _spec(tmp_path: Path, **overrides: Any) -> SandboxRunSpec:
    defaults: dict[str, Any] = {
        "image": _IMAGE,
        "code_dir": tmp_path / "code",
        "data_dir": tmp_path / "data",
        "out_dir": tmp_path / "out",
        "timeout_seconds": 120.0,
        "memory_mb": 1024,
        "cpus": 2.0,
        "pids_limit": 256,
    }
    defaults.update(overrides)
    return SandboxRunSpec(**defaults)


async def _run(tmp_path: Path, **overrides):
    return await ResearchSandboxRunner(SubprocessDockerDriver()).run(
        _spec(tmp_path, **overrides)
    )


class TestSandboxE2E:
    async def test_sample_factor_matches_reference(self, tmp_path: Path) -> None:
        bars = await _build_mount(tmp_path)
        _make_code(tmp_path, _MOMENTUM)
        result = await _run(tmp_path)
        assert result.exit_code == 0, result.stderr
        scores = pd.read_parquet(tmp_path / "out" / "scores.parquet")
        for row in scores.itertuples(index=False):
            closes = bars[bars["symbol"] == row.symbol]["close"].to_numpy()
            expected = closes[-1] / closes[0] - 1.0
            assert row.score == pytest.approx(float(expected), rel=1e-9)
        metrics = json.loads(
            (tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
        )
        assert metrics["coverage"] == 1.0
        assert metrics["kit_version"]  # 镜像 tag 与 kit 版本绑定,自报留档

    async def test_container_output_publishes_as_snapshot(self, tmp_path: Path) -> None:
        """issue #217:真实容器输出 → 质量门 → 快照构造(run 锚定)全接缝。"""

        from finboard_backtest.research_sandbox.executor import _load_scores
        from finboard_backtest.research_sandbox.factor_publish import (
            build_factor_snapshot,
            check_output_quality,
        )

        await _build_mount(tmp_path)
        _make_code(tmp_path, _MOMENTUM)
        result = await _run(tmp_path)
        assert result.exit_code == 0, result.stderr

        scores = _load_scores(tmp_path / "out" / "scores.parquet")
        manifest_path = tmp_path / "data" / "mount_manifest.json"
        mount_checksum = hashlib.sha256(
            manifest_path.read_bytes()
        ).hexdigest()
        quality = check_output_quality(
            scores, universe_size=len(_SYMBOLS),
            max_nan_ratio=0.5, min_coverage=0.5,
        )
        assert quality.passed, quality.failures
        snapshot = build_factor_snapshot(
            factor_artifact_name="mom20",
            run_id="RCR-e2e000000000000001",
            decision_at=_DECISION_AT,
            commit="a" * 40,
            mount_manifest_checksum=mount_checksum,
            scores=scores,
            quality=quality,
        )
        assert snapshot.source_run_id == "RCR-e2e000000000000001"
        assert snapshot.dataset_release_id is None
        assert snapshot.code_version == "a" * 40
        # 观测 = 容器 scores 的有限值,因子名带 u_ 前缀
        assert {o.feature_name for o in snapshot.observations} == {"u_mom20"}
        assert {o.symbol for o in snapshot.observations} == set(scores)
        by_symbol = {o.symbol: o.value for o in snapshot.observations}
        for symbol, value in scores.items():
            assert by_symbol[symbol] == pytest.approx(value, rel=1e-12)

    async def test_pit_manifest_no_future_files(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path)
        manifest = json.loads(
            (tmp_path / "data" / "mount_manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["decision_at"] == _DECISION_AT.isoformat()
        for dataset in manifest["datasets"]:
            assert dataset["max_data_date"] <= _DECISION_AT.date().isoformat()
        bars = pq.read_table(tmp_path / "data" / "bars.parquet")
        max_day = max(d.isoformat() for d in bars.column("date").to_pylist())
        assert max_day <= _DECISION_AT.date().isoformat()

    async def test_network_denied(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path)
        _make_code(
            tmp_path,
            f"import socket\n\n\ndef compute(ctx):\n"
            f"    socket.create_connection({('1.1.1.1', 53)!r}, timeout=5)\n"
            f"    return {{s: 0.0 for s in ctx.symbols}}\n",
        )
        result = await _run(tmp_path)
        assert result.exit_code == 4  # harness:runtime_error
        error = json.loads(
            (tmp_path / "out" / "error.json").read_text(encoding="utf-8")
        )
        assert error["error_code"] == "runtime_error"
        traceback_text = error.get("traceback", "")
        assert isinstance(traceback_text, str)
        assert "socket" in traceback_text

    async def test_readonly_mounts(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path)
        _make_code(
            tmp_path,
            "def compute(ctx):\n"
            "    with open('/data/evil.txt', 'w') as fh:\n"
            "        fh.write('x')\n"
            "    return {s: 0.0 for s in ctx.symbols}\n",
        )
        result = await _run(tmp_path)
        assert result.exit_code == 4
        error = json.loads(
            (tmp_path / "out" / "error.json").read_text(encoding="utf-8")
        )
        assert "Read-only" in error["message"] or "Errno 30" in error["message"]
        # 输出仅出现在 /out(宿主 out 目录),数据面无污染
        assert not (tmp_path / "data" / "evil.txt").exists()
        assert (tmp_path / "out" / "error.json").exists()

    async def test_timeout_killed(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path)
        _make_code(
            tmp_path,
            "import time\n\n\ndef compute(ctx):\n"
            "    time.sleep(600)\n"
            "    return {s: 0.0 for s in ctx.symbols}\n",
        )
        started = time.monotonic()
        result = await _run(tmp_path, timeout_seconds=15.0)
        assert result.timed_out
        assert time.monotonic() - started < 90
        assert result.usage  # 资源用量已采样归档

    async def test_oom_killed(self, tmp_path: Path) -> None:
        await _build_mount(tmp_path)
        _make_code(
            tmp_path,
            "def compute(ctx):\n"
            "    chunks = []\n"
            "    for _ in range(64):\n"
            "        chunks.append(bytearray(64 * 1024 * 1024))\n"
            "    return {s: 0.0 for s in ctx.symbols}\n",
        )
        result = await _run(tmp_path, memory_mb=512, timeout_seconds=180.0)
        assert result.oom_killed or result.exit_code == 137
