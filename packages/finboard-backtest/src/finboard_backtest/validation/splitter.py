"""Walk-forward 时间切分(issue #57)。

支持 rolling / expanding 模式,严格防止未来数据泄漏:

* 训练 / 验证 / 测试集时间不重叠
* 任意窗口的 train_end <= validation_start <= validation_end <= test_start
* Walk-forward 窗口在 [validation_start, test_end] 之间滑动
* 每个 walk-forward 窗口的 test 区间不得越过最终冻结测试集
* 不允许随机打乱(``shuffle=False``)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from finboard_backtest.validation.contracts import ValidationMode, WindowRole


@dataclass(frozen=True, slots=True)
class WindowSlice:
    """单个 walk-forward 窗口的时间区间。

    每个 walk-forward 窗口包含一个 train / 一个 test 区间。``window_index``
    从 0 开始单调递增。
    """

    window_index: int
    role: WindowRole
    start: date
    end: date

    @property
    def length_days(self) -> int:
        return (self.end - self.start).days

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    def overlaps(self, other: WindowSlice) -> bool:
        return not (self.end < other.start or other.end < self.start)


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """一个 walk-forward 窗口的完整 train + test 配对。"""

    window_index: int
    train: WindowSlice
    test: WindowSlice

    def __post_init__(self) -> None:
        if self.train.window_index != self.window_index:
            raise ValueError("train.window_index must equal window_index")
        if self.test.window_index != self.window_index:
            raise ValueError("test.window_index must equal window_index")
        if self.train.role != WindowRole.TRAIN:
            raise ValueError("train role mismatch")
        if self.test.role != WindowRole.TEST:
            raise ValueError("test role mismatch")
        if self.train.end >= self.test.start:
            raise ValueError(
                f"train_end ({self.train.end}) must be < test_start ({self.test.start})"
            )
        if self.train.overlaps(self.test):
            raise ValueError(
                f"window {self.window_index}: train overlaps test (future leakage)"
            )

    def as_dict(self) -> dict[str, object]:
        return {
            "window_index": self.window_index,
            "train": {
                "start": self.train.start.isoformat(),
                "end": self.train.end.isoformat(),
            },
            "test": {
                "start": self.test.start.isoformat(),
                "end": self.test.end.isoformat(),
            },
        }


def _add_trading_days(d: date, days: int) -> date:
    """近似交易日增加(跳过周末,不含节假日)。"""
    cur = d
    added = 0
    step = 1 if days >= 0 else -1
    target = abs(days)
    while added < target:
        cur = cur + timedelta(days=step)
        if cur.weekday() < 5:
            added += 1
    return cur


def generate_walk_forward_windows(
    *,
    mode: ValidationMode,
    train_start: date,
    train_end: date,
    test_start: date,
    test_end: date,
    train_window_days: int,
    test_window_days: int,
    step_days: int,
) -> list[WalkForwardWindow]:
    """生成 walk-forward 窗口列表。

    参数
    ----
    mode
        ``ROLLING``:固定 train_window_days 长度向前滑动;
        ``EXPANDING``:train 起点固定为 train_start,终点向前扩展。
    train_start
        训练数据最早起点。第一个窗口的 train.start = train_start。
    train_end
        训练数据最晚终点。任意 window.train.end 不得超过。
    test_start
        测试集最早起点(必须 > train_start,防止首窗口 test 与 train 重叠)。
        Walk-forward 窗口在 [test_start, test_end] 内滑动。
    test_end
        测试集最晚终点(冻结测试集的终点)。
    train_window_days / test_window_days / step_days
        每个窗口的 train / test 长度(近似交易日)与滑动步长。

    返回
    ----
    ``list[WalkForwardWindow]`` —— 至少包含一个窗口;若不足以生成任何窗口则返回
    空列表(调用方应据此 REJECTED 实验)。

    说明
    ----
    Walk-forward 模式下,window N+1 的 train 会与 window N 的 test 重叠
    (rolling 模式下尤其明显),这是正常行为 —— 每个窗口内部严格保证
    ``train.end < test.start``,但跨窗口允许重叠。
    """
    if train_start >= train_end:
        raise ValueError("train_start must be < train_end")
    if test_start >= test_end:
        raise ValueError("test_start must be < test_end")
    if train_start >= test_start:
        raise ValueError(
            f"train_start ({train_start}) must be < test_start ({test_start})"
        )
    if train_window_days <= 0 or test_window_days <= 0 or step_days <= 0:
        raise ValueError("window/step days must be > 0")

    windows: list[WalkForwardWindow] = []
    window_index = 0

    # 第一个 train 窗口必须包含至少 train_window_days
    cur_train_start = train_start
    cur_train_end = _add_trading_days(cur_train_start, train_window_days - 1)

    while True:
        # train_end 越过全局 train_end 上限 → 停止
        if cur_train_end > train_end:
            break

        cur_test_start = _add_trading_days(cur_train_end, 1)
        cur_test_end = _add_trading_days(cur_test_start, test_window_days - 1)

        # 测试集越过冻结 test_end → 停止
        if cur_test_end > test_end:
            break

        # 测试集还在 test_start 之前(首窗口 train 太短)→ 跳过本窗口但继续扩展
        if cur_test_start < test_start:
            if mode == ValidationMode.ROLLING:
                cur_train_start = _add_trading_days(cur_train_start, step_days)
                cur_train_end = _add_trading_days(cur_train_start, train_window_days - 1)
            else:  # EXPANDING
                cur_train_end = _add_trading_days(cur_train_end, step_days)
            continue

        train_slice = WindowSlice(
            window_index=window_index,
            role=WindowRole.TRAIN,
            start=cur_train_start,
            end=cur_train_end,
        )
        test_slice = WindowSlice(
            window_index=window_index,
            role=WindowRole.TEST,
            start=cur_test_start,
            end=cur_test_end,
        )
        windows.append(
            WalkForwardWindow(
                window_index=window_index,
                train=train_slice,
                test=test_slice,
            )
        )

        window_index += 1

        if mode == ValidationMode.ROLLING:
            # 滑动 train 起点
            cur_train_start = _add_trading_days(cur_train_start, step_days)
            cur_train_end = _add_trading_days(cur_train_start, train_window_days - 1)
        else:  # EXPANDING
            # train 起点固定,终点向前扩展
            cur_train_end = _add_trading_days(cur_train_end, step_days)

    return windows


def make_in_sample_slice(
    *, train_start: date, train_end: date
) -> WindowSlice:
    """构造 IS(in-sample)训练集切片。"""
    return WindowSlice(
        window_index=0,
        role=WindowRole.TRAIN,
        start=train_start,
        end=train_end,
    )


def make_validation_slice(
    *, validation_start: date, validation_end: date
) -> WindowSlice:
    """构造调参 / 早停验证集切片。"""
    return WindowSlice(
        window_index=0,
        role=WindowRole.VALIDATION,
        start=validation_start,
        end=validation_end,
    )


def make_final_test_slice(
    *, test_start: date, test_end: date
) -> WindowSlice:
    """构造冻结测试集切片(只允许一次揭盲)。"""
    return WindowSlice(
        window_index=0,
        role=WindowRole.TEST,
        start=test_start,
        end=test_end,
    )


def assert_no_overlap(*slices: WindowSlice) -> None:
    """断言多个时间区间不重叠(防止数据泄漏)。

    用于在实验创建时验证 plan 的 train / validation / test 三个区间不重叠。
    """
    sorted_slices = sorted(slices, key=lambda s: s.start)
    for i in range(1, len(sorted_slices)):
        prev = sorted_slices[i - 1]
        cur = sorted_slices[i]
        if prev.role == cur.role:
            # 同 role 区间允许重叠(例如多个 walk-forward 窗口)
            continue
        if prev.end >= cur.start:
            raise ValueError(
                f"time leakage: {prev.role.value} [{prev.start}..{prev.end}] "
                f"overlaps {cur.role.value} [{cur.start}..{cur.end}]"
            )
