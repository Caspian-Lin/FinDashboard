"""issue #450:factor_series 加载路径优化 —— 重读重校验降为序列数次。

生产取证(75 期 x 4 个 u_ 因子,py-spy 三次采样两次同栈):``_series_provider``
无 memoize(对照 #287 provider_memo),每期每序列 ``FactorSeriesRepository.get``
拉整行(values JSON ~26MB)+ 反序列化,300 次/run ≈ 7.8GB DB→worker 搬运;
``FactorSeriesRecord.__post_init__`` 无条件重算 content_checksum(全量
canonical json dumps + sha256,写入时已校验过),读取路径纯浪费。

本文件锁定四组不变量:

* ``_series_provider`` 按 run(工厂闭包)memoize(#287 同构):N 期 x M 序列
  加载场景 ``get`` 恰好 M 次(而非 NxM);记录是 frozen dataclass 跨期共享
  安全;缺失(None)不进 memo(fail-closed 语义不变);新 run 适配器 / 新工厂
  各自独立 memo 不串;
* 持久化读取(``_record_from_row``)传 ``verify_content_checksum=False``:
  读取路径 ``compute_content_checksum`` 调用 0 次;series_key 校验廉价保留,
  篡改读取仍抛 ValueError;写入 / 用户构造默认 True 行为逐位不变;
* 预计算段进度帧(#450):``build_decision_load_contexts`` 新可选
  ``precompute_phase_reporter`` 只写 phase 文本(close 段逐标的节流帧 +
  start/done 段帧),分块探针调用序列(#306/#308 契约)不受影响,上报抛错
  不阻断加载;daily 段计数跨发布累积;
* 节流打点器:首帧 / 步长帧 / 末帧;phase 上报器 best-effort 写 job 行。

不依赖 PostgreSQL:工厂级 memo 用 monkeypatch 替换 finboard_persistence 的
Repository;加载级集成复用 #306 固定样本真实发布,#438 的 bars + daily
发布用于 daily 段进度帧。
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any, cast

import pytest

from finboard_backtest.research_run.contracts import (
    FrozenArtifactRef,
    ResearchRunManifest,
)
from finboard_backtest.research_run.signal_engine import (
    SignalEnginePipelineAdapter,
    build_decision_load_contexts,
    build_run_phase_reporter,
    build_signal_engine_adapter_factory,
)
from finboard_persistence import FactorSeriesRecord, FactorSeriesRepository
from finboard_persistence import factor_series_repo as factor_series_repo_module
from finboard_persistence.models import ResearchFactorSeriesModel

from .test_issue_306_load_probe import (
    _RELEASE_ID,
    _build_release,
    _month_end_decisions,
    _noop_snapshot_provider,
)
from .test_issue_306_load_probe import (
    _manifest as _probe_manifest,
)
from .test_issue_438_daily_precompute import (
    _CODES,
    _daily,
    _precompute_manifest,
    _publish_bars_and_daily,
)

# ---- 固定样本序列记录 --------------------------------------------------------

#: #306 固定样本的三个标的(与序列 values 的截面键一致)。
_CODES_306 = ("600519.SH", "000001.SZ", "600036.SH")

#: 序列覆盖的决策日(= #306 固定样本的月末决策日)。
_SERIES_DATES: tuple[date, ...] = tuple(_month_end_decisions())


def _series_record(artifact: str, *, offset: float) -> FactorSeriesRecord:
    """构造一份覆盖全部固定样本决策日的序列记录(kind=factor → u_<artifact>)。

    series_key 不含 code_artifact(内容寻址 = commit/发布/params/窗口),
    params 里带上 artifact 名使不同因子的序列键不碰撞(与构建执行器同构)。
    """
    values = {
        day.isoformat(): {
            code: offset + index * 0.1 for index, code in enumerate(_CODES_306)
        }
        for day in _SERIES_DATES
    }
    return FactorSeriesRecord.build(
        code_artifact=artifact,
        code_commit="deadbeef450",
        kind="factor",
        release_id=_RELEASE_ID,
        dataset_release_ids=(_RELEASE_ID,),
        params={"window": 20, "factor": artifact},
        window_start=_SERIES_DATES[0],
        window_end=_SERIES_DATES[-1],
        dates=list(_SERIES_DATES),
        values=values,
    )


def _manifest_with_series(
    records: tuple[FactorSeriesRecord, ...],
) -> ResearchRunManifest:
    """#306 固定样本 manifest + 冻结序列引用(artifact_id = 记录 series_id)。"""
    refs = tuple(
        FrozenArtifactRef(artifact_id=record.series_id, version="v1", checksum="c" * 64)
        for record in records
    )
    return replace(_probe_manifest(), factor_series=refs)


