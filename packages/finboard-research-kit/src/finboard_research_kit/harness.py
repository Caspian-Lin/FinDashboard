"""沙箱 harness —— 容器内的执行入口(issue #216;#218 增 strategy 模式)。

``python -m finboard_research_kit.harness`` 由服务端 ``ResearchSandboxRunner``
作为容器 command 启动,职责:

1. 读 ``/data/mount_manifest.json``(服务端 data_mount 写出的挂载清单)与
   各 parquet,按 ``--mode`` 装配 :class:`FactorContext` 或
   :class:`StrategyContext`(PIT 由挂载内容物理保证);
2. 读 ``/code/manifest.toml`` 的 ``manifest.entry``(``factor.compute`` /
   ``strategy.decide``),按文件路径导入入口模块并调用入口函数;
3. 规范化输出(见 ``result``):factor 模式写 ``/out/scores.parquet``,
   strategy 模式写 ``/out/targets.parquet``;两者都写
   ``/out/metrics.json``(耗时 / kit_version / 模式专属指标)。

退出码协议(服务端按此分类失败):

* 0 —— 成功,输出 + metrics 已落盘;
* 3 —— 输出契约不符(/out/error.json 附 ``output_contract_violation``);
* 4 —— 用户代码运行时异常(/out/error.json 附 ``runtime_error`` + traceback)。

超时 / OOM 由外部 ``docker kill`` 终止,不会有 error.json —— 服务端按容器
State(OOMKilled / 137 / kill 时序)分类。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pandas as pd

from finboard_research_kit import __version__
from finboard_research_kit.context import FactorContext, StrategyConstraints, StrategyContext
from finboard_research_kit.result import (
    OutputContractError,
    normalize_result,
    normalize_strategy_result,
    result_metrics,
    strategy_metrics,
)

EXIT_OK = 0
EXIT_OUTPUT_CONTRACT = 3
EXIT_RUNTIME = 4

MODE_FACTOR = "factor"
MODE_STRATEGY = "strategy"

_MOUNT_MANIFEST = "mount_manifest.json"
_SCORES = "scores.parquet"
_TARGETS = "targets.parquet"
_METRICS = "metrics.json"
_ERROR = "error.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finboard_research_kit.harness")
    parser.add_argument("--code-dir", default="/code")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--out-dir", default="/out")
    parser.add_argument(
        "--mode",
        choices=(MODE_FACTOR, MODE_STRATEGY),
        default=MODE_FACTOR,
        help="执行协议:factor.compute(scores)/ strategy.decide(targets)",
    )
    args = parser.parse_args(argv)

    code_dir = Path(args.code_dir)
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        entry = _read_entry(code_dir)
        if args.mode == MODE_STRATEGY:
            return _run_strategy(
                code_dir=code_dir,
                data_dir=data_dir,
                out_dir=out_dir,
                entry=entry,
                started=started,
            )
        ctx = build_context(code_dir=code_dir, data_dir=data_dir)
        raw = _invoke(code_dir=code_dir, entry=entry, ctx=ctx)
        normalized = normalize_result(raw)
        _check_universe(normalized, ctx.symbols)
        metrics = {
            **result_metrics(normalized, universe=ctx.symbols),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "entry": entry,
            "kit_version": __version__,
            "mode": MODE_FACTOR,
        }
        frame = normalized.rename("score").rename_axis("symbol").reset_index()
        frame.to_parquet(out_dir / _SCORES, index=False)
        (out_dir / _METRICS).write_text(
            json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return EXIT_OK
    except OutputContractError as exc:
        _write_error(out_dir, "output_contract_violation", str(exc))
        return EXIT_OUTPUT_CONTRACT
    except BaseException as exc:
        _write_error(
            out_dir,
            "runtime_error",
            f"{type(exc).__name__}: {exc}",
            traceback.format_exc(),
        )
        return EXIT_RUNTIME


def _run_strategy(
    *,
    code_dir: Path,
    data_dir: Path,
    out_dir: Path,
    entry: str,
    started: float,
) -> int:
    """strategy.decide(ctx) → targets.parquet(issue #218 逐日决策函数)。"""
    ctx = build_strategy_context(code_dir=code_dir, data_dir=data_dir)
    raw = _invoke(code_dir=code_dir, entry=entry, ctx=ctx)
    targets, meta = normalize_strategy_result(raw)
    _check_universe(targets, ctx.symbols)
    metrics = {
        **strategy_metrics(targets, universe=ctx.symbols, meta=meta),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "entry": entry,
        "kit_version": __version__,
        "mode": MODE_STRATEGY,
    }
    frame = targets.rename("weight").rename_axis("symbol").reset_index()
    frame.to_parquet(out_dir / _TARGETS, index=False)
    (out_dir / _METRICS).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return EXIT_OK


def build_context(*, code_dir: Path, data_dir: Path) -> FactorContext:
    """从挂载目录装配 FactorContext(不读任何挂载清单之外的数据)。"""
    manifest = json.loads(
        (data_dir / _MOUNT_MANIFEST).read_text(encoding="utf-8")
    )
    decision_at = datetime.fromisoformat(manifest["decision_at"])
    bars = _read_frame(data_dir / "bars.parquet")
    if bars is None:
        raise OutputContractError("挂载缺少 bars.parquet(数据面不完整)")
    daily = _read_frame(data_dir / "daily_metrics.parquet")
    fin = _read_frame(data_dir / "financial_indicators.parquet")
    params = _merge_params(code_dir, manifest)
    return FactorContext(
        decision_at=decision_at,
        symbols=tuple(manifest.get("symbols", ())),
        bars=bars,
        daily_metrics=daily,
        financial_indicators=fin,
        params=params,
    )


def build_strategy_context(*, code_dir: Path, data_dir: Path) -> StrategyContext:
    """从挂载目录装配 StrategyContext(挂载清单 v2 增权重回显与约束视图)。"""
    manifest = json.loads(
        (data_dir / _MOUNT_MANIFEST).read_text(encoding="utf-8")
    )
    decision_at = datetime.fromisoformat(manifest["decision_at"])
    bars = _read_frame(data_dir / "bars.parquet")
    if bars is None:
        raise OutputContractError("挂载缺少 bars.parquet(数据面不完整)")
    daily = _read_frame(data_dir / "daily_metrics.parquet")
    fin = _read_frame(data_dir / "financial_indicators.parquet")
    raw_weights = manifest.get("current_weights") or {}
    current_weights = pd.Series(
        {str(k): float(v) for k, v in raw_weights.items()}, dtype="float64"
    )
    raw_constraints = manifest.get("strategy_constraints") or {}
    constraints = StrategyConstraints(
        max_weight_per_asset=float(
            raw_constraints.get("max_weight_per_asset", 1.0)
        ),
        long_only=bool(raw_constraints.get("long_only", True)),
        max_gross_exposure=float(
            raw_constraints.get("max_gross_exposure", 1.0)
        ),
        min_cash_buffer=float(raw_constraints.get("min_cash_buffer", 0.0)),
    )
    params = _merge_params(code_dir, manifest)
    return StrategyContext(
        decision_at=decision_at,
        symbols=tuple(manifest.get("symbols", ())),
        bars=bars,
        daily_metrics=daily,
        financial_indicators=fin,
        current_weights=current_weights,
        constraints=constraints,
        params=params,
    )


def _read_frame(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"])
    return frame


def _merge_params(code_dir: Path, mount_manifest: dict[str, Any]) -> Any:
    """运行参数优先级:run params(服务端注入 params.json)> manifest.params。"""
    merged: dict[str, Any] = {}
    manifest_toml = code_dir / "manifest.toml"
    if manifest_toml.exists():
        import tomllib

        doc = tomllib.loads(manifest_toml.read_text(encoding="utf-8"))
        top = doc.get("manifest", doc)
        if isinstance(top.get("params"), dict):
            merged.update(top["params"])
    injected = code_dir / "params.json"
    if injected.exists():
        merged.update(json.loads(injected.read_text(encoding="utf-8")))
    return MappingProxyType(merged)


def _read_entry(code_dir: Path) -> str:
    import tomllib

    doc = tomllib.loads((code_dir / "manifest.toml").read_text(encoding="utf-8"))
    top = doc.get("manifest", doc)
    entry = top.get("entry")
    if not isinstance(entry, str) or "." not in entry:
        raise OutputContractError(
            f"manifest.toml 缺少合法 manifest.entry(如 'factor.compute'): {entry!r}"
        )
    return entry


def _invoke(*, code_dir: Path, entry: str, ctx: FactorContext | StrategyContext) -> Any:
    """按文件路径加载入口模块并调用入口函数。

    不走 ``sys.path`` + ``importlib.import_module``:同进程多次运行时
    ``sys.modules`` 会返回上一次的缓存模块(跨运行污染);spec_from_file_location
    每次重新加载,且不在解释器留下可复用的模块状态。
    """
    module_name, _, func_name = entry.partition(".")
    source = code_dir / f"{module_name}.py"
    if not source.exists():
        raise OutputContractError(
            f"入口模块文件不存在: {source}(entry={entry})"
        )
    spec = importlib.util.spec_from_file_location(
        f"_sandbox_entry_{module_name}", source
    )
    if spec is None or spec.loader is None:
        raise OutputContractError(f"无法加载入口模块: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise OutputContractError(
            f"入口 {entry} 不是可调用函数(module={module_name})"
        )
    return func(ctx)


def _check_universe(scores: pd.Series, universe: tuple[str, ...]) -> None:
    allowed = set(universe)
    if not allowed:
        return
    outside = [str(s) for s in scores.index if str(s) not in allowed]
    if outside:
        raise OutputContractError(
            f"scores 含候选池外 symbol({len(outside)} 个,如 {outside[:5]});"
            f"候选池大小 {len(allowed)}"
        )


def _write_error(
    out_dir: Path, code: str, message: str, tb: str | None = None
) -> None:
    payload: dict[str, Any] = {"error_code": code, "message": message[:4000]}
    if tb:
        payload["traceback"] = tb[:16000]
    with contextlib.suppress(OSError):
        (out_dir / _ERROR).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    sys.exit(main())
