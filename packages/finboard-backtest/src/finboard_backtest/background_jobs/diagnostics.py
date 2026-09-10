"""终态 job 诊断重放的门控与产物路径约定(issue #383 CLI / #373 REST 服务化共用)。

单一事实源:kind 放行/拒绝集合、终态门控纯函数与 py-spy 解析原先长在
``finboard_app.cli``(#383),#373 任务中心要把诊断入口服务化到 REST——
api 层不得 import cli(重依赖 typer 装配),门控/集合/解析提取到本模块,
CLI 与 REST 触发侧共用同一判定,防止两入口口径漂移。

边界:本模块只做「判定与解析」,不 spawn 进程、不读 background_jobs;
触发(cli 子命令 / api 路由)各自负责拉起诊断子进程与产物目录管理。
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

#: 诊断重放会话目录命名:``<job_id>-<YYYYmmdd-HHMMSS>``。REST 侧以该
#: 形态做路径校验(防目录穿越),与 ``job-flamegraph`` CLI 的默认命名一致。
SESSION_DIR_RE = re.compile(r"^BJ-[A-Za-z0-9]+-\d{8}-\d{6}$")

#: ``job-flamegraph`` / REST 触发的诊断重放放行 kind → 重放副作用说明
#: (透传给操作者)。只放行「重放幂等」或「副作用已知且明示」的离线域
#: kind;判定依据见 issue #383:research_run 走 #305 replay 新建 run;
#: dataset_publish scratch root 强制重算 + DB 身份幂等;feature_snapshot /
#: factor_series_build 内容寻址幂等;backtest_run 执行层无幂等(明示会多插一行)。
FLAMEGRAPH_REPLAYABLE_KINDS: dict[str, str] = {
    "research_run": "走 #305 replay 语义新建 run 并在本诊断进程内同步执行"
    "(产生新 run 行,带 replay_of_run_id 血缘;completed 源兼得确定性对照)",
    "dataset_publish": "scratch release_root 强制重新冻结(计算全量执行);"
    "DB 身份命中返回已有行,不插新行(scratch 用后即删)",
    "feature_snapshot": "内容寻址快照:计算照跑,落库幂等(不覆盖已有行)",
    "backtest_run": "重放会真实插入一行新 backtest_runs(执行层无幂等检查)",
    "factor_series_build": "内容寻址序列构建:计算照跑(含容器),同键 upsert 幂等;"
    "容器内计算 py-spy 采样不到,火焰图覆盖挂载构建/审计/落库编排段",
}

#: 明确拒绝重放的 kind → 原因。
FLAMEGRAPH_REJECTED_KINDS: dict[str, str] = {
    "bulk_download": "网络摄取:重放会重复消耗数据源配额/限流",
    "data_sync": "网络摄取:重放会重复消耗数据源配额/限流",
    "dataset_sync": "网络摄取:重放会重复消耗数据源配额/限流",
    "quality_repair": "重放会改写真实数据缓存文件",
    "research_code_run": "计算在一次性 Docker 容器内,py-spy 采样不到",
    "validation_experiment": "揭盲是一次性门,重放会重复消耗试验预算",
    "echo": "自检桩,无诊断价值",
}

#: 终态集合(queued/running/retry_waiting/interrupted/cancel_requested 不可重放)。
FLAMEGRAPH_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})


def flamegraph_gate_error(kind: str, status: str) -> str | None:
    """诊断重放门控(issue #383):返回拒绝原因,None=放行。纯函数便于单测。

    dataset_publish 额外要求源 job succeeded —— 失败源重放会以真实身份
    检查路径走 DB 插行(把失败的发布真的完成),诊断工具不做这件事;
    其余放行 kind 对失败源亦安全(重放幂等或副作用已知)。
    """

    if status not in FLAMEGRAPH_TERMINAL_STATUSES:
        return f"job 尚为终态前状态 {status};只放行 succeeded/failed/cancelled"
    if kind in FLAMEGRAPH_REJECTED_KINDS:
        return f"kind={kind} 不支持诊断重放:{FLAMEGRAPH_REJECTED_KINDS[kind]}"
    if kind not in FLAMEGRAPH_REPLAYABLE_KINDS:
        return f"未知 job kind: {kind}"
    if kind == "dataset_publish" and status != "succeeded":
        return (
            "dataset_publish 仅放行 succeeded 源:"
            "失败源重放会真实完成发布(插 DB 行)"
        )
    return None


def resolve_py_spy() -> str | None:
    """解析 py-spy 可执行文件(issue #383):venv 同目录优先,退 PATH。

    py-spy 在 dev 依赖组 —— 经 ``uv run`` / venv 内 ``finboard.exe`` 运行时
    同目录即有 ``py-spy(.exe)``;直接 PATH 调用(如 uv tool 独立安装)仍可命中。
    """

    exe = "py-spy.exe" if sys.platform == "win32" else "py-spy"
    candidate = Path(sys.executable).with_name(exe)
    if candidate.is_file():
        return str(candidate)
    return shutil.which("py-spy")


def is_valid_session_dir_name(name: str) -> bool:
    """会话目录名合法性(REST 路径校验用,防目录穿越)。"""

    return SESSION_DIR_RE.fullmatch(name) is not None


__all__ = [
    "FLAMEGRAPH_REJECTED_KINDS",
    "FLAMEGRAPH_REPLAYABLE_KINDS",
    "FLAMEGRAPH_TERMINAL_STATUSES",
    "SESSION_DIR_RE",
    "flamegraph_gate_error",
    "is_valid_session_dir_name",
    "resolve_py_spy",
]
