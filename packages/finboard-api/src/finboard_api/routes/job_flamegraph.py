"""任务诊断重放(火焰图)REST 服务化(issue #373;CLI 入口见 #383)。

把 ``finboard job-flamegraph`` 的诊断重放从纯 CLI 形态服务化:任务中心对
终态且 kind 放行的 job 提供「火焰图诊断」入口。触发侧 spawn 独立诊断
子进程(py-spy record 包 ``job-replay-exec``,launch 模式 #383 已验证),
HTTP 请求立即返回 202;重放耗时与原 job 同量级,绝不在请求内同步执行。

与 background_jobs 队列的关系(#383 的设计决定,此处保持):诊断重放是
故意不入队、不写 background_jobs 的旁路 —— 进程句柄只登记在本模块内存;
服务重启丢句柄后未完成会话按 ``orphaned`` 呈现,分离的重放进程完成后
产物落盘,状态自愈为 ``done``。

门控单一事实源在 ``finboard_backtest.background_jobs.diagnostics``(api
层不 import cli),本路由与 CLI 共用同一放行/拒绝判定。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from finboard_api.deps import get_db_session
from finboard_api.job_schemas import (
    FlamegraphMetaOut,
    FlamegraphReplayResultOut,
    FlamegraphSessionOut,
    FlamegraphStartOut,
)
from finboard_backtest.background_jobs.diagnostics import (
    FLAMEGRAPH_REJECTED_KINDS,
    FLAMEGRAPH_REPLAYABLE_KINDS,
    flamegraph_gate_error,
    is_valid_session_dir_name,
    resolve_py_spy,
)
from finboard_persistence import BackgroundJobRepository

router = APIRouter(prefix="/api/jobs", tags=["jobs"])

#: 诊断产物根(CLI ``job-flamegraph`` 默认同款,CWD 相对 —— dev/服务同以
#: 仓库根为 CWD 运行)。测试经 monkeypatch 替换。
PROFILE_ROOT = Path("data_cache/job_profiles")

#: 采样参数 v1 固定默认值(CLI 同款);不做请求级参数化,产物口径稳定。
_SAMPLE_RATE_HZ = 50

#: 在途诊断进程句柄(session_id → Popen)。仅本进程内有效;后台任务队列
#: (background_jobs)刻意不复用 —— 诊断重放不入队、不写 job 行。
_ACTIVE_REPLAYS: dict[str, subprocess.Popen[bytes]] = {}


def _read_json(path: Path) -> dict[str, Any] | None:
    """读产物 JSON;不存在 / 写一半被轮询读到 → None(前端按未就绪处理)。"""

    try:
        value: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        return value
    except (OSError, json.JSONDecodeError):
        return None


def _session_dir(job_id: str, session_id: str) -> Path:
    """校验并解析会话目录(路径校验:目录名形态固定,防目录穿越)。"""

    if not is_valid_session_dir_name(session_id) or not session_id.startswith(
        f"{job_id}-"
    ):
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    path = PROFILE_ROOT / session_id
    if not path.is_dir():
        raise HTTPException(status_code=404, detail="诊断会话不存在")
    return path


def _list_session_dirs(job_id: str) -> list[Path]:
    root = PROFILE_ROOT
    if not root.is_dir():
        return []
    prefix = f"{job_id}-"
    return sorted(
        (
            p
            for p in root.iterdir()
            if p.is_dir() and p.name.startswith(prefix) and is_valid_session_dir_name(p.name)
        ),
        key=lambda p: p.name,
        reverse=True,
    )


def _session_status(session_id: str, path: Path) -> str:
    handle = _ACTIVE_REPLAYS.get(session_id)
    if handle is not None and handle.poll() is None:
        return "running"
    if (path / "flamegraph.svg").is_file():
        return "done"
    if handle is not None:
        return "failed"
    return "orphaned"


def _session_out(path: Path) -> FlamegraphSessionOut:
    session_id = path.name
    result_payload = _read_json(path / "result.json")
    result = (
        FlamegraphReplayResultOut(
            status=str(result_payload.get("status", "unknown")),
            result_ref=result_payload.get("result_ref"),
            error_code=result_payload.get("error_code"),
            error_summary=result_payload.get("error_summary"),
        )
        if result_payload is not None
        else None
    )
    return FlamegraphSessionOut(
        session_id=session_id,
        status=_session_status(session_id, path),  # type: ignore[arg-type]
        meta=_read_json(path / "meta.json"),
        timing=_read_json(path / "timing.json"),
        result=result,
    )


@router.get("/flamegraph/meta", response_model=FlamegraphMetaOut)
async def flamegraph_meta() -> FlamegraphMetaOut:
    """诊断重放能力表(前端渲染按钮置灰 tooltip 与确认弹窗副作用文案)。"""

    return FlamegraphMetaOut(
        replayable_kinds=FLAMEGRAPH_REPLAYABLE_KINDS,
        rejected_kinds=FLAMEGRAPH_REJECTED_KINDS,
    )


@router.get("/{job_id}/flamegraph", response_model=list[FlamegraphSessionOut])
async def list_flamegraph_sessions(job_id: str) -> list[FlamegraphSessionOut]:
    return [_session_out(path) for path in _list_session_dirs(job_id)]


@router.post(
    "/{job_id}/flamegraph",
    response_model=FlamegraphStartOut,
    status_code=202,
)
async def start_flamegraph(
    job_id: str,
    session: AsyncSession = Depends(get_db_session),
) -> FlamegraphStartOut:
    """对一个终态 job 触发诊断重放,spawn 分离子进程并立即返回 202。

    门控与 CLI 同源(diagnostics.flamegraph_gate_error);py-spy 缺失 503;
    同 job 已有在途诊断 409(避免并发重放互相污染采样与产物)。
    """

    row = await BackgroundJobRepository(session).get(job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="后台任务不存在")
    gate_error = flamegraph_gate_error(row.kind, row.status)
    if gate_error is not None:
        raise HTTPException(status_code=422, detail=gate_error)
    pyspy = resolve_py_spy()
    if pyspy is None:
        raise HTTPException(
            status_code=503,
            detail="py-spy 不可用(dev 依赖组)。请在服务运行环境执行 uv sync",
        )
    for existing in _list_session_dirs(job_id):
        if _session_status(existing.name, existing) == "running":
            raise HTTPException(
                status_code=409,
                detail=f"该任务已有诊断在运行: {existing.name}",
            )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    session_dir = PROFILE_ROOT / f"{job_id}-{stamp}"
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "meta.json").write_text(
        json.dumps(
            {
                "job_id": job_id,
                "kind": row.kind,
                "source_status": row.status,
                "replay_side_effect": FLAMEGRAPH_REPLAYABLE_KINDS[row.kind],
                "format": "flamegraph",
                "rate_hz": _SAMPLE_RATE_HZ,
                "started_at": datetime.now(UTC).isoformat(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    cmd = [
        pyspy,
        "record",
        "--format",
        "flamegraph",
        "--rate",
        str(_SAMPLE_RATE_HZ),
        "--subprocesses",
        "-o",
        str(session_dir / "flamegraph.svg"),
        "--",
        sys.executable,
        "-m",
        "finboard_app.cli",
        "job-replay-exec",
        job_id,
        "--out-dir",
        str(session_dir.resolve()),
    ]
    # ASYNC220:spawn 是阻塞调用,经 to_thread 卸载(一次性毫秒级,但守规矩)。
    handle = await asyncio.to_thread(_spawn_detached, cmd, session_dir / "replay.log")
    _ACTIVE_REPLAYS[session_dir.name] = handle
    return FlamegraphStartOut(session=_session_out(session_dir))


def _spawn_detached(cmd: list[str], log_path: Path) -> subprocess.Popen[bytes]:
    """分离式拉起诊断子进程(同步助手,ASYNC220:调用方经 to_thread)。

    stdout/stderr 并入会话目录 ``replay.log``;Windows 新进程组 + 不弹控制台、
    POSIX 新会话 —— 重放进程脱离服务进程组,服务重启不杀诊断(产物落盘后
    状态自愈为 done)。
    """

    popen_kwargs: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    log_file = log_path.open("wb")
    try:
        return subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            **popen_kwargs,
        )
    finally:
        log_file.close()


@router.get("/{job_id}/flamegraph/{session_id}", response_model=FlamegraphSessionOut)
async def get_flamegraph_session(
    job_id: str, session_id: str
) -> FlamegraphSessionOut:
    return _session_out(_session_dir(job_id, session_id))


@router.get("/{job_id}/flamegraph/{session_id}/flamegraph.svg")
async def get_flamegraph_svg(job_id: str, session_id: str) -> FileResponse:
    path = _session_dir(job_id, session_id)
    svg = path / "flamegraph.svg"
    if not svg.is_file():
        raise HTTPException(
            status_code=404, detail="火焰图尚未生成(诊断可能仍在进行)"
        )
    return FileResponse(
        svg, media_type="image/svg+xml", filename=f"{session_id}-flamegraph.svg"
    )


__all__ = ["router"]
