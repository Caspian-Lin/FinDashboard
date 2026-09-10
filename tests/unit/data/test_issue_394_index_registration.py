"""指数链路 tushare 化测试(issue #394)。

三层:

* ``fetch_index_profiles`` —— tushare ``index_basic`` 全量分页拉取
  (编外市场代码 ``930955.CSI`` 不是契约违规;页级护栏 / 脏行口径);
* ``discover_indices`` —— 登记域收窄 ``is_index_code``、在市过滤、
  ``base_date`` → ``InstrumentInfo.list_date`` 映射;
* ``is_benchmark_index`` —— ``BENCHMARK_INDEX_REGISTRY`` 收窄为基准
  资格白名单后的判定语义。
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from finboard_data import (
    ResearchDataContractError,
    TushareResearchDataProvider,
)
from finboard_data.discovery import (
    BENCHMARK_INDEX_REGISTRY,
    InstrumentInfo,
    UniverseDiscovery,
    is_benchmark_index,
)
from finboard_data.research import IndexProfile
from finboard_shared.types import InstrumentType, ListingBoard, Market

OBSERVED_AT = datetime(2026, 9, 9, 3, 0, tzinfo=UTC)


class NoopBudget:
    async def acquire(self) -> None:
        return None


def _index_row(**overrides: object) -> dict[str, object]:
    """tushare index_basic 单行(默认沪深300)。"""
    row: dict[str, object] = {
        "ts_code": "000300.SH",
        "name": "沪深300",
        "fullname": "沪深300指数",
        "publisher": "中证公司",
        "category": "规模指数",
        "market": "SSE",
        "base_date": "20050408",
        "list_date": "",
        "list_status": "L",
    }
    row.update(overrides)
    return row


class FakeIndexBasicClient:
    """按页返回 index_basic 行的离线 client(记录调用参数)。

    ``TushareClient`` Protocol 的其余成员按「未预期调用」处理:本套测试
    只打 index_basic,其他 endpoint 被调到即失败(在线式防护)。
    """

    def __init__(self, pages: list[list[dict[str, object]]]) -> None:
        self._pages = pages
        self.calls: list[tuple[str, dict[str, str]]] = []

    def index_basic(self, **kwargs: str) -> object:
        self.calls.append(("index_basic", kwargs))
        offset = int(kwargs.get("offset", "0"))
        page_index = offset // 5000
        return self._pages[page_index] if page_index < len(self._pages) else []

    def stock_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def daily_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def fina_indicator(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def index_member_all(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def namechange(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def cb_basic(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")

    def suspend_d(self, **kwargs: str) -> object:
        raise AssertionError("unexpected call")


def _provider(client: FakeIndexBasicClient) -> TushareResearchDataProvider:
    return TushareResearchDataProvider(
        client=client,
        now=lambda: OBSERVED_AT,
        budget=NoopBudget(),
    )


# ---- fetch_index_profiles -----------------------------------------------------


@pytest.mark.unit
async def test_fetch_index_profiles_single_page_parses_and_sorts() -> None:
    client = FakeIndexBasicClient(
        [
            [
                _index_row(),
                _index_row(
                    ts_code="399006.SZ",
                    name="创业板指",
                    fullname="创业板指数P",
                    market="SZSE",
                    base_date="20100531",
                    list_date="20100601",
                ),
                # 编外市场代码:合法快照行,不是脏行(#394 对账口径保真)。
                _index_row(
                    ts_code="930955.CSI",
                    name="中证500红利",
                    market="CSI",
                    base_date="20051230",
                ),
            ]
        ]
    )
    profiles = await _provider(client).fetch_index_profiles()

    assert [item.symbol for item in profiles] == [
        "000300.SH",
        "399006.SZ",
        "930955.CSI",
    ]
    hs300 = profiles[0]
    assert isinstance(hs300, IndexProfile)
    assert hs300.name == "沪深300"
    assert hs300.full_name == "沪深300指数"
    assert hs300.publisher == "中证公司"
    assert hs300.base_date == date(2005, 4, 8)
    assert hs300.list_date is None  # 上游空串 → None(大量交易所指数如此)
    assert hs300.list_status == "L"
    assert hs300.source == "tushare"
    assert hs300.observed_at == OBSERVED_AT
    assert hs300.available_at == OBSERVED_AT
    # 全量单页:fields 白名单(list_status 不请求 —— 上游按分片动态裁列,
    # 2026-09-10 实测请求了也不回)+ 不带市场过滤(全市场快照)。
    assert client.calls == [
        (
            "index_basic",
            {
                "fields": (
                    "ts_code,name,fullname,publisher,category,market,"
                    "base_date,list_date"
                ),
                "limit": "5000",
                "offset": "0",
            },
        )
    ]


@pytest.mark.unit
async def test_fetch_index_profiles_paginates_until_exhausted() -> None:
    # 页终止条件是「行数 < 页大小」:只有整页(5000 行)才会翻页,
    # 末页半页即止(namechange 同款语义)。
    from finboard_data.tushare_provider import _INDEX_BASIC_PAGE_SIZE

    def _page(start: int, count: int) -> list[dict[str, object]]:
        return [
            _index_row(
                ts_code=f"000{start + index:03d}.SH",
                name=f"指数{start + index}",
            )
            for index in range(count)
        ]

    client = FakeIndexBasicClient(
        [
            _page(0, _INDEX_BASIC_PAGE_SIZE),
            _page(0, _INDEX_BASIC_PAGE_SIZE),
            _page(0, 3),
        ]
    )
    profiles = await _provider(client).fetch_index_profiles()

    assert len(profiles) == 2 * _INDEX_BASIC_PAGE_SIZE + 3
    offsets = [kwargs["offset"] for _, kwargs in client.calls]
    assert offsets == ["0", "5000", "10000"]


@pytest.mark.unit
async def test_fetch_index_profiles_empty_upstream_returns_empty() -> None:
    client = FakeIndexBasicClient([[]])
    assert await _provider(client).fetch_index_profiles() == []
    assert len(client.calls) == 1


@pytest.mark.unit
async def test_fetch_index_profiles_dirty_row_skipped_with_all_skipped_reject() -> None:
    client = FakeIndexBasicClient(
        [
            [
                _index_row(),
                # 缺 name:单行契约违规,默认(skip 口径)跳过。
                _index_row(ts_code="000016.SH", name=""),
            ]
        ]
    )
    provider = _provider(client)
    profiles = await provider.fetch_index_profiles()
    assert [item.symbol for item in profiles] == ["000300.SH"]

    # 显式 reject 收紧:同批整批拒。
    with pytest.raises(ResearchDataContractError):
        await provider.fetch_index_profiles(dirty_row_policy="reject")


@pytest.mark.unit
async def test_fetch_index_profiles_all_rows_dirty_is_schema_break() -> None:
    client = FakeIndexBasicClient(
        [[_index_row(name=""), _index_row(ts_code="000016.SH", name="")]]
    )
    with pytest.raises(ResearchDataContractError, match="schema"):
        await _provider(client).fetch_index_profiles()


@pytest.mark.unit
async def test_fetch_index_profiles_garbage_symbol_is_dirty_row() -> None:
    client = FakeIndexBasicClient(
        [
            [
                _index_row(),
                _index_row(ts_code="not-a-code", name="乱码行"),
            ]
        ]
    )
    profiles = await _provider(client).fetch_index_profiles()
    assert [item.symbol for item in profiles] == ["000300.SH"]


@pytest.mark.unit
async def test_fetch_index_profiles_reject_policy_rejects_garbage() -> None:
    client = FakeIndexBasicClient([[_index_row(ts_code="not-a-code")]])
    with pytest.raises(ResearchDataContractError):
        await _provider(client).fetch_index_profiles(dirty_row_policy="reject")


@pytest.mark.unit
async def test_fetch_index_profiles_tolerates_upstream_column_trimming() -> None:
    """上游按市场分片裁列(实测 CSI 批次缺 list_status):缺列行不是脏行。

    2026-09-10 真实对账发现:请求 list_status 时 CSI 等市场整批不回该列,
    分页到该市场后全部行都会缺键 —— 必需列只有 ts_code/name,其余按可选。
    """
    client = FakeIndexBasicClient(
        [
            [
                # 整行只有核心两列(模拟上游裁列后的最瘦行)。
                {"ts_code": "930955.CSI", "name": "中证500红利"},
                {"ts_code": "000300.SH", "name": "沪深300", "base_date": "20050408"},
            ]
        ]
    )
    profiles = await _provider(client).fetch_index_profiles()
    by_symbol = {item.symbol: item for item in profiles}
    assert by_symbol["930955.CSI"].full_name is None
    assert by_symbol["930955.CSI"].base_date is None
    assert by_symbol["930955.CSI"].list_status is None
    assert by_symbol["000300.SH"].base_date == date(2005, 4, 8)


# ---- discover_indices(#394 登记扩大)------------------------------------------


def _stub_provider(rows: list[dict[str, object]]) -> TushareResearchDataProvider:
    return _provider(FakeIndexBasicClient([rows]))


@pytest.mark.unit
async def test_discover_indices_registers_full_a_share_index_domain() -> None:
    """index_basic 全量 → is_index_code 收窄登记;编外市场不进 instruments。"""
    instruments = await UniverseDiscovery().discover_indices(
        _stub_provider(
            [
                _index_row(base_date="20050408"),
                _index_row(ts_code="399006.SZ", name="创业板指", market="SZSE"),
                _index_row(ts_code="899050.BJ", name="北证50", market="BSE"),
                # 编外市场:保留在快照里,但不登记。
                _index_row(ts_code="930955.CSI", name="中证500红利", market="CSI"),
                _index_row(ts_code="H30269.CSI", name="中证 spirituality", market="CSI"),
                # 沪市股票段 600xxx 不是指数代码规则,不登记。
                _index_row(ts_code="600000.SH", name="混进行", market="SSE"),
            ]
        )
    )

    assert [item.code for item in instruments] == [
        "000300.SH",
        "399006.SZ",
        "899050.BJ",
    ]
    for item in instruments:
        assert item.market is Market.A_SHARE
        assert item.instrument_type is InstrumentType.INDEX
        assert item.listing_board is ListingBoard.UNKNOWN
    by_code = {item.code: item for item in instruments}
    assert by_code["000300.SH"].exchange == "SSE"
    assert by_code["399006.SZ"].exchange == "SZSE"
    assert by_code["899050.BJ"].exchange == "BSE"


@pytest.mark.unit
async def test_discover_indices_maps_base_date_to_list_date() -> None:
    """base_date 是 instruments.list_date 的回填上游(#394);list_date 兜底。"""
    instruments = await UniverseDiscovery().discover_indices(
        _stub_provider(
            [
                # 常态:base_date 有值、list_date 空。
                _index_row(base_date="20050408", list_date=""),
                # base_date 缺失时回退上游 list_date。
                _index_row(ts_code="399006.SZ", name="创业板指", base_date="", list_date="20100601"),
                # 双缺失:保持 None 可见缺失(不虚构元数据)。
                _index_row(ts_code="899050.BJ", name="北证50", base_date="", list_date=""),
            ]
        )
    )
    by_code = {item.code: item for item in instruments}
    assert by_code["000300.SH"].list_date == date(2005, 4, 8)
    assert by_code["399006.SZ"].list_date == date(2010, 6, 1)
    assert by_code["899050.BJ"].list_date is None


@pytest.mark.unit
async def test_discover_indices_only_lists_listed_status() -> None:
    """退市(D)/ 未上市(I)指数不登记,交给生命周期 diff 语义。"""
    instruments = await UniverseDiscovery().discover_indices(
        _stub_provider(
            [
                _index_row(),
                _index_row(ts_code="000016.SH", name="上证50", list_status="L"),
                _index_row(ts_code="000018.SH", name="180复权", list_status="D"),
                _index_row(ts_code="000009.SH", name="上证380", list_status="I"),
                _index_row(ts_code="000010.SH", name="上证180", list_status=""),
            ]
        )
    )
    codes = [item.code for item in instruments]
    assert "000018.SH" not in codes
    assert "000009.SH" not in codes
    assert "000010.SH" in codes  # 空状态按在市处理
    assert "000016.SH" in codes


@pytest.mark.unit
async def test_discover_indices_empty_registration_is_schema_break() -> None:
    """全量快照但没有可登记 A 股指数 = 上游 schema 破坏,具名拒绝。"""
    with pytest.raises(RuntimeError, match="index_basic"):
        await UniverseDiscovery().discover_indices(
            _stub_provider([_index_row(ts_code="930955.CSI", name="编外")])
        )


@pytest.mark.unit
async def test_discover_indices_whitelist_subset_still_registered() -> None:
    """基准资格白名单 9 只必须仍是登记域子集(受控语义保留)。"""
    rows = [
        _index_row(ts_code=code, name=name)
        for code, name in BENCHMARK_INDEX_REGISTRY
    ]
    instruments = await UniverseDiscovery().discover_indices(_stub_provider(rows))
    registered = {item.code for item in instruments}
    assert {code for code, _ in BENCHMARK_INDEX_REGISTRY} <= registered


# ---- is_benchmark_index(白名单收窄)-------------------------------------------


@pytest.mark.unit
def test_is_benchmark_index_recognizes_only_whitelist() -> None:
    for code, _ in BENCHMARK_INDEX_REGISTRY:
        assert is_benchmark_index(code), code
        assert is_benchmark_index(code.lower()), code


@pytest.mark.unit
def test_is_benchmark_index_rejects_non_whitelist_indices() -> None:
    """登记扩大的指数(白名单外)不是基准资格资产(#394 语义收窄核心)。"""
    assert not is_benchmark_index("000010.SH")  # 上证180:可登记,非基准资格
    assert not is_benchmark_index("000300.SZ")  # 错误后缀
    assert not is_benchmark_index("600000.SH")  # 股票
    assert not is_benchmark_index("930955.CSI")  # 编外市场


# ---- InstrumentInfo.list_date 默认值(存量路径零回归)---------------------------


@pytest.mark.unit
def test_instrument_info_list_date_defaults_to_none() -> None:
    info = InstrumentInfo(
        code="600000.SH",
        name="浦发银行",
        market=Market.A_SHARE,
        instrument_type=InstrumentType.STOCK,
        exchange="SSE",
    )
    assert info.list_date is None
