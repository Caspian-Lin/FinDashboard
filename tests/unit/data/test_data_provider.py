"""finboard-data 单元测试 —— mock akshare 返回值,不实际联网。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from finboard_data.akshare_provider import AkShareProvider, is_etf_code, is_index_code
from finboard_data.base import HistoricalDataProvider
from finboard_data.cache import ParquetCache, expected_last_bar_date
from finboard_data.fallback import FallbackBarProvider
from finboard_data.yfinance_provider import YFinanceProvider
from finboard_shared.models import Bar, Symbol
from finboard_shared.types import BarPeriod, Market

SYMBOL = Symbol(code="510300.SH", market=Market.A_SHARE)


def _make_bar(ts: str, close: str = "4.00") -> Bar:
    return Bar(
        symbol=SYMBOL,
        period=BarPeriod.D1,
        timestamp=datetime.strptime(ts, "%Y-%m-%d").replace(tzinfo=UTC),
        open=Decimal("3.90"),
        high=Decimal("4.10"),
        low=Decimal("3.80"),
        close=Decimal(close),
        volume=Decimal("1000000"),
        amount=Decimal("4000000"),
    )


# --------------------------------------------------------------------------- ParquetCache


class TestParquetCache:
    @pytest.mark.unit
    async def test_write_and_read(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        bars = [_make_bar("2024-01-02", "4.00"), _make_bar("2024-01-03", "4.10")]
        await cache.write(SYMBOL, BarPeriod.D1, "qfq", bars)

        result = await cache.read(SYMBOL, BarPeriod.D1, "qfq")
        assert len(result) == 2
        assert result[0].close == Decimal("4.00")
        assert result[1].close == Decimal("4.10")

    @pytest.mark.unit
    async def test_read_missing(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        result = await cache.read(SYMBOL, BarPeriod.D1, "qfq")
        assert result == []

    @pytest.mark.unit
    async def test_merge_dedup(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        initial = [_make_bar("2024-01-02", "4.00"), _make_bar("2024-01-03", "4.10")]
        await cache.write(SYMBOL, BarPeriod.D1, "qfq", initial)

        new = [_make_bar("2024-01-03", "4.50"), _make_bar("2024-01-04", "4.60")]
        merged = await cache.merge(SYMBOL, BarPeriod.D1, "qfq", new)

        assert len(merged) == 3
        # 重复日期应被覆盖
        jan03 = next(b for b in merged if b.timestamp.day == 3)
        assert jan03.close == Decimal("4.50")

    @pytest.mark.unit
    async def test_metadata_does_not_decode_bars(self, tmp_path: Path) -> None:
        cache = ParquetCache(tmp_path)
        bars = [_make_bar("2024-01-02"), _make_bar("2024-01-03")]
        await cache.write(SYMBOL, BarPeriod.D1, "qfq", bars)

        with patch.object(cache, "_read_sync") as read_sync:
            metadata = await cache.metadata_for(SYMBOL, BarPeriod.D1, "qfq")

        read_sync.assert_not_called()
        assert metadata is not None
        assert metadata.bar_count == 2
        assert metadata.first_date == date(2024, 1, 2)
        assert metadata.last_date == date(2024, 1, 3)

        with patch("pyarrow.parquet.ParquetFile") as parquet_file:
            cached_metadata = await cache.metadata_for(SYMBOL, BarPeriod.D1, "qfq")
        parquet_file.assert_not_called()
        assert cached_metadata == metadata

    @pytest.mark.unit
    async def test_repairs_legacy_yfinance_daily_timezone(self, tmp_path: Path) -> None:
        """旧缓存的 16:00 UTC 应还原为次日交易日 UTC 零点。"""
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = tmp_path / "510300.SH_1d_qfq.parquet"
        table = pa.table(
            {
                "timestamp": [datetime(2024, 1, 1, 16, tzinfo=UTC)],
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": [1.0],
                "amount": [0.0],
            }
        )
        pq.write_table(table, path)
        cache = ParquetCache(tmp_path)

        metadata = await cache.metadata_for(SYMBOL, BarPeriod.D1, "qfq")
        bars = await cache.read(SYMBOL, BarPeriod.D1, "qfq")

        assert metadata is not None
        assert metadata.last_date == date(2024, 1, 2)
        assert bars[0].timestamp == datetime(2024, 1, 2, tzinfo=UTC)

    @pytest.mark.unit
    def test_filter_by_date(self) -> None:
        bars = [
            _make_bar("2024-01-01"),
            _make_bar("2024-01-02"),
            _make_bar("2024-01-03"),
            _make_bar("2024-01-04"),
        ]
        result = ParquetCache.filter_by_date(bars, date(2024, 1, 2), date(2024, 1, 3))
        assert len(result) == 2


# --------------------------------------------------------------------------- AkShareProvider


class TestAkShareProvider:
    @pytest.mark.unit
    async def test_is_protocol(self) -> None:
        provider = AkShareProvider(use_cache=False)
        assert isinstance(provider, HistoricalDataProvider)

    @pytest.mark.unit
    async def test_fetch_with_mock(self, tmp_path: Path) -> None:
        provider = AkShareProvider(cache_dir=tmp_path, use_cache=True)

        mock_bars = [
            _make_bar("2024-01-02", "4.00"),
            _make_bar("2024-01-03", "4.10"),
            _make_bar("2024-01-04", "4.20"),
        ]

        with patch.object(AkShareProvider, "_fetch_from_akshare", return_value=mock_bars):
            result = await provider.fetch_bars(
                SYMBOL,
                BarPeriod.D1,
                date(2024, 1, 2),
                date(2024, 1, 4),
                adjust="qfq",
            )

        assert len(result) == 3
        assert result[0].close == Decimal("4.00")
        assert result[-1].close == Decimal("4.20")

    @pytest.mark.unit
    async def test_cache_hit_avoids_refetch(self, tmp_path: Path) -> None:
        provider = AkShareProvider(cache_dir=tmp_path, use_cache=True)

        mock_bars = [
            _make_bar("2024-01-02", "4.00"),
            _make_bar("2024-01-03", "4.10"),
            _make_bar("2024-01-04", "4.20"),
        ]

        call_count = 0

        async def mock_fetch(*args: object, **kwargs: object) -> list[Bar]:
            nonlocal call_count
            call_count += 1
            return mock_bars

        with patch.object(AkShareProvider, "_fetch_from_akshare", side_effect=mock_fetch):
            await provider.fetch_bars(SYMBOL, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 4))
            # 第二次应该命中缓存
            result = await provider.fetch_bars(
                SYMBOL, BarPeriod.D1, date(2024, 1, 2), date(2024, 1, 4)
            )

        assert call_count == 1
        assert len(result) == 3

    @pytest.mark.unit
    async def test_incremental_fetch_reuses_first_cache_read(self, tmp_path: Path) -> None:
        provider = AkShareProvider(cache_dir=tmp_path, use_cache=True)
        assert provider._cache is not None
        cached = [_make_bar("2024-01-10")]
        fresh = [_make_bar("2024-01-11"), _make_bar("2024-01-12")]
        read_mock = AsyncMock(return_value=cached)
        merge_mock = AsyncMock(return_value=cached + fresh)
        fetch_mock = AsyncMock(return_value=fresh)

        with (
            patch.object(provider._cache, "read", read_mock),
            patch.object(provider._cache, "merge", merge_mock),
            patch.object(provider, "_fetch_from_akshare", fetch_mock),
        ):
            await provider.fetch_bars(
                SYMBOL,
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 12),
            )

        read_mock.assert_awaited_once()
        fetch_call = fetch_mock.await_args
        merge_call = merge_mock.await_args
        assert fetch_call is not None
        assert merge_call is not None
        assert fetch_call.args[2] == date(2024, 1, 3)
        assert merge_call.kwargs["existing_bars"] is cached

    @pytest.mark.unit
    def test_weekend_uses_previous_workday(self) -> None:
        assert expected_last_bar_date(date(2026, 7, 26), today=date(2026, 7, 26)) == date(
            2026, 7, 24
        )

    @pytest.mark.unit
    def test_pre_market_falls_back_to_previous_weekday(self) -> None:
        # 周一盘前(10:00,A 股尚未收盘)应回退到上周五
        assert expected_last_bar_date(
            date(2026, 8, 3),
            today=date(2026, 8, 3),
            now=datetime(2026, 8, 3, 10, 0),
        ) == date(2026, 7, 31)

    @pytest.mark.unit
    def test_post_market_threshold_keeps_today(self) -> None:
        # 21:00(收盘+6h)后视为今天数据已发布
        assert expected_last_bar_date(
            date(2026, 8, 3),
            today=date(2026, 8, 3),
            now=datetime(2026, 8, 3, 21, 0),
        ) == date(2026, 8, 3)

    @pytest.mark.unit
    def test_before_threshold_falls_back(self) -> None:
        # 20:59 仍触发回退
        assert expected_last_bar_date(
            date(2026, 8, 3),
            today=date(2026, 8, 3),
            now=datetime(2026, 8, 3, 20, 59),
        ) == date(2026, 7, 31)

    @pytest.mark.unit
    def test_past_end_unaffected_by_market_hours(self) -> None:
        # end 在过去时,盘前回退不应触发
        assert expected_last_bar_date(
            date(2026, 7, 1),
            today=date(2026, 8, 3),
            now=datetime(2026, 8, 3, 10, 0),
        ) == date(2026, 7, 1)

    @pytest.mark.unit
    def test_parse_timestamp_daily(self) -> None:
        ts = AkShareProvider._parse_timestamp("2024-03-15", BarPeriod.D1)
        assert ts.year == 2024
        assert ts.month == 3
        assert ts.day == 15
        assert ts.tzinfo == UTC

    @pytest.mark.unit
    def test_parse_timestamp_minute(self) -> None:
        ts = AkShareProvider._parse_timestamp("2024-03-15 10:30:00", BarPeriod.M5)
        assert ts.hour == 10
        assert ts.minute == 30


class TestYFinanceProvider:
    @pytest.mark.unit
    def test_daily_timestamp_uses_trading_date_at_utc_midnight(self) -> None:
        class Frame:
            empty = False

            def iterrows(self):
                yield (
                    datetime.fromisoformat("2024-01-02T00:00:00+08:00"),
                    {
                        "Open": 1,
                        "High": 2,
                        "Low": 0.5,
                        "Close": 1.5,
                        "Volume": 100,
                    },
                )

        ticker = SimpleNamespace(history=lambda **kwargs: Frame())
        module = SimpleNamespace(Ticker=lambda symbol: ticker)
        provider = YFinanceProvider(use_cache=False)

        with patch.dict("sys.modules", {"yfinance": module}):
            bars = provider._fetch_sync(
                SYMBOL,
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 3),
                "qfq",
            )

        assert bars[0].timestamp == datetime(2024, 1, 2, tzinfo=UTC)


# --------------------------------------------------------------------------- 指数代码识别与指数日线(issue #184)


class TestIndexCodeDetection:
    @pytest.mark.unit
    def test_sse_indexes_are_detected(self) -> None:
        assert is_index_code("000300.SH") is True
        assert is_index_code("000001.SH") is True
        assert is_index_code("000905.SH") is True

    @pytest.mark.unit
    def test_szse_indexes_are_detected(self) -> None:
        assert is_index_code("399006.SZ") is True
        assert is_index_code("399001.SZ") is True

    @pytest.mark.unit
    def test_bse_index_is_detected(self) -> None:
        assert is_index_code("899050.BJ") is True

    @pytest.mark.unit
    def test_stocks_and_etfs_are_not_indexes(self) -> None:
        assert is_index_code("000001.SZ") is False  # 平安银行(深市股票)
        assert is_index_code("600519.SH") is False  # 贵州茅台(沪市股票)
        assert is_index_code("510300.SH") is False  # 沪深300 ETF
        assert is_index_code("000300") is False  # 无后缀不猜测

    @pytest.mark.unit
    def test_unknown_suffix_returns_false(self) -> None:
        assert is_index_code("000300.HK") is False
        assert is_index_code("000300.US") is False


class TestAkShareIndexDailyFetch:
    @pytest.mark.unit
    def test_index_daily_routes_to_index_interface(self, tmp_path: Path) -> None:
        """000300.SH 走 index_zh_a_hist(不带 adjust),列名与股票日线兼容。"""
        import pandas as pd

        from finboard_data.akshare_provider import AkShareProvider

        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        df = pd.DataFrame(
            {
                "日期": ["2024-01-02", "2024-01-03"],
                "开盘": [3800.0, 3850.0],
                "收盘": [3810.0, 3890.0],
                "最高": [3860.0, 3900.0],
                "最低": [3790.0, 3800.0],
                "成交量": [100000000.0, 120000000.0],
                "成交额": [400000000000.0, 480000000000.0],
                "振幅": [1.0, 2.0],
                "涨跌幅": [0.5, 2.1],
                "涨跌额": [20.0, 80.0],
                "换手率": [0.1, 0.2],
            }
        )
        with (
            patch("akshare.index_zh_a_hist", return_value=df) as index_mock,
            patch("akshare.stock_zh_a_hist") as stock_mock,
        ):
            bars = provider._fetch_sync(
                Symbol(code="000300.SH", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )
        index_mock.assert_called_once()
        stock_mock.assert_not_called()
        assert [bar.close for bar in bars] == [Decimal("3810"), Decimal("3890")]
        assert all(bar.symbol.code == "000300.SH" for bar in bars)
        assert all(bar.timestamp.tzinfo is not None for bar in bars)

    @pytest.mark.unit
    def test_stock_still_routes_to_stock_interface(self, tmp_path: Path) -> None:
        """深市 000 段股票(000001.SZ)仍走 stock_zh_a_hist。"""
        import pandas as pd

        from finboard_data.akshare_provider import AkShareProvider

        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        df = pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "开盘": [10.0],
                "收盘": [10.5],
                "最高": [10.6],
                "最低": [9.9],
                "成交量": [1000000.0],
                "成交额": [10500000.0],
            }
        )
        with (
            patch("akshare.stock_zh_a_hist", return_value=df) as stock_mock,
            patch("akshare.index_zh_a_hist") as index_mock,
        ):
            bars = provider._fetch_sync(
                Symbol(code="000001.SZ", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )
        stock_mock.assert_called_once()
        index_mock.assert_not_called()
        assert len(bars) == 1

    @pytest.mark.unit
    def test_index_minute_rejected(self, tmp_path: Path) -> None:
        """指数只放行日线,分钟线明确报错。"""
        from finboard_data.akshare_provider import AkShareProvider

        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        with pytest.raises(ValueError, match="仅支持日线"):
            provider._fetch_sync(
                Symbol(code="000300.SH", market=Market.A_SHARE),
                BarPeriod.M1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )


# --------------------------------------------------------------------------- ETF 代码识别与基金日线(issue #257)


class TestEtfCodeDetection:
    @pytest.mark.unit
    def test_sh_fund_codes_are_detected(self) -> None:
        assert is_etf_code("510300.SH") is True  # 沪深300 ETF
        assert is_etf_code("588000.SH") is True  # 科创50 ETF
        assert is_etf_code("563800.SH") is True
        assert is_etf_code("501018.SH") is True  # 南方原油(LOF)

    @pytest.mark.unit
    def test_sz_fund_codes_are_detected(self) -> None:
        assert is_etf_code("159915.SZ") is True  # 创业板 ETF
        assert is_etf_code("159707.SZ") is True
        assert is_etf_code("160323.SZ") is True  # LOF

    @pytest.mark.unit
    def test_stocks_indexes_and_unknown_are_not_etf(self) -> None:
        assert is_etf_code("600519.SH") is False  # 沪市股票
        assert is_etf_code("000001.SZ") is False  # 深市股票
        assert is_etf_code("000300.SH") is False  # 沪市指数
        assert is_etf_code("399006.SZ") is False  # 深市指数
        assert is_etf_code("899050.BJ") is False  # 北交所无场内基金
        assert is_etf_code("510300") is False  # 无后缀不猜测
        assert is_etf_code("510300.HK") is False


def _etf_daily_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "日期": ["2024-01-02", "2024-01-03"],
            "开盘": [3.22, 3.17],
            "收盘": [3.17, 3.16],
            "最高": [3.22, 3.18],
            "最低": [3.17, 3.15],
            "成交量": [9429306.0, 10617503.0],
            "成交额": [3.269677e09, 3.654758e09],
            "振幅": [1.58, 1.01],
            "涨跌幅": [-1.43, -0.32],
            "涨跌额": [-0.046, -0.010],
            "换手率": [4.10, 4.62],
        }
    )


class TestAkShareEtfFetch:
    @pytest.mark.unit
    def test_sh_etf_daily_routes_to_fund_interface(self, tmp_path: Path) -> None:
        """510300.SH 必须走 fund_etf_hist_em;股票接口会把 5 开头误判为深市。"""
        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        with (
            patch("akshare.fund_etf_hist_em", return_value=_etf_daily_frame()) as fund_mock,
            patch("akshare.stock_zh_a_hist") as stock_mock,
            patch("akshare.index_zh_a_hist") as index_mock,
        ):
            bars = provider._fetch_sync(
                Symbol(code="510300.SH", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )
        fund_mock.assert_called_once()
        call = fund_mock.call_args
        assert call.kwargs["symbol"] == "510300"
        assert call.kwargs["period"] == "daily"
        assert call.kwargs["adjust"] == "qfq"
        stock_mock.assert_not_called()
        index_mock.assert_not_called()
        assert [bar.close for bar in bars] == [Decimal("3.17"), Decimal("3.16")]
        assert all(bar.symbol.code == "510300.SH" for bar in bars)
        assert all(bar.timestamp.tzinfo is not None for bar in bars)

    @pytest.mark.unit
    def test_sz_etf_daily_routes_to_fund_interface(self, tmp_path: Path) -> None:
        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        with patch("akshare.fund_etf_hist_em", return_value=_etf_daily_frame()) as fund_mock:
            bars = provider._fetch_sync(
                Symbol(code="159915.SZ", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )
        fund_mock.assert_called_once()
        assert fund_mock.call_args.kwargs["symbol"] == "159915"
        assert len(bars) == 2

    @pytest.mark.unit
    def test_stock_still_uses_stock_interface_not_fund(self, tmp_path: Path) -> None:
        """沪市股票(6 开头)不被 ETF 规则抢走。"""
        import pandas as pd

        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        df = pd.DataFrame(
            {
                "日期": ["2024-01-02"],
                "开盘": [10.0],
                "收盘": [10.5],
                "最高": [10.6],
                "最低": [9.9],
                "成交量": [1000000.0],
                "成交额": [10500000.0],
            }
        )
        with (
            patch("akshare.stock_zh_a_hist", return_value=df) as stock_mock,
            patch("akshare.fund_etf_hist_em") as fund_mock,
        ):
            bars = provider._fetch_sync(
                Symbol(code="600519.SH", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "qfq",
            )
        stock_mock.assert_called_once()
        fund_mock.assert_not_called()
        assert len(bars) == 1

    @pytest.mark.unit
    def test_etf_minute_routes_to_fund_min_interface(self, tmp_path: Path) -> None:
        import pandas as pd

        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        df = pd.DataFrame(
            {
                "时间": ["2024-01-02 09:31:00", "2024-01-02 09:32:00"],
                "开盘": [3.20, 3.21],
                "收盘": [3.21, 3.20],
                "最高": [3.22, 3.21],
                "最低": [3.20, 3.19],
                "成交量": [100000.0, 120000.0],
                "成交额": [32100000.0, 38520000.0],
            }
        )
        with (
            patch("akshare.fund_etf_hist_min_em", return_value=df) as fund_min_mock,
            patch("akshare.stock_zh_a_hist_min_em") as stock_min_mock,
        ):
            bars = provider._fetch_sync(
                Symbol(code="510300.SH", market=Market.A_SHARE),
                BarPeriod.M1,
                date(2024, 1, 2),
                date(2024, 1, 2),
                "qfq",
            )
        fund_min_mock.assert_called_once()
        assert fund_min_mock.call_args.kwargs["symbol"] == "510300"
        stock_min_mock.assert_not_called()
        assert [bar.close for bar in bars] == [Decimal("3.21"), Decimal("3.20")]

    @pytest.mark.unit
    def test_etf_none_adjust_maps_to_empty_string(self, tmp_path: Path) -> None:
        """adjust=none 映射为 EM 接口的空字符串,与股票分支语义一致。"""
        provider = AkShareProvider(cache_dir=tmp_path / "cache")
        with patch("akshare.fund_etf_hist_em", return_value=_etf_daily_frame()) as fund_mock:
            provider._fetch_sync(
                Symbol(code="510300.SH", market=Market.A_SHARE),
                BarPeriod.D1,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "none",
            )
        assert fund_mock.call_args.kwargs["adjust"] == ""


# --------------------------------------------------------------------------- FallbackBarProvider(issue #257)


class TestFallbackBarProvider:
    def _provider_pair(
        self,
        primary_bars: list[Bar] | Exception,
        fallback_bars: list[Bar],
    ) -> tuple[FallbackBarProvider, AsyncMock, AsyncMock]:
        primary = AsyncMock(name="primary")
        primary.fetch_bars.return_value = None
        if isinstance(primary_bars, Exception):
            primary.fetch_bars.side_effect = primary_bars
        else:
            primary.fetch_bars.return_value = primary_bars
        fallback = AsyncMock(name="fallback")
        fallback.fetch_bars.return_value = fallback_bars
        wrapper = FallbackBarProvider(
            primary=primary,
            primary_name="tushare",
            fallback=fallback,
            fallback_name="akshare",
        )
        return wrapper, primary, fallback

    @pytest.mark.unit
    async def test_is_protocol(self) -> None:
        wrapper, _, _ = self._provider_pair([_make_bar("2024-01-02")], [])
        assert isinstance(wrapper, HistoricalDataProvider)

    @pytest.mark.unit
    async def test_primary_result_passes_through(self) -> None:
        bars = [_make_bar("2024-01-02")]
        wrapper, primary, fallback = self._provider_pair(bars, [])
        result = await wrapper.fetch_bars(
            SYMBOL, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5)
        )
        assert result is bars
        primary.fetch_bars.assert_awaited_once()
        fallback.fetch_bars.assert_not_awaited()

    @pytest.mark.unit
    async def test_empty_primary_falls_back(self) -> None:
        fallback_bars = [_make_bar("2024-01-02")]
        wrapper, primary, fallback = self._provider_pair([], fallback_bars)
        result = await wrapper.fetch_bars(
            SYMBOL, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5)
        )
        assert result is fallback_bars
        primary.fetch_bars.assert_awaited_once()
        fallback.fetch_bars.assert_awaited_once()

    @pytest.mark.unit
    async def test_primary_exception_falls_back(self) -> None:
        fallback_bars = [_make_bar("2024-01-02")]
        wrapper, _, _fallback = self._provider_pair(
            RuntimeError("Tushare daily 调用失败"), fallback_bars
        )
        result = await wrapper.fetch_bars(
            SYMBOL, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5)
        )
        assert result is fallback_bars

    @pytest.mark.unit
    async def test_both_empty_returns_empty(self) -> None:
        wrapper, _, _ = self._provider_pair([], [])
        result = await wrapper.fetch_bars(
            SYMBOL, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5)
        )
        assert result == []

    @pytest.mark.unit
    async def test_adjust_kwarg_forwarded(self) -> None:
        wrapper, primary, _ = self._provider_pair([_make_bar("2024-01-02")], [])
        await wrapper.fetch_bars(
            SYMBOL, BarPeriod.D1, date(2024, 1, 1), date(2024, 1, 5), adjust="none"
        )
        assert primary.fetch_bars.await_args.kwargs["adjust"] == "none"
