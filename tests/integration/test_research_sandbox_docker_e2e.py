"""研究沙箱 Docker E2E —— 容器级加固验收(issue #216)。

默认跳过;显式开启::

    FINBOARD_SANDBOX_E2E=1 uv run pytest tests/integration/test_research_sandbox_docker_e2e.py -v

前置:Docker Desktop 运行 + 镜像已构建(仓库根)::

    docker build -f docker/research-sandbox/Dockerfile -t finboard-research-sandbox:0.3.1 .

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
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pandas as pd
import pyarrow.parquet as pq
import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
    stable_checksum,
)
from finboard_backtest.research_sandbox.data_mount import (
    build_data_mount,
    build_window_data_mount,
)
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

_IMAGE = os.getenv("FINBOARD_SANDBOX_IMAGE", "finboard-research-sandbox:0.3.1")
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
    # issue #359:窗口挂载(v3)读取逐行 available_at;缺省按 D1 语义
    # 派生为业务日 15:30 UTC(FrozenReleaseProvider 的确定性规则)。
    available_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.available_at is None:
            self.available_at = datetime(
                self.day.year, self.day.month, self.day.day, 15, 30,
                tzinfo=UTC,
            )

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


def _make_code(root: Path, source: str, entry: str = "factor.compute") -> Path:
    code = root / "code"
    code.mkdir(parents=True, exist_ok=True)
    (code / "factor.py").write_text(source, encoding="utf-8")
    (code / "manifest.toml").write_text(
        f'[manifest]\nentry = "{entry}"\n', encoding="utf-8"
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


# ---- 策略协议 E2E(issue #218)------------------------------------------------


_MEAN_REVERSION_ZSCORE = '''
import numpy as np


def decide(ctx):
    """布林带式均值回归:20 日 zscore 超卖入场,回归带内持有(权重回显)。"""
    window = int(ctx.params.get("zscore_window", 20))
    entry_z = float(ctx.params.get("entry_z", 2.0))
    exit_z = float(ctx.params.get("exit_z", 0.5))
    cap = float(ctx.constraints.max_weight_per_asset)
    held = dict(ctx.current_weights)
    targets = {}
    for symbol in ctx.symbols:
        closes = ctx.bars_for(symbol)["close"].to_numpy()
        if len(closes) < window:
            continue
        seg = closes[-window:]
        std = float(np.std(seg))
        if std <= 0.0:
            continue
        z = float((seg[-1] - float(np.mean(seg))) / std)
        if z <= -entry_z:
            targets[symbol] = cap
        elif symbol in held and z < exit_z:
            targets[symbol] = float(held[symbol])
    return targets
'''


class TestStrategyDecideContainer:
    """单容器 strategy.decide:数值与本地参照一致 + 权重回显进决策。"""

    async def test_mean_reversion_decide_matches_reference(
        self, tmp_path: Path
    ) -> None:
        import numpy as np

        provider, frame = _mount_provider()
        # 000001.SZ 单调下行(z 高度负)→ 超卖入场;600000.SH 上行 → 不持有。
        await build_data_mount(
            providers=[provider],
            decision_at=_DECISION_AT,
            out_root=tmp_path / "data",
            current_weights={"000001.SZ": 0.3},
            strategy_constraints={
                "max_weight_per_asset": 0.2,
                "long_only": True,
                "max_gross_exposure": 1.0,
                "min_cash_buffer": 0.05,
            },
        )
        code = tmp_path / "code"
        code.mkdir(parents=True, exist_ok=True)
        (code / "strategy.py").write_text(_MEAN_REVERSION_ZSCORE, encoding="utf-8")
        (code / "manifest.toml").write_text(
            '[manifest]\nentry = "strategy.decide"\n\n[manifest.params]\n'
            "zscore_window = 20\nentry_z = 2.0\nexit_z = 0.5\n",
            encoding="utf-8",
        )
        result = await _run(tmp_path, mode="strategy")
        assert result.exit_code == 0, result.stderr

        targets = pd.read_parquet(tmp_path / "out" / "targets.parquet")
        weights = dict(zip(targets["symbol"], targets["weight"], strict=True))
        closes_b = frame[frame["symbol"] == "000001.SZ"]["close"].to_numpy()
        seg = closes_b[-20:]
        z_b = (seg[-1] - seg.mean()) / np.std(seg)
        # 线性温和下行:z 落在 (-entry_z, exit_z) 回归带内 → 持有分支,
        # 目标 = 引擎回显的当前权重(跨日路径依赖由此覆盖)。
        assert -2.0 < z_b < 0.5
        assert weights.get("600000.SH") is None  # 上行且未持有 → 无目标
        assert weights.get("000001.SZ") == pytest.approx(0.3)  # 权重回显
        metrics = json.loads(
            (tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
        )
        assert metrics["mode"] == "strategy"
        assert metrics["n_positive"] == 1
        assert metrics["gross_exposure"] == pytest.approx(0.3)


@dataclass
class _Provider4Strategy:
    """signal_engine 决策加载 + data_mount 挂载双面可用的 stub 发布。"""

    symbols: tuple[str, ...]
    days: list[date]
    closes: dict[str, dict[date, float]]
    release: Any = None

    def __post_init__(self) -> None:
        from tests.integration.test_research_run_signal_engine_worker import (
            _StubInstrument,
            _StubRelease,
        )

        self.release = _StubRelease(
            "frozen-release-multi",
            tuple(_StubInstrument(code=s) for s in self.symbols),
            start_date=self.days[0],
            end_date=self.days[-1],
        )

    def _pit_bars(self, symbol: str, decision_at: datetime):
        # PIT 门控:只返回 decision_at 之前的行(data_mount 还有二层防线)。
        cutoff = decision_at.date()
        return [
            _PITBarTz(code=symbol, day=day, close=self.closes[symbol][day])
            for day in self.days
            if day <= cutoff and day in self.closes[symbol]
        ]

    async def fetch_point_in_time_bars(
        self, symbol, period, start, end, *, decision_at, adjust="qfq"
    ):
        return self._pit_bars(symbol.code, decision_at)

    async def fetch_point_in_time_prices(
        self, symbol, period, start, end, *, decision_at, adjust="qfq"
    ):
        # signal_engine 价格特征消费 worker 模块的 PIT 价格形状。
        from tests.integration.test_research_run_signal_engine_worker import (
            _StubPointInTimePrice,
        )

        return [
            _StubPointInTimePrice(
                timestamp=datetime(d.year, d.month, d.day, tzinfo=UTC),
                close=Decimal(str(self.closes[symbol.code][d])),
                available_at=datetime(d.year, d.month, d.day, tzinfo=UTC),
            )
            for d in self.days
            if d <= end and d in self.closes[symbol.code]
        ]

    async def fetch_bars(self, symbol, period, start, end, *, adjust="qfq"):
        # 交易日历推断消费 worker 模块的轻量 Bar 形状(close + timestamp)。
        from tests.integration.test_research_run_signal_engine_worker import (
            _StubBar,
        )

        return [
            _StubBar(
                close=Decimal(str(self.closes[symbol.code][d])),
                timestamp=datetime(d.year, d.month, d.day, tzinfo=UTC),
            )
            for d in self.days
            if d <= end and d in self.closes[symbol.code]
        ]


@dataclass
class _PITBarTz:
    """tz-aware PIT bar(data_mount OHLCV 列 + 价格特征 observed_at 均要求带时区)。"""

    code: str
    day: date
    close: float

    @property
    def bar(self) -> _Bar:
        ts = datetime(self.day.year, self.day.month, self.day.day, tzinfo=UTC)
        return _Bar(
            symbol=_Sym(self.code),
            timestamp=ts,
            open=self.close * 0.995,
            high=self.close * 1.01,
            low=self.close * 0.99,
            close=self.close,
            volume=10000.0,
            amount=self.close * 10000.0,
        )


def _group_by_month(days: list[date]) -> dict[int, list[date]]:
    out: dict[int, list[date]] = {}
    for day in days:
        out.setdefault(day.month, []).append(day)
    return out


def _user_code_e2e_manifest(commit: str) -> ResearchRunManifest:
    import hashlib
    from decimal import Decimal

    from finboard_backtest.research_run.contracts import ResearchRunManifest
    from finboard_backtest.strategy_spec import build_strategy_template
    from finboard_backtest.strategy_spec.contracts import (
        FeatureGraph,
        SignalRules,
        StrategyCodeArtifactRef,
    )

    spec = build_strategy_template(
        "multi_factor",
        strategy_id="user_code_e2e",
        dataset_release_ids=("frozen-release-multi",),
    ).model_copy(
        update={
            "strategy_kind": "user_code",
            "feature_graph": FeatureGraph(nodes=(), outputs=()),
            "signal_rules": SignalRules(rules=()),
            "code_artifact": StrategyCodeArtifactRef(
                name="mean_reversion_zscore", commit=commit
            ),
        }
    )
    digest = hashlib.sha256(b"user-code-e2e").hexdigest()[:24]
    return ResearchRunManifest(
        run_id=f"RR-{digest}",
        idempotency_key="user-code-e2e",
        strategy_spec=spec,
        strategy_spec_checksum=stable_checksum(spec.canonical_payload()),
        dataset_releases=(
            FrozenArtifactRef(
                artifact_id="frozen-release-multi",
                version="v1",
                checksum="a" * 64,
                capabilities=("stock",),
            ),
        ),
        parameters={"rebalance_frequency": "monthly"},
        code_version="abcdef0123456789",
        initial_capital=Decimal("200000"),
        requested_by="agent:e2e",
    )


class TestUserCodeStrategyMultiPeriodE2E:
    """验收里程碑:agent 提交的均值回归策略经真实容器在 multi_period 跑通。

    真实链路:git 提交(#215)→ StrategySandboxCaller(每决策日一个真实
    一次性容器,PIT 挂载 + 权重回显)→ #91 组合管线 → Coordinator →
    完整 report(含 sandbox_provenance 与 equity_curve)。
    """

    async def test_agent_mean_reversion_full_run(self, tmp_path: Path) -> None:
        from types import SimpleNamespace

        from finboard_backtest.research_code import ResearchCodeService
        from finboard_backtest.research_run.contracts import ResearchRunStatus
        from finboard_backtest.research_run.runner import (
            ResearchRunCoordinator,
        )
        from finboard_backtest.research_run.store import InMemoryResearchRunStore
        from finboard_backtest.research_run.user_code_engine import (
            UserCodeStrategyAdapter,
        )
        from tests.integration.test_research_run_signal_engine_worker import (
            _multi_period_calendar,
        )

        # 1. agent 提交策略代码到真实 bare git 仓库。
        repo_path = tmp_path / "research_code.git"
        service = ResearchCodeService.from_path(str(repo_path))
        submitted = service.submit(
            kind="strategy",
            name="mean_reversion_zscore",
            files={
                "strategy.py": _MEAN_REVERSION_ZSCORE,
                "manifest.toml": (
                    '[manifest]\nentry = "strategy.decide"\n\n[manifest.params]\n'
                    "zscore_window = 20\nentry_z = 2.0\nexit_z = 0.5\n"
                ),
            },
            author="agent:e2e",
        )

        # 2. 多期价格序列:每月末一部分标的连跌(≥4 个 z <= -2,风险贡献可行)。
        symbols = ("A.SH", "B.SH", "C.SH", "D.SH", "E.SH", "F.SH")
        days = _multi_period_calendar(date(2024, 1, 1), date(2024, 4, 30))
        month_days = _group_by_month(days)
        tail_by_month = {
            month: set(chunk[-4:]) for month, chunk in month_days.items()
        }
        closes: dict[str, dict[date, float]] = {s: {} for s in symbols}
        price = dict.fromkeys(symbols, 10.0)
        for i, day in enumerate(days):
            tail = day in tail_by_month[day.month]
            for index, symbol in enumerate(symbols):
                drift = 1.0 + 0.0005 * ((i * 7 + index * 13) % 5 - 2) / 4
                if tail:
                    # 轮换:每月末让 4 个标的急跌(其余微涨),保证入场数 >= 4。
                    dropped = (index + day.month) % 3 != 0
                    price[symbol] *= drift * (0.965 if dropped else 1.001)
                else:
                    price[symbol] *= drift
                closes[symbol][day] = round(price[symbol], 4)

        provider = _Provider4Strategy(symbols, days, closes)
        manifest = _user_code_e2e_manifest(submitted["commit"])

        settings = SimpleNamespace(
            research_sandbox_enabled=True,
            research_sandbox_image=_IMAGE,
            research_sandbox_docker_bin="docker",
            research_sandbox_timeout_seconds=120.0,
            research_sandbox_memory_mb=1024,
            research_sandbox_cpus=2.0,
            research_sandbox_pids_limit=256,
            research_sandbox_user="65532",
            research_sandbox_workspace_root=str(tmp_path / "ws"),
            research_code_repo_path=str(repo_path),
            research_code_max_files=32,
            research_code_max_file_bytes=262144,
        )

        def release_factory(release_id: str):
            assert release_id == "frozen-release-multi"
            return provider

        async def snapshot_provider(snapshot_id: str):
            return None

        adapter = UserCodeStrategyAdapter(
            manifest=manifest,
            release_provider_factory=release_factory,
            snapshot_provider=snapshot_provider,
            settings_factory=lambda: settings,
        )
        store = InMemoryResearchRunStore()
        record = await ResearchRunCoordinator(store).execute(manifest, adapter)

        assert record.status is ResearchRunStatus.COMPLETED, record.error_summary
        report = record.result
        assert report is not None
        assert report.execution_mode.value == "multi_period"
        assert report.decision_count >= 3
        assert report.equity_curve
        provenance = cast(
            dict[str, Any],
            cast(object, report.sandbox_provenance),
        )
        assert provenance is not None
        assert provenance["commit"] == submitted["commit"]
        assert provenance["image_digest"].startswith("sha256:")
        assert provenance["image"] == _IMAGE
        assert len(provenance["decisions"]) == report.decision_count
        assert all(item["targets_checksum"] for item in provenance["decisions"])

        # decide 逐决策 workspace 留档(代码 staging + 每决策 data/out)。
        ws = tmp_path / "ws" / manifest.run_id
        assert (ws / "code" / "strategy.py").exists()
        decide_dirs = [d for d in ws.iterdir() if d.name.startswith("D")]
        assert len(decide_dirs) == report.decision_count


# ---- 因子区间协议 E2E(issue #359 协议;#374 Arrow harness 容器级验收)--------


_SERIES_MOMENTUM = '''
def compute_series(ctx):
    values = {}
    for day in ctx.dates:
        view = ctx.bars_view(day)
        cross = {}
        for symbol in ctx.symbols:
            closes = view.for_symbol(symbol)["close"].to_numpy()
            cross[symbol] = (
                float(closes[-1] / closes[-3] - 1.0) if len(closes) >= 3 else None
            )
        values[day] = cross
    return values
'''


class TestFactorSeriesSandboxE2E:
    """``--mode factor_series`` 端到端:窗口挂载 v3 + 容器内 Arrow 数据面。

    进程内等值覆盖见 ``tests/unit/research_sandbox/test_issue_374_kit_arrow.py``;
    这里验证真实镜像(0.3.1 起 harness 装配为 Arrow 常驻 + 按需截面)产出
    canonical ``factor_series.json`` 且逐日值与进程内参照一致。
    """

    async def test_compute_series_container_end_to_end(self, tmp_path: Path) -> None:
        provider, _ = _mount_provider()
        window_dates = [date(2024, 5, 18), date(2024, 5, 19), date(2024, 5, 20)]
        await build_window_data_mount(
            providers=[provider],
            window_start=window_dates[0],
            window_end=window_dates[-1],
            dates=window_dates,
            out_root=tmp_path / "data",
            code_artifact="mom_series",
            code_commit="a" * 40,
            release_id="DR-e2e",
            dataset_release_ids=[],
        )
        _make_code(tmp_path, _SERIES_MOMENTUM, entry="factor.compute_series")
        result = await _run(tmp_path, mode="factor_series")
        assert result.exit_code == 0, result.stderr
        payload = json.loads(
            (tmp_path / "out" / "factor_series.json").read_text(encoding="utf-8")
        )
        assert payload["protocol_version"] == 2
        assert payload["code_artifact"] == "mom_series"
        assert payload["release_id"] == "DR-e2e"
        assert payload["dates"] == [d.isoformat() for d in window_dates]
        # 逐日 PIT:决策日 D 的截面 = available_at <= D 日终的收盘前缀动量
        for symbol in _SYMBOLS:
            symbol_bars = sorted(
                (b for b in provider.bars if b.code == symbol),
                key=lambda b: b.day,
            )
            for day in window_dates:
                prefix = [b.close for b in symbol_bars if b.day <= day]
                expected = prefix[-1] / prefix[-3] - 1.0
                got = payload["values"][day.isoformat()][symbol]
                assert got == pytest.approx(expected, rel=1e-9)
        metrics = json.loads(
            (tmp_path / "out" / "metrics.json").read_text(encoding="utf-8")
        )
        assert metrics["mode"] == "factor_series"
        assert metrics["protocol_version"] == 2
        if os.getenv("FINBOARD_SANDBOX_IMAGE") is None:
            # 默认镜像 tag 与 kit 版本绑定:0.3.1 起容器内为 Arrow 数据面
            assert metrics["kit_version"] == "0.3.1"