# ---- _series_provider 按 run memoize(工厂闭包)-----------------------------


class _FakeSession:
    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False


def _fake_session_maker() -> Any:
    """工厂需要的 session_maker 形状(可调用,返回 async 上下文会话)。"""
    return _FakeSession


def _signal_engine_adapter(factory_output: Any) -> SignalEnginePipelineAdapter:
    """multi_factor manifest 断言产物为信号引擎适配器(取 _series_provider)。"""
    assert isinstance(factory_output, SignalEnginePipelineAdapter)
    return factory_output


def _install_fake_series_repo(
    monkeypatch: pytest.MonkeyPatch,
    records: dict[str, FactorSeriesRecord],
) -> list[str]:
    """把 ``finboard_persistence.FactorSeriesRepository`` 换成内存桩。

    返回 get 调用清单;桩只顶替 DB 行来源,``_series_provider`` 的
    ``from finboard_persistence import ...`` 在调用期解析,包属性替换即生效。
    """
    calls: list[str] = []

    class _FakeRepo:
        def __init__(self, session: object) -> None:
            del session

        async def get(self, series_id: str) -> FactorSeriesRecord | None:
            calls.append(series_id)
            return records.get(series_id)

    monkeypatch.setattr("finboard_persistence.FactorSeriesRepository", _FakeRepo)
    return calls


async def test_series_provider_memoizes_per_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """N 期 x M 序列:同一 series_id 在单 run 内只查一次,记录对象复用。"""

    records = [_series_record("mom450", offset=0.5), _series_record("rev450", offset=1.5)]
    by_id = {record.series_id: record for record in records}
    calls = _install_fake_series_repo(monkeypatch, by_id)
    factory = build_signal_engine_adapter_factory(_fake_session_maker())
    adapter = _signal_engine_adapter(factory(_manifest_with_series((records[0], records[1]))))

    provider = adapter._series_provider
    assert provider is not None
    for _ in range(3):  # 3 个决策期消费同一序列 → 仍只查一次。
        first = await provider(records[0].series_id)
        assert first is records[0]
    second = await provider(records[1].series_id)
    assert second is records[1]

    assert calls == [records[0].series_id, records[1].series_id]


async def test_missing_series_not_memoized(monkeypatch: pytest.MonkeyPatch) -> None:
    """缺失(None)不进 memo:每次调用都回查,fail-closed 语义保持。"""

    calls = _install_fake_series_repo(monkeypatch, {})
    factory = build_signal_engine_adapter_factory(_fake_session_maker())
    adapter = _signal_engine_adapter(factory(_probe_manifest()))

    provider = adapter._series_provider
    assert provider is not None
    assert await provider("FS-missing12345") is None
    assert await provider("FS-missing12345") is None
    assert calls == ["FS-missing12345", "FS-missing12345"]


async def test_new_run_adapter_and_new_factory_get_fresh_memo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """memo 生命周期 = 工厂闭包 = 单次 run:新适配器 / 新工厂各查各的。"""

    records = [_series_record("mom450", offset=0.5)]
    by_id = {record.series_id: record for record in records}
    calls = _install_fake_series_repo(monkeypatch, by_id)
    factory = build_signal_engine_adapter_factory(_fake_session_maker())

    first_provider = _signal_engine_adapter(factory(_probe_manifest()))._series_provider
    second_provider = _signal_engine_adapter(factory(_probe_manifest()))._series_provider
    assert first_provider is not None
    assert second_provider is not None
    assert await first_provider(records[0].series_id) is not None
    # 新 run 适配器(新闭包,同一工厂再次调用)不共享 memo → 再次回查。
    assert await second_provider(records[0].series_id) is not None
    # 全新工厂同理。
    fresh_factory = build_signal_engine_adapter_factory(_fake_session_maker())
    fresh_provider = _signal_engine_adapter(fresh_factory(_probe_manifest()))._series_provider
    assert fresh_provider is not None
    assert await fresh_provider(records[0].series_id) is not None

    assert calls == [records[0].series_id] * 3


