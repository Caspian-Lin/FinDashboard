"""沙箱 harness —— 容器内的执行入口(issue #216)。

``python -m finboard_research_kit.harness`` 由服务端 ``ResearchSandboxRunner``
作为容器 command 启动,职责:

1. 读 ``/data/mount_manifest.json``(服务端 data_mount 写出的挂载清单)与
   各 parquet,装配 :class:`FactorContext`(PIT 由挂载内容物理保证);
2. 读 ``/code/manifest.toml`` 的 ``manifest.entry``(如 ``factor.compute``),
   以 ``/code`` 为首位的 sys.path 导入入口模块并调用入口函数;
3. 规范化输出(见 ``result``),写 ``/out/scores.parquet`` 与
   ``/out/metrics.json``(coverage / nan_ratio / 耗时 / kit_version)。

退出码协议(服务端按此分类失败):

* 0 —— 成功,scores + metrics 已落盘;
* 3 —— 输出契约不符(/out/error.json 附 ``output_contract_violation``);
* 4 —— 因子运行时异常(/out/error.json 附 ``runtime_error`` + traceback)。

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
from finboard_research_kit.context import FactorContext
from finboard_research_kit.result import (
    OutputContractError,
    normalize_result,
    result_metrics,
)

EXIT_OK = 0
EXIT_OUTPUT_CONTRACT = 3
EXIT_RUNTIME = 4

_MOUNT_MANIFEST = "mount_manifest.json"
_SCORES = "scores.parquet"
_METRICS = "metrics.json"
_ERROR = "error.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finboard_research_kit.harness")
    parser.add_argument("--code-dir", default="/code")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--out-dir", default="/out")
    args = parser.parse_args(argv)

    code_dir = Path(args.code_dir)
    data_dir = Path(args.data_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        ctx = build_context(code_dir=code_dir, data_dir=data_dir)
        entry = _read_entry(code_dir)
        scores = _invoke(code_dir=code_dir, entry=entry, ctx=ctx)
        normalized = normalize_result(scores)
        _check_universe(normalized, ctx.symbols)
        metrics = {
            **result_metrics(normalized, universe=ctx.symbols),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "entry": entry,
            "kit_version": __version__,
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


def _invoke(*, code_dir: Path, entry: str, ctx: FactorContext) -> Any:
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
