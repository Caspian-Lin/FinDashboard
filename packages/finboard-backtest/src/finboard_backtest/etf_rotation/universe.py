"""ETF 候选池与时点化(PIT)过滤。

issue #61 的核心约束:

* 候选池只包含**历史可见**的上市信息 —— 某日筛选时,未上市或已退市的
  ETF 不会出现在候选中。
* 国债 ETF / 货币 ETF / 跨境 ETF 有不同的交易规则(T+0/T+1、印花税、手数),
  这些由 :mod:`finboard_backtest.asset_rules` 的 ``AssetRuleTable`` 处理;
  本模块只负责候选池的构成和 PIT 过滤。
* **国债 ETF 是风险资产而非保本现金等价物**(README 已明确)。

默认候选池(``DEFAULT_ETF_UNIVERSE``)覆盖 A 股主流宽基/行业/红利/黄金/
跨境/国债/货币 ETF。上市日期为近似值(公告日 vs 首日交易可能差几天),
对回测精度影响可忽略;如需精确日期可在替换成员时修正。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

from finboard_shared.types import AssetClass, EtfCategory


class EtfSector(StrEnum):
    """ETF 在组合中的功能分组(用于资产大类约束和避险切换)。"""

    BROAD_INDEX = "broad_index"
    """宽基指数(沪深300 / 上证50 / 中证500 / 创业板 / 科创50)。"""

    SECTOR = "sector"
    """行业 / 主题 ETF(医药 / 消费 / 科技 / 新能源)。"""

    DIVIDEND = "dividend"
    """红利策略 ETF。"""

    GOLD = "gold"
    """黄金 / 商品 ETF。"""

    CROSS_BORDER = "cross_border"
    """跨境 ETF(纳指 / 标普500 / 恒生)。"""

    GOVERNMENT_BOND = "government_bond"
    """国债 / 政策金融债 ETF(避险候选)。"""

    MONEY_MARKET = "money_market"
    """货币 ETF(现金管理工具)。"""


SECTOR_TO_ASSET_CLASS: dict[EtfSector, AssetClass] = {
    EtfSector.BROAD_INDEX: AssetClass.EQUITY,
    EtfSector.SECTOR: AssetClass.EQUITY,
    EtfSector.DIVIDEND: AssetClass.EQUITY,
    EtfSector.GOLD: AssetClass.COMMODITY,
    EtfSector.CROSS_BORDER: AssetClass.EQUITY,
    EtfSector.GOVERNMENT_BOND: AssetClass.FIXED_INCOME,
    EtfSector.MONEY_MARKET: AssetClass.CASH,
}


@dataclass(frozen=True, slots=True)
class EtfUniverseMember:
    """候选池中一只 ETF 的时点化元数据。"""

    code: str
    name: str
    etf_category: EtfCategory
    sector: EtfSector
    listing_date: date
    delisting_date: date | None = None
    lot_size: int = 100
    """最小交易单位(股票/跨境 ETF=100,债券 ETF=10,货币 ETF=100)。"""

    @property
    def asset_class(self) -> AssetClass:
        """从 sector 推导资产大类。"""
        return SECTOR_TO_ASSET_CLASS[self.sector]

    @property
    def is_listed(self) -> bool:
        """当前是否在市(未退市)。"""
        return self.delisting_date is None

    def is_tradable_on(self, as_of: date) -> bool:
        """该 ETF 在 ``as_of`` 日是否可交易(已上市且未退市)。"""
        if as_of < self.listing_date:
            return False
        return self.delisting_date is None or as_of < self.delisting_date


@dataclass(frozen=True, slots=True)
class UniverseFilterConfig:
    """候选池 PIT 过滤参数。"""

    min_listing_days: int = 60
    """上市至少 N 个日历日后才纳入候选(避免新 ETF 上市初期流动性不足)。"""
    exclude_delisted: bool = True
    exclude_sectors: frozenset[EtfSector] = frozenset()
    """排除的 sector 集合(例如回测时排除货币市场 ETF)。"""


@dataclass(frozen=True, slots=True)
class EtfUniverse:
    """ETF 候选池。"""

    members: tuple[EtfUniverseMember, ...]

    def __post_init__(self) -> None:
        codes = [m.code for m in self.members]
        if len(codes) != len(set(codes)):
            raise ValueError("候选池中存在重复 code")

    def get(self, code: str) -> EtfUniverseMember | None:
        """按 code 查找成员。"""
        for m in self.members:
            if m.code == code:
                return m
        return None

    def filter_pit(
        self,
        as_of: date,
        config: UniverseFilterConfig | None = None,
    ) -> list[EtfUniverseMember]:
        """时点化过滤:返回 ``as_of`` 日可交易且满足过滤条件的成员。

        * 已上市 ``min_listing_days`` 天以上
        * 未退市(若 ``exclude_delisted`` 为 True)
        * sector 不在排除集合中
        """
        cfg = config or UniverseFilterConfig()
        result: list[EtfUniverseMember] = []
        for m in self.members:
            if not m.is_tradable_on(as_of):
                continue
            days_listed = (as_of - m.listing_date).days
            if days_listed < cfg.min_listing_days:
                continue
            if cfg.exclude_delisted and m.delisting_date is not None:
                continue
            if m.sector in cfg.exclude_sectors:
                continue
            result.append(m)
        result.sort(key=lambda x: x.code)
        return result

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(m.code for m in self.members)


def _member(
    code: str,
    name: str,
    etf_category: EtfCategory,
    sector: EtfSector,
    listing_date: str,
    delisting_date: str | None = None,
    lot_size: int = 100,
) -> EtfUniverseMember:
    return EtfUniverseMember(
        code=code,
        name=name,
        etf_category=etf_category,
        sector=sector,
        listing_date=date.fromisoformat(listing_date),
        delisting_date=date.fromisoformat(delisting_date) if delisting_date else None,
        lot_size=lot_size,
    )


DEFAULT_ETF_UNIVERSE = EtfUniverse(
    members=(
        # ── 宽基指数 ──────────────────────────────────────────────────
        _member("510050.SH", "上证50ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2004-12-30"),
        _member("510300.SH", "沪深300ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2012-05-28"),
        _member("510500.SH", "中证500ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2013-03-15"),
        _member("159915.SZ", "创业板ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2011-09-20"),
        _member("588000.SH", "科创50ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2020-09-22"),
        _member("512100.SH", "中证1000ETF", EtfCategory.INDEX, EtfSector.BROAD_INDEX, "2014-12-16"),
        # ── 行业 / 主题 ────────────────────────────────────────────────
        _member("512010.SH", "医药ETF", EtfCategory.INDEX, EtfSector.SECTOR, "2013-09-16"),
        _member("512690.SH", "酒ETF", EtfCategory.INDEX, EtfSector.SECTOR, "2014-07-30"),
        _member("512480.SH", "半导体ETF", EtfCategory.INDEX, EtfSector.SECTOR, "2019-05-08"),
        _member("515790.SH", "光伏ETF", EtfCategory.INDEX, EtfSector.SECTOR, "2020-04-28"),
        _member("512660.SH", "军工ETF", EtfCategory.INDEX, EtfSector.SECTOR, "2016-08-08"),
        # ── 红利 ──────────────────────────────────────────────────────
        _member("510880.SH", "红利ETF", EtfCategory.INDEX, EtfSector.DIVIDEND, "2007-01-18"),
        # ── 黄金 / 商品 ────────────────────────────────────────────────
        _member("518880.SH", "黄金ETF", EtfCategory.COMMODITY, EtfSector.GOLD, "2013-07-29"),
        # ── 跨境 ──────────────────────────────────────────────────────
        _member("513100.SH", "纳指ETF", EtfCategory.CROSS_BORDER, EtfSector.CROSS_BORDER, "2013-05-15"),
        _member("513500.SH", "标普500ETF", EtfCategory.CROSS_BORDER, EtfSector.CROSS_BORDER, "2013-12-05"),
        _member("159920.SZ", "恒生ETF", EtfCategory.CROSS_BORDER, EtfSector.CROSS_BORDER, "2012-08-09"),
        # ── 国债 / 债券 ────────────────────────────────────────────────
        _member("511010.SH", "国债ETF", EtfCategory.BOND, EtfSector.GOVERNMENT_BOND, "2013-03-05", lot_size=10),
        _member("511260.SH", "十年国债ETF", EtfCategory.BOND, EtfSector.GOVERNMENT_BOND, "2017-08-16", lot_size=10),
        # ── 货币 ──────────────────────────────────────────────────────
        _member("511990.SH", "华宝添益", EtfCategory.MONEY_MARKET, EtfSector.MONEY_MARKET, "2013-01-28"),
    )
)
"""A 股主流 ETF 默认候选池(18 只)。

覆盖宽基 / 行业 / 红利 / 黄金 / 跨境 / 国债 / 货币 ETF。
上市日期为近似首日交易日期,如需更精确可替换成员。
"""
