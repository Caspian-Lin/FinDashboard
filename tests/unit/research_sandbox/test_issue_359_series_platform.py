"""issue #359 平台侧单元测试 —— 静态校验白名单 / 序列质量门 /
factor_series payload 与失败分类 / run_factor_series_container 契约。

沙箱开关与容器链路依赖 DB + Docker,由集成与 E2E 门控测试覆盖;此处覆盖
纯函数面与注入了假 driver / 假 provider 工厂 / 显式 workspace 的
``run_factor_series_container`` 全链路(无需真实 Docker,对齐既有
test_runner / test_executor 的假件风格)。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from finboard_backtest.research_code.validation import validate_submission
from finboard_backtest.research_sandbox.errors import (
    OUTPUT_CONTRACT_VIOLATION,
    SandboxError,
)
from finboard_backtest.research_sandbox.factor_publish import (
    check_series_quality,
)
from finboard_backtest.research_sandbox.runner import (
    FactorSeriesRunSpec,
    run_factor_series_container,
)

# --------------------------------------------------------------------------- #
# 静态校验:factor.compute_series 入口签名白名单
# --------------------------------------------------------------------------- #


class TestValidationSeriesEntry:
    def test_compute_series_manifest_accepted(self) -> None:
        issues = validate_submission(
            kind="factor",
            name="mom_series",
            files={
                "factor.py": (
                    "def compute_series(ctx):\n    return {}\n"
                ),
                "manifest.toml": '[manifest]\nentry = "factor.compute_series"\n',
            },
        )
        assert issues == []

    def test_compute_v1_still_accepted(self) -> None:
        issues = validate_submission(
            kind="factor",
            name="mom20",
            files={
                "factor.py": "def compute(ctx):\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "factor.compute"\n',
            },
        )
        assert issues == []

    def test_unknown_factor_func_rejected(self) -> None:
        issues = validate_submission(
            kind="factor",
            name="mom",
            files={
                "factor.py": "def compute_all(ctx):\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "factor.compute_all"\n',
            },
        )
        assert any(i.code == "manifest_entry_mismatch" for i in issues)

    def test_compute_series_signature_checked(self) -> None:
        """声明 compute_series 时入口签名按该函数校验(缺参/*args 拒绝)。"""
        issues = validate_submission(
            kind="factor",
            name="mom_series",
            files={
                "factor.py": "def compute_series():\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "factor.compute_series"\n',
            },
        )
        assert any(i.code == "entry_signature" for i in issues)
        issues = validate_submission(
            kind="factor",
            name="mom_series",
            files={
                "factor.py": "def compute_series(*args):\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "factor.compute_series"\n',
            },
        )
        assert any(i.code == "entry_signature" for i in issues)

    def test_strategy_entry_unchanged(self) -> None:
        issues = validate_submission(
            kind="strategy",
            name="strat",
            files={
                "strategy.py": "def decide(ctx):\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "strategy.decide"\n',
            },
        )
        assert issues == []
        issues = validate_submission(
            kind="strategy",
            name="strat",
            files={
                "strategy.py": "def compute_series(ctx):\n    return {}\n",
                "manifest.toml": '[manifest]\nentry = "strategy.compute_series"\n',
            },
        )
        assert any(i.code == "manifest_entry_mismatch" for i in issues)


# --------------------------------------------------------------------------- #
# 序列质量门(逐日截面口径;阈值复用 research_sandbox_*)
# --------------------------------------------------------------------------- #


_D = [date(2024, 6, 3), date(2024, 6, 4), date(2024, 6, 5)]
_U = ("600000.SH", "000001.SZ")


class TestSeriesQuality:
    def test_passes_full_coverage(self) -> None:
        values = {
            d: dict.fromkeys(_U, 1.0) for d in _D
        }
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.5, min_coverage=0.5
        )
        assert report.passed
        assert report.coverage == 1.0
        assert report.nan_ratio == 0.0
        assert report.worst_day_nan_ratio == 0.0
        assert report.as_dict()["thresholds"] == {
            "max_nan_ratio": 0.5,
            "min_coverage": 0.5,
        }

    def test_fails_on_aggregate_nan_ratio(self) -> None:
        values = {
            d: {"600000.SH": float("nan"), "000001.SZ": float("nan")}
            for d in _D
        }
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.5, min_coverage=0.5
        )
        assert not report.passed
        assert report.nan_ratio == pytest.approx(1.0)
        assert any("整窗 NaN 比例" in f for f in report.failures)

    def test_fails_on_worst_day_nan_ratio(self) -> None:
        values: dict[date, dict[str, float | None]] = {
            date(2024, 6, 3): {"600000.SH": 1.0, "000001.SZ": 1.0},
            date(2024, 6, 4): {"600000.SH": None, "000001.SZ": None},
            date(2024, 6, 5): {"600000.SH": 1.0, "000001.SZ": 1.0},
        }
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.5, min_coverage=0.5
        )
        assert not report.passed
        assert report.worst_day == date(2024, 6, 4)
        assert report.worst_day_nan_ratio == 1.0
        assert any("最差决策日" in f for f in report.failures)

    def test_fails_on_coverage(self) -> None:
        values = {d: {"600000.SH": 1.0, "000001.SZ": None} for d in _D}
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.9, min_coverage=0.8
        )
        assert not report.passed
        assert report.coverage == pytest.approx(0.5)
        assert any("覆盖率" in f for f in report.failures)

    def test_empty_output_rejected(self) -> None:
        values: dict[date, dict[str, float | None]] = {}
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.5, min_coverage=0.5
        )
        assert not report.passed
        assert any("区间输出为空" in f for f in report.failures)

    def test_inf_counts_as_non_finite(self) -> None:
        values = {
            date(2024, 6, 3): {"600000.SH": float("inf"), "000001.SZ": 1.0},
            date(2024, 6, 4): {"600000.SH": 1.0, "000001.SZ": 1.0},
            date(2024, 6, 5): {"600000.SH": 1.0, "000001.SZ": 1.0},
        }
        report = check_series_quality(
            values, dates=_D, universe=_U, max_nan_ratio=0.5, min_coverage=0.5
        )
        assert report.passed  # 1/6 有限性缺口 ≤ 0.5
        assert report.nan_ratio == pytest.approx(1 / 6)


# --------------------------------------------------------------------------- #
# run_factor_series_container(假 driver / 假 provider 工厂 / 显式 workspace)
# --------------------------------------------------------------------------- #


_COMMIT = "b" * 40


def _code_files(entry: str = "factor.compute_series") -> dict[str, str]:
    return {
        "factor.py": (
            "def compute_series(ctx):\n"
            "    return {d: {s: 1.0 for s in ctx.symbols} for d in ctx.dates}\n"
        ),
        "manifest.toml": f'[manifest]\nentry = "{entry}"\n',
    }


@dataclass
class _SeriesDriver:
    """假 DockerDriver:执行 = 直接写出 canonical factor_series.json。"""

    digest_value: str = "sha256:series"
    argv_seen: list[str] = field(default_factory=list)
    mode_seen: str = ""
    fail: str = ""  # "":成功;"exit3"/"exit4"/"timeout"/"oom"/"no_output"
    _polls: int = 0

    async def image_digest(self, image: str) -> str:
        return self.digest_value

    async def run_detached(self, argv: list[str]) -> str:
        self.argv_seen = list(argv)
        idx = argv.index("--mode")
        self.mode_seen = argv[idx + 1]
        # 容器 /out 对应宿主机挂载源(对齐真实 mount 语义)
        mount_arg = next(a for a in argv if "target=/out" in a)
        out_dir = Path(mount_arg.split("source=", 1)[1].split(",", 1)[0])
        out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240 -- 测试假件写宿主机本地盘
        if self.fail == "exit3":
            (out_dir / "error.json").write_text(
                json.dumps({"error_code": "output_contract_violation",
                            "message": "日期集不一致"}),
                encoding="utf-8",
            )
            return "cid"
        if self.fail == "exit4":
            (out_dir / "error.json").write_text(
                json.dumps({"error_code": "runtime_error",
                            "message": "ValueError: boom"}),
                encoding="utf-8",
            )
            return "cid"
        if self.fail in {"timeout", "oom", "no_output"}:
            return "cid"
        payload = {
            "protocol_version": 2,
            "code_artifact": "mom_series",
            "code_commit": _COMMIT,
            "kind": "factor",
            "release_id": "DR-bars",
            "dataset_release_ids": ["DR-bars"],
            "params": {"window": 2},
            "window_start": "2024-06-01",
            "window_end": "2024-06-03",
            "dates": [d.isoformat() for d in (date(2024, 6, 2), date(2024, 6, 3))],
            "values": {
                "2024-06-02": {"600000.SH": 1.0},
                "2024-06-03": {"600000.SH": None},
            },
        }
        (out_dir / "factor_series.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (out_dir / "metrics.json").write_text(
            json.dumps({"coverage": 0.5, "nan_ratio": 0.5}), encoding="utf-8"
        )
        return "cid"

    async def inspect_state(self, container: str) -> dict[str, Any]:
        import asyncio

        if self.fail == "timeout":
            # 前两次轮询保持 Running,让 runner 的超时判定真实触发
            self._polls += 1
            if self._polls <= 2:
                await asyncio.sleep(0.04)
                return {"Running": True}
            return {"Running": False, "ExitCode": None, "OOMKilled": False}
        if self.fail == "oom":
            return {"Running": False, "ExitCode": 137, "OOMKilled": True}
        return {"Running": False, "ExitCode": {"exit3": 3, "exit4": 4}.get(self.fail, 0),
                "OOMKilled": False}

    async def stats(self, container: str) -> dict[str, Any] | None:
        return None

    async def logs(self, container: str) -> tuple[str, str]:
        return "out", "err"

    async def kill(self, container: str) -> None:
        pass

    async def rm(self, container: str) -> None:
        pass


@dataclass
class _SeriesRelease:
    release_id: str
    dataset_kind: Any
    instruments: list[Any] = field(default_factory=list)
    period: str = "1d"
    adjustment: str = "qfq"
    start_date: date = date(2024, 1, 1)
    end_date: date = date(2024, 12, 31)


@dataclass
class _WindowProvider:
    release: _SeriesRelease

    async def fetch_point_in_time_bars(self, symbol, period, start, end, *,
                                       decision_at, adjust="qfq"):
        from tests.unit.research_sandbox.test_data_mount import (
            _PITBar,
        )

        return [] if self.release.instruments == [] else [
            _PITBar(code=symbol.code, day=date(2024, 6, 2), close=10.0)
        ]

    async def fetch_daily_metrics(self, symbol, *, start, end, decision_at):
        return []

    async def fetch_financial_indicators(self, symbol, *, decision_at):
        return []


def _provider_factory(release_id: str) -> _WindowProvider:
    from tests.unit.research_sandbox.test_data_mount import _Inst, _Kind

    return _WindowProvider(
        release=_SeriesRelease(
            release_id=release_id, dataset_kind=_Kind("bars"),
            instruments=[_Inst("600000.SH")],
        )
    )


def _series_settings(tmp_path: Path) -> Any:
    class S:
        research_sandbox_enabled = True
        research_sandbox_image = "finboard-research-sandbox:0.3.0"
        research_sandbox_docker_bin = "docker"
        research_sandbox_workspace_root = str(tmp_path / "ws")
        research_sandbox_timeout_seconds = 60.0
        research_sandbox_memory_mb = 1024
        research_sandbox_cpus = 1.0
        research_sandbox_pids_limit = 64
        research_sandbox_user = "65532"
        research_sandbox_max_nan_ratio = 0.5
        research_sandbox_min_coverage = 0.5
        research_code_repo_path = ""
        research_code_max_files = 32
        research_code_max_file_bytes = 256 * 1024

    return S()


class _FakeCodeService:
    """research_code 解析桩:绕过 git 仓库(执行链路由集成测试覆盖)。"""

    def exists(self, *, kind: str, name: str, commit: str) -> bool:
        return True

    def read(self, *, kind: str, name: str, commit: str) -> dict[str, str]:
        return _code_files()


def _spec(**overrides: Any) -> FactorSeriesRunSpec:
    defaults: dict[str, Any] = {
        "code_artifact": "mom_series",
        "code_commit": _COMMIT,
        "release_id": "DR-bars",
        "dataset_release_ids": ("DR-bars",),
        "params": {"window": 2},
        "window_start": date(2024, 6, 1),
        "window_end": date(2024, 6, 3),
        "dates": (date(2024, 6, 2), date(2024, 6, 3)),
    }
    defaults.update(overrides)
    return FactorSeriesRunSpec(**defaults)


async def _run(tmp_path: Path, driver: _SeriesDriver, **kwargs: Any):
    settings = _series_settings(tmp_path)
    if driver.fail == "timeout":
        # 让 runner 的墙钟超时判定在测试时窗内真实触发
        settings.research_sandbox_timeout_seconds = 0.05
    return await run_factor_series_container(
        _spec(**kwargs),
        settings=settings,
        driver=driver,
        release_provider_factory=_provider_factory,
        workspace_root=tmp_path / "ws",
    )


class TestRunFactorSeriesContainer:
    async def test_end_to_end_happy_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver()
        output = await _run(tmp_path, driver)
        assert driver.mode_seen == "factor_series"
        assert output.code_commit == _COMMIT
        assert output.release_id == "DR-bars"
        assert output.dates == (date(2024, 6, 2), date(2024, 6, 3))
        assert output.values[date(2024, 6, 2)]["600000.SH"] == 1.0
        assert output.values[date(2024, 6, 3)]["600000.SH"] is None
        assert output.quality.coverage == pytest.approx(0.5)
        assert output.series_checksum
        assert output.mount_manifest_checksum
        assert output.image == "finboard-research-sandbox:0.3.0"
        # workspace 归档
        ws = output.workspace_dir
        assert (ws / "out" / "factor_series.json").exists()
        assert (ws / "container.json").exists()
        manifest = json.loads(
            (ws / "data" / "mount_manifest.json").read_text(encoding="utf-8")
        )
        assert manifest["version"] == 3

    async def test_output_contract_violation_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver(fail="exit3")
        with pytest.raises(SandboxError) as exc_info:
            await _run(tmp_path, driver)
        assert exc_info.value.code == OUTPUT_CONTRACT_VIOLATION
        assert "日期集" in exc_info.value.summary

    async def test_runtime_error_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver(fail="exit4")
        with pytest.raises(SandboxError) as exc_info:
            await _run(tmp_path, driver)
        assert exc_info.value.code == "runtime_error"

    async def test_timeout_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver(fail="timeout")
        with pytest.raises(SandboxError) as exc_info:
            await _run(tmp_path, driver)
        assert exc_info.value.code == "timeout"

    async def test_oom_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver(fail="oom")
        with pytest.raises(SandboxError) as exc_info:
            await _run(tmp_path, driver)
        assert exc_info.value.code == "oom_killed"

    async def test_missing_output_raises(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "finboard_backtest.research_sandbox.runner._resolve_series_code",
            _fake_resolve,
        )
        driver = _SeriesDriver(fail="no_output")
        with pytest.raises(SandboxError) as exc_info:
            await _run(tmp_path, driver)
        assert exc_info.value.code == OUTPUT_CONTRACT_VIOLATION
        assert "factor_series.json" in exc_info.value.summary

    async def test_spec_validation_dates_outside_window(self, tmp_path: Path) -> None:
        driver = _SeriesDriver()
        with pytest.raises(SandboxError, match="窗口外决策日"):
            await _run(
                tmp_path,
                driver,
                dates=(date(2024, 6, 30),),
                window_start=date(2024, 6, 1),
                window_end=date(2024, 6, 3),
            )

    async def test_spec_validation_empty_dates(self, tmp_path: Path) -> None:
        driver = _SeriesDriver()
        with pytest.raises(SandboxError, match="dates 不能为空"):
            await _run(tmp_path, driver, dates=())

    async def test_disabled_sandbox_raises(self, tmp_path: Path) -> None:
        settings = _series_settings(tmp_path)
        settings.research_sandbox_enabled = False
        with pytest.raises(SandboxError, match="research_sandbox_enabled"):
            await run_factor_series_container(
                _spec(),
                settings=settings,
                driver=_SeriesDriver(),
                release_provider_factory=_provider_factory,
                workspace_root=tmp_path / "ws",
            )


async def _fake_resolve(settings: Any, spec: FactorSeriesRunSpec) -> dict[str, str]:
    return _code_files()
