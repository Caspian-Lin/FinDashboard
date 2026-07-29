"""标的生命周期检测纯逻辑测试(issue #35)。

覆盖 :class:`SuspendDetector`(停牌检测)和 :func:`compute_has_new`
(缓存 last_date 前进判定),不依赖 DB。
"""

from __future__ import annotations

from datetime import date

import pytest

from finboard_data.lifecycle import (
    SuspendDecision,
    SuspendDetector,
    compute_has_new,
)


class TestComputeHasNew:
    """比较拉取前后缓存 last_date 判断是否有新 bar。"""

    def test_no_cache_then_fetched(self) -> None:
        assert compute_has_new(None, date(2026, 7, 29)) is True

    def test_no_cache_no_data(self) -> None:
        assert compute_has_new(None, None) is False

    def test_advanced(self) -> None:
        assert compute_has_new(date(2026, 7, 28), date(2026, 7, 29)) is True

    def test_not_advanced(self) -> None:
        assert compute_has_new(date(2026, 7, 29), date(2026, 7, 29)) is False

    def test_regression(self) -> None:
        # after 早于 before(异常,不应判为有新数据)
        assert compute_has_new(date(2026, 7, 29), date(2026, 7, 28)) is False

    def test_after_none(self) -> None:
        assert compute_has_new(date(2026, 7, 28), None) is False


class TestSuspendDetectorThreshold:
    def test_invalid_threshold_zero(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            SuspendDetector(threshold=0)

    def test_invalid_threshold_negative(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            SuspendDetector(threshold=-1)

    def test_threshold_default(self) -> None:
        assert SuspendDetector().threshold == 5


class TestSuspendDetectorProcess:
    """停牌检测核心计数与决策。"""

    def test_no_data_below_threshold_no_suspend(self) -> None:
        det = SuspendDetector(threshold=5)
        decision = det.process(
            fetch_results={"000001.SZ": False},
            active_codes={"000001.SZ"},
        )
        assert decision.to_suspend == []
        assert det.streak("000001.SZ") == 1
        assert decision.is_empty

    def test_reach_threshold_suspends(self) -> None:
        det = SuspendDetector(threshold=3)
        codes = {"000001.SZ"}
        for _ in range(2):
            det.process(fetch_results={"000001.SZ": False}, active_codes=codes)
        # 第三次达到阈值
        decision = det.process(
            fetch_results={"000001.SZ": False}, active_codes=codes
        )
        assert decision.to_suspend == ["000001.SZ"]

    def test_has_new_resets_streak(self) -> None:
        det = SuspendDetector(threshold=3)
        det.process(fetch_results={"000001.SZ": False}, active_codes={"000001.SZ"})
        assert det.streak("000001.SZ") == 1
        det.process(fetch_results={"000001.SZ": True}, active_codes={"000001.SZ"})
        assert det.streak("000001.SZ") == 0

    def test_has_new_after_threshold_no_suspend(self) -> None:
        # 中途恢复,计数归零,不触发停牌
        det = SuspendDetector(threshold=3)
        det.process(fetch_results={"000001.SZ": False}, active_codes={"000001.SZ"})
        det.process(fetch_results={"000001.SZ": False}, active_codes={"000001.SZ"})
        det.process(fetch_results={"000001.SZ": True}, active_codes={"000001.SZ"})
        decision = det.process(
            fetch_results={"000001.SZ": False}, active_codes={"000001.SZ"}
        )
        assert decision.to_suspend == []

    def test_suspended_with_data_resumes(self) -> None:
        det = SuspendDetector(threshold=2)
        decision = det.process(
            fetch_results={"600000.SH": True},
            active_codes=set(),
            suspended_codes={"600000.SH"},
        )
        assert decision.to_resume == ["600000.SH"]

    def test_suspended_no_data_not_counted(self) -> None:
        # 已 suspended 的标的继续无数据,不应累计也不重复 suspend
        det = SuspendDetector(threshold=2)
        for _ in range(5):
            decision = det.process(
                fetch_results={"600000.SH": False},
                active_codes=set(),
                suspended_codes={"600000.SH"},
            )
        assert decision.to_suspend == []
        assert det.streak("600000.SH") == 0

    def test_only_active_codes_accumulate(self) -> None:
        # 非活跃(如已退市)标的的无数据不累计
        det = SuspendDetector(threshold=2)
        det.process(
            fetch_results={"000001.SZ": False},
            active_codes=set(),  # 该 code 不在 active 集合
        )
        assert det.streak("000001.SZ") == 0

    def test_multiple_mixed(self) -> None:
        det = SuspendDetector(threshold=2)
        # 预热 000001 到 streak=1
        det.process(
            fetch_results={"000001.SZ": False, "000002.SZ": False},
            active_codes={"000001.SZ", "000002.SZ"},
        )
        # 第二次:000001 仍无数据(达阈值),000002 恢复,000003 有数据(新增)
        decision = det.process(
            fetch_results={
                "000001.SZ": False,
                "000002.SZ": True,
                "000003.SZ": True,
            },
            active_codes={"000001.SZ", "000002.SZ", "000003.SZ"},
        )
        assert decision.to_suspend == ["000001.SZ"]
        assert decision.to_resume == []
        assert det.streak("000002.SZ") == 0


class TestSuspendDetectorState:
    def test_state_export_restore(self) -> None:
        det = SuspendDetector(threshold=3)
        det.process(
            fetch_results={"A.SZ": False, "B.SZ": False, "C.SZ": True},
            active_codes={"A.SZ", "B.SZ", "C.SZ"},
        )
        state = det.state()
        assert state == {"A.SZ": 1, "B.SZ": 1, "C.SZ": 0}

        det2 = SuspendDetector(threshold=3)
        assert det2.streak("A.SZ") == 0
        det2.restore(state)
        assert det2.streak("A.SZ") == 1
        # 恢复后继续累计
        decision = det2.process(
            fetch_results={"A.SZ": False}, active_codes={"A.SZ"}
        )
        assert det2.streak("A.SZ") == 2
        assert decision.to_suspend == []

    def test_restore_coerces_to_int(self) -> None:
        det = SuspendDetector(threshold=2)
        det.restore({"X.SZ": "3"})  # type: ignore[dict-item]
        assert det.streak("X.SZ") == 3


class TestSuspendDecision:
    def test_is_empty(self) -> None:
        assert SuspendDecision([], []).is_empty is True
        assert SuspendDecision(["A"], []).is_empty is False
        assert SuspendDecision([], ["B"]).is_empty is False