async def test_load_n_periods_m_series_queries_at_most_m(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加载级集成:6 期 x 2 序列经真实发布加载,get 恰好 2 次(非 12 次)。"""

    provider = await _build_release(tmp_path)
    records = [_series_record("mom450", offset=0.5), _series_record("rev450", offset=1.5)]
    by_id = {record.series_id: record for record in records}
    calls = _install_fake_series_repo(monkeypatch, by_id)
    factory = build_signal_engine_adapter_factory(_fake_session_maker())
    adapter = _signal_engine_adapter(factory(_manifest_with_series((records[0], records[1]))))
    assert adapter._series_provider is not None

    contexts = await build_decision_load_contexts(
        _manifest_with_series((records[0], records[1])),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        series_provider=adapter._series_provider,
    )

    assert len(contexts) == len(_SERIES_DATES)
    # 每 series_id 恰一次 DB 读取(memo 命中后续期复用同一记录对象)。
    assert sorted(calls) == sorted(by_id)
    # 序列观测真实进入特征截面(u_ 前缀,按 #398 命名)。
    for context in contexts:
        series_features = [
            item for item in context.features if item.feature_id.startswith("u_")
        ]
        assert {item.feature_id for item in series_features} == {"u_mom450", "u_rev450"}
        assert all(item.value is not None for item in series_features)


# ---- 持久化读取跳过 content_checksum 重算 -----------------------------------


def _row_from_record(record: FactorSeriesRecord) -> ResearchFactorSeriesModel:
    """按 upsert 落库列形态构造行对象(不落库,仅走 _record_from_row 装配)。"""
    return ResearchFactorSeriesModel(
        series_id=record.series_id,
        series_key=record.series_key,
        code_artifact=record.code_artifact,
        code_commit=record.code_commit,
        kind=record.kind,
        release_id=record.release_id,
        dataset_release_ids=list(record.dataset_release_ids),
        params=record.params,
        window_start=record.window_start,
        window_end=record.window_end,
        dates=[item.isoformat() for item in record.dates],
        values=record.values,
        content_checksum=record.content_checksum,
        quality=record.quality,
        source_run_id=record.source_run_id,
    )


def _counting_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, int]:
    """把 ``compute_content_checksum`` 包上计数器,返回 ``{"count": n}``。"""
    counter = {"count": 0}
    real = factor_series_repo_module.compute_content_checksum

    def _counting(dates: Any, values: Any) -> str:
        counter["count"] += 1
        return real(dates, values)

    monkeypatch.setattr(
        factor_series_repo_module, "compute_content_checksum", _counting
    )
    return counter


def test_read_path_skips_content_checksum_recompute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取路径(_record_from_row)compute_content_checksum 调用 0 次。"""

    record = _series_record("mom450", offset=0.5)
    counter = _counting_checksum(monkeypatch)
    loaded = FactorSeriesRepository(cast(Any, None))._record_from_row(
        _row_from_record(record)
    )

    assert counter["count"] == 0
    assert loaded.verify_content_checksum is False
    # 开关不参与相等性(compare=False):与构造记录逐字段相等。
    assert loaded == record
    assert loaded.dates == record.dates
    assert loaded.values == record.values


def test_series_key_tamper_still_rejected_on_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """series_key 校验(廉价)读取路径保留:篡改行装配即抛 ValueError。"""

    record = _series_record("mom450", offset=0.5)
    row = _row_from_record(record)
    row.series_key = "f" * 64
    counter = _counting_checksum(monkeypatch)

    with pytest.raises(ValueError, match="series_key 与内容寻址规则不一致"):
        FactorSeriesRepository(cast(Any, None))._record_from_row(row)
    assert counter["count"] == 0


def test_write_and_user_construction_still_verify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """写入路径(build)与用户直接构造默认校验 content_checksum,行为不变。"""

    counter = _counting_checksum(monkeypatch)
    record = _series_record("mom450", offset=0.5)
    assert counter["count"] >= 1  # build 派生 content_checksum 时调用。

    # 直接构造:checksum 一致 → 通过;篡改 → 拒绝。
    ok = FactorSeriesRecord(
        series_id=record.series_id,
        series_key=record.series_key,
        code_artifact=record.code_artifact,
        code_commit=record.code_commit,
        kind=record.kind,
        release_id=record.release_id,
        dataset_release_ids=record.dataset_release_ids,
        params=record.params,
        window_start=record.window_start,
        window_end=record.window_end,
        dates=record.dates,
        values=record.values,
        content_checksum=record.content_checksum,
    )
    assert ok.verify_content_checksum is True
    with pytest.raises(ValueError, match="content_checksum 与 dates/values 内容不一致"):
        FactorSeriesRecord(
            series_id=record.series_id,
            series_key=record.series_key,
            code_artifact=record.code_artifact,
            code_commit=record.code_commit,
            kind=record.kind,
            release_id=record.release_id,
            dataset_release_ids=record.dataset_release_ids,
            params=record.params,
            window_start=record.window_start,
            window_end=record.window_end,
            dates=record.dates,
            values=record.values,
            content_checksum="e" * 64,
        )
    # 显式关闸(读取信任语义)可不校验。
    trusted = FactorSeriesRecord(
        series_id=record.series_id,
        series_key=record.series_key,
        code_artifact=record.code_artifact,
        code_commit=record.code_commit,
        kind=record.kind,
        release_id=record.release_id,
        dataset_release_ids=record.dataset_release_ids,
        params=record.params,
        window_start=record.window_start,
        window_end=record.window_end,
        dates=record.dates,
        values=record.values,
        content_checksum="e" * 64,
        verify_content_checksum=False,
    )
    assert trusted.content_checksum == "e" * 64


# ---- 预计算段进度帧 ---------------------------------------------------------

_PRECOMPUTE_CLOSE_RE = re.compile(r"precompute close (\d+)/(\d+)$")
_PRECOMPUTE_DAILY_RE = re.compile(r"precompute daily (\d+)/(\d+)$")


class _PhaseRecorder:
    """记录 phase 文本的进度上报器(可注入抛错)。"""

    def __init__(self, *, raise_on_text: str | None = None) -> None:
        self.texts: list[str] = []
        self._raise_on_text = raise_on_text

    async def __call__(self, phase: str) -> None:
        if self._raise_on_text is not None and phase == self._raise_on_text:
            raise RuntimeError("模拟进度上报失败")
        self.texts.append(phase)

    def frames(self, pattern: re.Pattern[str]) -> list[tuple[int, int]]:
        return [
            (int(match.group(1)), int(match.group(2)))
            for text in self.texts
            if (match := pattern.search(text))
        ]


async def test_precompute_frames_monotone_and_probe_contract_intact(
    tmp_path: Path,
) -> None:
    """close 段节流帧 + start/done 段帧;#306/#308 分块探针序列不受影响。"""

    provider = await _build_release(tmp_path)
    recorder = _PhaseRecorder()
    chunk_calls: list[tuple[int, int]] = []

    async def probe(done: int, total: int) -> None:
        chunk_calls.append((done, total))

    contexts = await build_decision_load_contexts(
        _probe_manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        chunk_probe=probe,
        precompute_phase_reporter=recorder,
    )

    assert len(contexts) == len(_SERIES_DATES)
    # #306/#308 契约:分块探针调用序列保持 [(0, N), (4, N)]。
    assert chunk_calls == [(0, len(_SERIES_DATES)), (4, len(_SERIES_DATES))]
    # 段帧:start 开头、done 结尾,均只写 phase 文本。
    assert recorder.texts[0] == "research_run:decision_load precompute start"
    assert recorder.texts[-1] == "research_run:decision_load precompute done"
    # close 段帧单调递增(3 个标的 < 节流步长 → 首帧 + 末帧),done==total。
    frames = recorder.frames(_PRECOMPUTE_CLOSE_RE)
    assert frames
    assert all(total == len(_CODES_306) for _, total in frames)
    dones = [done for done, _ in frames]
    assert dones == sorted(set(dones))
    assert frames[0][0] == 1
    assert dones[-1] == len(_CODES_306)
    # 无 daily_metrics 发布 → 不产生 daily 段帧。
    assert recorder.frames(_PRECOMPUTE_DAILY_RE) == []


async def test_precompute_reporter_failure_does_not_block_load(tmp_path: Path) -> None:
    """上报抛错被吞掉:加载结果与无上报器路径一致(尽力而为)。"""

    provider = await _build_release(tmp_path)
    recorder = _PhaseRecorder(
        raise_on_text="research_run:decision_load precompute start"
    )

    contexts = await build_decision_load_contexts(
        _probe_manifest(),
        release_provider_factory=lambda _release_id: provider,
        snapshot_provider=_noop_snapshot_provider,
        precompute_phase_reporter=recorder,
    )

    assert len(contexts) == len(_SERIES_DATES)
    # start 帧抛错被吞,后续帧照常上报。
    assert recorder.texts[0] != "research_run:decision_load precompute start"
    assert recorder.texts[-1] == "research_run:decision_load precompute done"


async def test_daily_precompute_progress_frames(tmp_path: Path) -> None:
    """daily 段逐标的节流帧(#438 发布);计数覆盖该 ensure 全部标的。"""

    from finboard_backtest.research_run.frozen_loader import FrozenInputLoader

    daily_records = {
        code: [
            _daily(
                code,
                day,
                available_at=datetime.combine(day, time(15, 30), tzinfo=UTC),
                pb=f"{10 + index}.{index}",
            )
            for index, day in enumerate((date(2024, 1, 2), date(2024, 1, 3)))
        ]
        for code in _CODES
    }
    providers = await _publish_bars_and_daily(tmp_path, daily_records)
    recorder = _PhaseRecorder()
    loader = FrozenInputLoader(
        release_provider_factory=lambda release_id: providers[release_id],
        snapshot_provider=_noop_snapshot_provider,
    )

    decisions = (
        datetime(2024, 1, 3, 16, 0, tzinfo=UTC),
        datetime(2024, 1, 4, 16, 0, tzinfo=UTC),
    )
    await loader.ensure_daily_metrics_histories(
        _precompute_manifest(), decisions, progress=recorder
    )

    # 2 标的 x 1 发布:首帧 + 末帧,done==total。
    assert recorder.frames(_PRECOMPUTE_DAILY_RE) == [(1, 2), (2, 2)]


# ---- 节流打点器与 phase 上报器 ----------------------------------------------


async def test_precompute_ticker_throttle_cadence() -> None:
    """打点器:首帧 / 步长帧 / 末帧强制,其余静默;None 与 total=0 为 no-op。"""

    from finboard_backtest.research_run.frozen_loader import (
        _PRECOMPUTE_PROGRESS_STEP,
        _make_precompute_ticker,
    )

    texts: list[str] = []

    async def reporter(phase: str) -> None:
        texts.append(phase)

    total = _PRECOMPUTE_PROGRESS_STEP * 2 + 3
    tick = _make_precompute_ticker(reporter, "close", total)
    for _ in range(total):
        await tick()

    expected = {1, _PRECOMPUTE_PROGRESS_STEP, _PRECOMPUTE_PROGRESS_STEP * 2, total}
    dones = {
        int(match.group(1))
        for text in texts
        if (match := _PRECOMPUTE_CLOSE_RE.search(text))
    }
    assert dones == expected
    assert all(text.endswith(f"/{total}") for text in texts)

    # no-op:None 上报器与 total=0 都不打点。
    noop = _make_precompute_ticker(None, "close", 10)
    await noop()
    zero = _make_precompute_ticker(reporter, "close", 0)
    await zero()
    assert len(texts) == len(expected)


def test_build_run_phase_reporter_writes_job_phase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """phase 上报器:run → job_id 解析后只写 phase 文本,done/total 不动。"""

    @dataclass
    class _RunRow:
        job_id: str | None

    run_row = _RunRow(job_id="BJ-450")
    updates: list[tuple[str, int, int | None, str | None]] = []

    class _FakeRunRepo:
        def __init__(self, session: object) -> None:
            del session

        async def get(self, run_id: str) -> _RunRow | None:
            return run_row if run_id == "RR-450" else None

    class _FakeJobRepo:
        def __init__(self, session: object) -> None:
            del session

        async def update_progress(
            self,
            job_id: str,
            *,
            done: int,
            total: int | None = None,
            phase: str | None = None,
        ) -> None:
            updates.append((job_id, done, total, phase))

        async def checkpoint(self) -> None:
            return None

    class _RaisingJobRepo(_FakeJobRepo):
        async def update_progress(
            self,
            job_id: str,
            *,
            done: int,
            total: int | None = None,
            phase: str | None = None,
        ) -> None:
            raise RuntimeError("模拟写库失败")

    monkeypatch.setattr("finboard_persistence.ResearchRunRepository", _FakeRunRepo)
    monkeypatch.setattr("finboard_persistence.BackgroundJobRepository", _FakeJobRepo)

    async def _drive(phase: str) -> None:
        # LoadPhaseReporter 返回 Awaitable,包一层协程供 asyncio.run 消费。
        await reporter(phase)

    reporter = build_run_phase_reporter(_fake_session_maker(), "RR-450")
    asyncio.run(_drive("research_run:decision_load precompute close 1/2"))
    assert updates == [
        ("BJ-450", 0, None, "research_run:decision_load precompute close 1/2")
    ]

    # run 不存在 / job_id 为空:不写。
    other = build_run_phase_reporter(_fake_session_maker(), "RR-none")

    async def _drive_other() -> None:
        await other("x")

    asyncio.run(_drive_other())
    run_row.job_id = None

    async def _drive_again() -> None:
        await reporter("x")

    asyncio.run(_drive_again())
    assert len(updates) == 1

    # 写库失败静默(尽力而为)。
    run_row.job_id = "BJ-450"
    monkeypatch.setattr(
        "finboard_persistence.BackgroundJobRepository", _RaisingJobRepo
    )

    async def _drive_raising() -> None:
        await reporter("x")

    asyncio.run(_drive_raising())
    assert len(updates) == 1
