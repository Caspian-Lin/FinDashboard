"""沙箱 harness —— 容器内的执行入口(issue #216;#218 增 strategy 模式;
#359 增 factor_series 区间模式)。

``python -m finboard_research_kit.harness`` 由服务端 ``ResearchSandboxRunner``
作为容器 command 启动,职责:

1. 读 ``/data/mount_manifest.json``(服务端 data_mount 写出的挂载清单)与
   各 parquet,按 ``--mode`` 装配 :class:`FactorContext` /
   :class:`StrategyContext` / :class:`FactorSeriesContext`(v1 单日两模式的
   PIT 由挂载内容物理保证;v2 区间模式的挂载覆盖整个窗口,逐日 PIT 由
   ``bars_view`` / ``dataset_view`` 访问器契约承担);
2. 读 ``/code/manifest.toml`` 的 ``manifest.entry``(``factor.compute`` /
   ``factor.compute_series`` / ``strategy.decide``),按文件路径导入入口
   模块并调用入口函数;factor_series 模式下 manifest 声明
   ``factor.compute_series`` 优先,缺失时回退 ``factor.compute``(v1 因子
   逐日重放,双轨);
3. 规范化输出(见 ``result``):factor 模式写 ``/out/scores.parquet``,
   strategy 模式写 ``/out/targets.parquet``,factor_series 模式写
   ``/out/factor_series.json``(canonical JSON,结构钉死供下游存储消费);
   三者都写 ``/out/metrics.json``(耗时 / kit_version / 模式专属指标)。

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
import math
import sys
import time
import traceback
from datetime import date, datetime
from functools import partial
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

import pandas as pd

from finboard_research_kit import __version__
from finboard_research_kit.context import (
    FactorContext,
    FactorSeriesContext,
    StrategyConstraints,
    StrategyContext,
    end_of_day,
)
from finboard_research_kit.result import (
    FactorSeries,
    OutputContractError,
    normalize_result,
    normalize_series_result,
    normalize_strategy_result,
    result_metrics,
    series_metrics,
    strategy_metrics,
)

if TYPE_CHECKING:
    import pyarrow as pa

EXIT_OK = 0
EXIT_OUTPUT_CONTRACT = 3
EXIT_RUNTIME = 4

MODE_FACTOR = "factor"
MODE_STRATEGY = "strategy"
MODE_FACTOR_SERIES = "factor_series"

_MOUNT_MANIFEST = "mount_manifest.json"
_SCORES = "scores.parquet"
_TARGETS = "targets.parquet"
_FACTOR_SERIES = "factor_series.json"
_METRICS = "metrics.json"
_ERROR = "error.json"

#: 协议 v2 的 canonical JSON 版本(issue #359 钉死供下游存储消费)
PROTOCOL_VERSION = 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="finboard_research_kit.harness")
    parser.add_argument("--code-dir", default="/code")
    parser.add_argument("--data-dir", default="/data")
    parser.add_argument("--out-dir", default="/out")
    parser.add_argument(
        "--mode",
        choices=(MODE_FACTOR, MODE_STRATEGY, MODE_FACTOR_SERIES),
        default=MODE_FACTOR,
        help=(
            "执行协议:factor.compute(scores)/ strategy.decide(targets)/ "
            "factor.compute_series(区间序列,挂载清单 v3)"
        ),
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
        if args.mode == MODE_FACTOR_SERIES:
            return _run_factor_series(
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


def _run_factor_series(
    *,
    code_dir: Path,
    data_dir: Path,
    out_dir: Path,
    entry: str,
    started: float,
) -> int:
    """factor.compute_series(ctx) → factor_series.json(协议 v2,issue #359)。

    入口解析:manifest 声明 ``factor.compute_series`` 优先;缺失回退
    ``factor.compute``(v1 因子逐日重放 —— 每个决策日构造单日 FactorContext
    调用一次 compute,汇成序列,双轨完整保留)。
    """
    manifest = _read_mount_manifest(data_dir)
    ctx = build_series_context(code_dir=code_dir, data_dir=data_dir, manifest=manifest)
    if entry.endswith(".compute_series"):
        raw = _invoke(code_dir=code_dir, entry=entry, ctx=ctx)
        series = normalize_series_result(raw, expected_dates=ctx.dates, universe=ctx.symbols)
        entry_used = entry
    elif entry.endswith(".compute"):
        series = _invoke_v1_per_day(code_dir=code_dir, entry=entry, ctx=ctx)
        entry_used = f"{entry}(v1 逐日回退)"
    else:
        raise OutputContractError(
            f"factor_series 模式的 manifest.entry 须为 factor.compute_series "
            f"或 factor.compute,收到 {entry!r}"
        )
    metrics = {
        **series_metrics(series, universe=ctx.symbols),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "entry": entry,
        "entry_used": entry_used,
        "kit_version": __version__,
        "mode": MODE_FACTOR_SERIES,
        "protocol_version": PROTOCOL_VERSION,
    }
    payload = _series_payload(manifest=manifest, series=series, ctx=ctx)
    (out_dir / _FACTOR_SERIES).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    (out_dir / _METRICS).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return EXIT_OK


def _invoke_v1_per_day(*, code_dir: Path, entry: str, ctx: FactorSeriesContext) -> FactorSeries:
    """v1 回退:逐决策日构造单日 FactorContext 调用 ``compute``。

    每日数据面 = 该日的 PIT 视图(``available_at <= t`` 日终),与 v1 单日
    容器的挂载内容逐值等值 —— v1 因子不改一行代码即可产序列。

    数据集经 ``*_factory`` 惰性构造(issue #378):因子不触碰的数据集
    当日完全不物化(此前逐日急切物化曾让只用 bars 的因子也被 daily_metrics
    的 705 万行前缀帧撞穿容器限额)。
    """
    values: dict[date, dict[str, float | None]] = {}
    for day in ctx.dates:
        day_ctx = FactorContext(
            decision_at=end_of_day(day),
            symbols=ctx.symbols,
            bars_factory=partial(_series_bars_day_frame, ctx, day),
            daily_metrics_factory=partial(_series_dataset_day_frame, ctx, "daily_metrics", day),
            financial_indicators_factory=partial(
                _series_dataset_day_frame, ctx, "financial_indicators", day
            ),
            params=ctx.params,
        )
        raw = _invoke(code_dir=code_dir, entry=entry, ctx=day_ctx)
        normalized = normalize_result(raw)
        _check_universe(normalized, ctx.symbols)
        values[day] = {
            str(symbol): float(value) if pd.notna(value) else None
            for symbol, value in normalized.items()
        }
    return normalize_series_result(values, expected_dates=ctx.dates, universe=ctx.symbols)


def _series_bars_day_frame(series_ctx: FactorSeriesContext, day: date) -> pd.DataFrame:
    """v1 回退的当日可见行情帧(factory 形态,首次访问才物化)。"""
    return series_ctx.bars_view(day).frame


def _series_dataset_day_frame(
    series_ctx: FactorSeriesContext, kind: str, day: date
) -> pd.DataFrame | None:
    """v1 回退的当日可见研究数据集帧(factory 形态,首次访问才物化)。"""
    return series_ctx.dataset_view(kind, day).frame


def _read_mount_manifest(data_dir: Path) -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads((data_dir / _MOUNT_MANIFEST).read_text(encoding="utf-8"))
    if int(manifest.get("version", 0)) < 3:
        raise OutputContractError(
            f"factor_series 模式要求挂载清单 v3(窗口语义),收到 version={manifest.get('version')!r}"
        )
    return manifest


def build_series_context(
    *, code_dir: Path, data_dir: Path, manifest: dict[str, Any]
) -> FactorSeriesContext:
    """从挂载清单 v3 + parquet 装配 :class:`FactorSeriesContext`。

    数据面以 **Arrow 常驻 + 按需截面** 装配(issue #374):parquet 直接读
    为 pyarrow 表(raw 内存,无 pandas 对象税),``as_of`` 过滤在 Arrow
    compute 内完成,仅当次访问的可见行物化为 pandas —— 容器内存不随窗口
    长度 x 标的数整表放大(705 万行 daily_metrics 整表进 pandas 曾以
    ~2GB 撞穿容器限额)。挂载内容与读取语义逐值不变(与 0.3.0 的
    ``pd.read_parquet`` 读出逐值一致)。

    惰性读取(issue #378):数据集经 ``*_loader`` 构造,**首次被访问才读
    parquet** —— 只用 bars 的因子不为未触碰的研究数据集付任何常驻成本。
    读取走 ``memory_map``(文件页支撑,cgroup 内存压力下可回收而非直接
    OOM kill),``pre_buffer=False`` 避免「先整文件读进内存」短路 mmap。
    """
    window = manifest.get("window") or {}
    dates = tuple(date.fromisoformat(str(d)) for d in window.get("dates", ()))
    if not dates:
        raise OutputContractError("挂载清单 v3 缺少 window.dates(窗口内决策日序列)")
    bars_path = data_dir / "bars.parquet"
    if not bars_path.exists():
        raise OutputContractError("挂载缺少 bars.parquet(数据面不完整)")
    daily_path = data_dir / "daily_metrics.parquet"
    fin_path = data_dir / "financial_indicators.parquet"
    params = _merge_params(code_dir, manifest)
    return FactorSeriesContext(
        dates=dates,
        symbols=tuple(manifest.get("symbols", ())),
        bars_loader=partial(_read_mount_table, bars_path),
        daily_metrics_loader=partial(_read_mount_table_optional, daily_path),
        financial_indicators_loader=partial(_read_mount_table_optional, fin_path),
        params=params,
    )


def _read_mount_table(path: Path) -> pa.Table:
    """挂载 parquet → Arrow 表(mmap 文件页支撑,cgroup 压力下可回收)。"""
    import pyarrow.parquet as pq

    return pq.read_table(path, memory_map=True, pre_buffer=False)


def _read_mount_table_optional(path: Path) -> pa.Table | None:
    """文件缺失返回 None(该数据集未挂载),存在则 mmap 读表。"""
    return _read_mount_table(path) if path.exists() else None


def _series_payload(
    *,
    manifest: dict[str, Any],
    series: FactorSeries,
    ctx: FactorSeriesContext,
) -> dict[str, Any]:
    """canonical ``factor_series.json``(结构钉死供下游存储消费)。

    非有限值(NaN/inf)序列化为 null —— canonical JSON 保持严格 JSON
    语法,缺测语义由 null 承担(质量门按非有限口径计数)。
    """
    window = manifest.get("window") or {}

    def cross(day: date) -> dict[str, float | None]:
        return {
            symbol: (value if value is not None and math.isfinite(value) else None)
            for symbol, value in sorted(series.values.get(day, {}).items())
        }

    return {
        "protocol_version": PROTOCOL_VERSION,
        "code_artifact": str(manifest.get("code_artifact", "")),
        "code_commit": str(manifest.get("code_commit", "")),
        "kind": "factor",
        "release_id": str(manifest.get("release_id", "")),
        "dataset_release_ids": [str(rid) for rid in manifest.get("dataset_release_ids", ())],
        "params": dict(ctx.params),
        "window_start": str(window.get("window_start", "")),
        "window_end": str(window.get("window_end", "")),
        "dates": [d.isoformat() for d in series.dates],
        "values": {d.isoformat(): cross(d) for d in series.dates},
    }


def _json_default(value: Any) -> Any:
    """metrics 序列化兜错:date/datetime/Decimal 等转可读字符串。"""
    if isinstance(value, date | datetime):
        return value.isoformat()
    if isinstance(value, MappingProxyType):
        return dict(value)
    return str(value)


def build_context(*, code_dir: Path, data_dir: Path) -> FactorContext:
    """从挂载目录装配 FactorContext(不读任何挂载清单之外的数据)。

    数据集经 ``*_factory`` 惰性构造(issue #378):因子不触碰的数据集
    不读盘、不物化;bars 缺失仍构造期 fail-closed。
    """
    manifest = json.loads((data_dir / _MOUNT_MANIFEST).read_text(encoding="utf-8"))
    decision_at = datetime.fromisoformat(manifest["decision_at"])
    if not (data_dir / "bars.parquet").exists():
        raise OutputContractError("挂载缺少 bars.parquet(数据面不完整)")
    params = _merge_params(code_dir, manifest)
    return FactorContext(
        decision_at=decision_at,
        symbols=tuple(manifest.get("symbols", ())),
        bars_factory=partial(_read_frame, data_dir / "bars.parquet"),
        daily_metrics_factory=partial(_read_frame, data_dir / "daily_metrics.parquet"),
        financial_indicators_factory=partial(
            _read_frame, data_dir / "financial_indicators.parquet"
        ),
        params=params,
    )


def build_strategy_context(*, code_dir: Path, data_dir: Path) -> StrategyContext:
    """从挂载目录装配 StrategyContext(挂载清单 v2 增权重回显与约束视图)。"""
    manifest = json.loads((data_dir / _MOUNT_MANIFEST).read_text(encoding="utf-8"))
    decision_at = datetime.fromisoformat(manifest["decision_at"])
    bars = _read_frame(data_dir / "bars.parquet")
    if bars is None:
        raise OutputContractError("挂载缺少 bars.parquet(数据面不完整)")
    daily = _read_frame(data_dir / "daily_metrics.parquet")
    fin = _read_frame(data_dir / "financial_indicators.parquet")
    raw_weights = manifest.get("current_weights") or {}
    current_weights = pd.Series({str(k): float(v) for k, v in raw_weights.items()}, dtype="float64")
    raw_constraints = manifest.get("strategy_constraints") or {}
    constraints = StrategyConstraints(
        max_weight_per_asset=float(raw_constraints.get("max_weight_per_asset", 1.0)),
        long_only=bool(raw_constraints.get("long_only", True)),
        max_gross_exposure=float(raw_constraints.get("max_gross_exposure", 1.0)),
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


def _invoke(
    *,
    code_dir: Path,
    entry: str,
    ctx: FactorContext | StrategyContext | FactorSeriesContext,
) -> Any:
    """按文件路径加载入口模块并调用入口函数。

    不走 ``sys.path`` + ``importlib.import_module``:同进程多次运行时
    ``sys.modules`` 会返回上一次的缓存模块(跨运行污染);spec_from_file_location
    每次重新加载,且不在解释器留下可复用的模块状态。
    """
    module_name, _, func_name = entry.partition(".")
    source = code_dir / f"{module_name}.py"
    if not source.exists():
        raise OutputContractError(f"入口模块文件不存在: {source}(entry={entry})")
    spec = importlib.util.spec_from_file_location(f"_sandbox_entry_{module_name}", source)
    if spec is None or spec.loader is None:
        raise OutputContractError(f"无法加载入口模块: {source}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise OutputContractError(f"入口 {entry} 不是可调用函数(module={module_name})")
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


def _write_error(out_dir: Path, code: str, message: str, tb: str | None = None) -> None:
    payload: dict[str, Any] = {"error_code": code, "message": message[:4000]}
    if tb:
        payload["traceback"] = tb[:16000]
    with contextlib.suppress(OSError):
        (out_dir / _ERROR).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


if __name__ == "__main__":
    sys.exit(main())
