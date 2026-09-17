"""issue #383:``collect_parquet_read_stats`` 嵌套安全(句柄栈语义)。

worker 层全程激活(issue #383)后,引擎内既有激活(#285 backtest /
research_run)不再遮蔽外层聚合 —— 读取向**全部活跃句柄**累加:外层句柄
= 全程,内层句柄 = 自身段;单层使用方行为逐位不变。
"""

from __future__ import annotations

from finboard_data.cache import ParquetReadJobStats, _record_job_read, collect_parquet_read_stats


class TestNestedActivation:
    def test_nested_both_handles_accumulate(self) -> None:
        with collect_parquet_read_stats() as outer:
            with collect_parquet_read_stats() as inner:
                _record_job_read("read", elapsed_ms=1.0, size_bytes=10)
            # 内层关闭后,读取只落外层。
            _record_job_read("metadata", elapsed_ms=2.0, size_bytes=5)

        assert inner.read_ops == 1
        assert inner.read_elapsed_ms == 1.0
        assert inner.read_bytes == 10
        assert inner.ops_by_entry == {"read": 1}
        # 外层句柄 = 全程:内层段(1.0ms)+ 内层关闭后的 metadata(2.0ms)。
        assert outer.read_ops == 2
        assert outer.read_elapsed_ms == 3.0
        assert outer.read_bytes == 15
        assert outer.ops_by_entry == {"metadata": 1, "read": 1}

    def test_deep_nesting_all_levels(self) -> None:
        with collect_parquet_read_stats() as level1:
            with collect_parquet_read_stats() as level2:
                with collect_parquet_read_stats() as level3:
                    _record_job_read("read", elapsed_ms=1.0, size_bytes=1)

        assert level1.read_ops == 1
        assert level2.read_ops == 1
        assert level3.read_ops == 1

    def test_single_layer_unchanged(self) -> None:
        """#285 既有单层使用方行为不变。"""

        with collect_parquet_read_stats() as stats:
            stats.record("read", elapsed_ms=3.0, size_bytes=100)
            stats.record("read_close_points", elapsed_ms=1.5, size_bytes=50)
        assert stats.as_dict() == {
            "read_ops": 2,
            "read_elapsed_ms": 4.5,
            "read_bytes": 150,
            "ops_by_entry": {"read": 1, "read_close_points": 1},
        }

    def test_inactive_is_noop(self) -> None:
        # 未激活时静默跳过(既有语义),不抛错。
        _record_job_read("read", elapsed_ms=1.0, size_bytes=1)

    def test_handle_reset_after_exit(self) -> None:
        with collect_parquet_read_stats():
            pass
        # 退出后无活跃句柄:读取不再累加到任何已关闭句柄。
        _record_job_read("read", elapsed_ms=1.0, size_bytes=1)


class TestStatsShape:
    def test_default_stats_zero(self) -> None:
        stats = ParquetReadJobStats()
        assert stats.as_dict() == {
            "read_ops": 0,
            "read_elapsed_ms": 0.0,
            "read_bytes": 0,
            "ops_by_entry": {},
        }
