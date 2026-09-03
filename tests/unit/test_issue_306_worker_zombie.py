"""issue #306:worker 僵尸无进展检测 —— ``_ZombieProgressWatch`` 纯逻辑单测。

锁定:

* 心跳续租 + progress/phase 指纹不动超阈值 → 判僵尸(True);
* 指纹变化(done 推进或 phase 切换)→ 基准重置,永不误判;
* 正常长加载对照:探针逐分块推进 done,每次观察都算「有进展」,不误杀;
* retain_only 只保留仍在 running 列表的监视项;threshold=0 直接关闭。

判定动作的并发安全(transition 行锁 + expected={running} 状态守卫)与
heartbeat 停续守卫在集成测试 ``tests/integration/test_issue_306_run_guard.py``
覆盖。
"""

from __future__ import annotations

from finboard_backtest.background_jobs.worker import _ZombieProgressWatch


class TestZombieProgressWatch:
    def test_disabled_when_threshold_zero(self) -> None:
        watch = _ZombieProgressWatch(0.0)
        assert watch.enabled is False

    def test_enabled_for_positive_threshold(self) -> None:
        assert _ZombieProgressWatch(3600.0).enabled is True

    def test_stale_fingerprint_beyond_threshold_is_zombie(self) -> None:
        watch = _ZombieProgressWatch(100.0)
        # 首次观察:建立基准,不算僵尸。
        assert watch.observe("BJ-1", (0, "research_run:decision_load"), now=0.0) is False
        # 阈值内:不判。
        assert watch.observe("BJ-1", (0, "research_run:decision_load"), now=99.9) is False
        # 超阈值且指纹未动:判僵尸。
        assert watch.observe("BJ-1", (0, "research_run:decision_load"), now=100.0) is True

    def test_progress_change_resets_baseline(self) -> None:
        watch = _ZombieProgressWatch(100.0)
        assert watch.observe("BJ-1", (0, "load"), now=0.0) is False
        assert watch.observe("BJ-1", (1, "load"), now=50.0) is False
        # done 变化后从 50s 重新起算,100s 时仅过了 50s。
        assert watch.observe("BJ-1", (1, "load"), now=100.0) is False
        assert watch.observe("BJ-1", (1, "load"), now=150.0) is True

    def test_phase_change_resets_baseline(self) -> None:
        watch = _ZombieProgressWatch(100.0)
        assert watch.observe("BJ-1", (3, "research_run:decision_load"), now=0.0) is False
        assert watch.observe("BJ-1", (3, "research_run:universe"), now=99.0) is False

    def test_normal_long_load_is_never_killed(self) -> None:
        """对照:逐分块推进 done 的健康长加载,任何时刻都不判僵尸。"""

        watch = _ZombieProgressWatch(100.0)
        now = 0.0
        for done in range(1, 51):
            # 每个观察周期(80s)完成一个分块:done 推进,心跳正常。
            now += 80.0
            assert watch.observe("BJ-1", (done, "load"), now=now) is False

    def test_watch_is_per_job(self) -> None:
        watch = _ZombieProgressWatch(100.0)
        assert watch.observe("BJ-A", (0, "x"), now=0.0) is False
        assert watch.observe("BJ-B", (0, "x"), now=50.0) is False
        # BJ-A 基准在 0s,101s 已超阈值;BJ-B 基准在 50s,仅过了 51s。
        assert watch.observe("BJ-A", (0, "x"), now=101.0) is True
        assert watch.observe("BJ-B", (0, "x"), now=101.0) is False

    def test_retain_only_drops_finished_jobs(self) -> None:
        watch = _ZombieProgressWatch(100.0)
        watch.observe("BJ-A", (0, "x"), now=0.0)
        watch.observe("BJ-B", (0, "x"), now=0.0)
        watch.retain_only({"BJ-A"})
        # BJ-B 被清除后重新观察回到「首见」分支。
        assert watch.observe("BJ-B", (0, "x"), now=101.0) is False
        assert watch.observe("BJ-A", (0, "x"), now=101.0) is True
