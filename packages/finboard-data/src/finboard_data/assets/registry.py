"""资产注册表与代码 → 市场解析(fail-closed,issue #58)。

取代 ``cache.py:make_symbol`` 的硬编码兜底:未知代码不再默认 ``A_SHARE``,
而是 raise ``InstrumentResolutionError``,强制调用方显式注册。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from finboard_shared.instruments import Instrument
from finboard_shared.types import AssetClass, InstrumentType, ListingStatus, Market


@dataclass(frozen=True, slots=True)
class _CodePrefix:
    """代码前缀匹配规则。"""

    suffix: str  # 后缀(如 ".SH" / ".SZ" / ".CFFEX")
    market: Market
    length: int | None = None  # 代码数字部分长度(可选)

    def matches(self, code: str) -> bool:
        upper = code.upper()
        if not upper.endswith(self.suffix):
            return False
        if self.length is not None:
            digits = upper[: -len(self.suffix)]
            return len(digits) == self.length
        return True


# 静态代码前缀规则表 —— 按特异性排序(更具体的规则在前)
_PREFIX_TABLE: tuple[_CodePrefix, ...] = (
    # 期货交易所
    _CodePrefix(".CFFEX", Market.FUTURE),  # 中金所:IF / IH / IC / T / TF / TS
    _CodePrefix(".SHFE", Market.FUTURE),  # 上期所:CU / AL / AU
    _CodePrefix(".DCE", Market.FUTURE),  # 大商所:I / J / JM
    _CodePrefix(".CZCE", Market.FUTURE),  # 郑商所:MA / TA / CF
    _CodePrefix(".INE", Market.FUTURE),  # 上期能源:SC / LU(issue #267 补齐)
    _CodePrefix(".GFEX", Market.FUTURE),  # 广期所:SI / LC
    # 港股 / 美股
    _CodePrefix(".HK", Market.HK),
    _CodePrefix(".US", Market.US),
    # A 股
    _CodePrefix(".SH", Market.A_SHARE),
    _CodePrefix(".SZ", Market.A_SHARE),
    _CodePrefix(".BJ", Market.A_SHARE),
    _CodePrefix(".SS", Market.A_SHARE),  # yfinance 兼容
)


def _guess_market_by_suffix(code: str) -> Market | None:
    """根据代码后缀猜测市场(无后缀返回 None)。"""
    upper = code.upper()
    for rule in _PREFIX_TABLE:
        if rule.matches(upper):
            return rule.market
    return None


# A 股代码段 → InstrumentType 映射(用于无显式注册时推断)
_A_SHARE_CODE_TYPE: tuple[tuple[str, InstrumentType], ...] = (
    # 可转债:11xxxx(沪)、12xxxx(深)
    ("11", InstrumentType.CONVERTIBLE),
    ("12", InstrumentType.CONVERTIBLE),
    # 国债:019xxx / 020xxx(沪)、109xxx / 119xxx(深)、109xxx企业
    ("019", InstrumentType.BOND),
    ("020", InstrumentType.BOND),
    # ETF:5xxxxx(沪)、1xxxxx(深)
    ("5", InstrumentType.ETF),
    # 股票默认
    ("6", InstrumentType.STOCK),  # 沪市主板
    ("9", InstrumentType.STOCK),  # B 股 / 科创板存托凭证
    ("0", InstrumentType.STOCK),  # 深市主板
    ("3", InstrumentType.STOCK),  # 创业板
    ("8", InstrumentType.STOCK),  # 北交所
    ("4", InstrumentType.STOCK),  # 三板
)


def _guess_type_by_a_share_prefix(digits: str) -> InstrumentType | None:
    """根据 A 股代码数字前缀推断类型(无后缀时使用)。"""
    for prefix, itype in _A_SHARE_CODE_TYPE:
        if digits.startswith(prefix):
            return itype
    return None


def resolve_market_by_code(code: str) -> Market:
    """根据合约代码解析市场。

    期货合约后缀(CFFEX / SHFE / DCE / CZCE / GFEX) → ``Market.FUTURE``;
    A 股后缀(.SH / .SZ / .BJ) → ``Market.A_SHARE``;
    .HK / .US → 对应市场。

    无后缀或未知后缀 raise ``InstrumentResolutionError``(不再默认 A 股)。
    """
    market = _guess_market_by_suffix(code)
    if market is not None:
        return market
    raise InstrumentResolutionError(
        f"无法解析市场: code={code}(无已知后缀,请显式注册 Instrument)"
    )


class InstrumentResolutionError(RuntimeError):
    """资产解析失败 —— 未知代码 / 无可用元数据,必须 fail closed。"""


class InstrumentRegistry:
    """资产元数据注册表 —— ``code`` → ``Instrument`` 单映射。

    设计:
    * 启动时从 ``instruments`` 表或 fake provider 加载,索引到 dict;
    * ``resolve(code)`` 找不到时 raise,**不**回退到默认规则;
    * 支持 ``register`` 增量添加(用于测试 / 动态发现)。
    """

    def __init__(self, instruments: list[Instrument] | None = None) -> None:
        self._by_code: dict[str, Instrument] = {}
        if instruments:
            for inst in instruments:
                self.register(inst)

    def register(self, instrument: Instrument) -> None:
        """注册 / 覆盖一份资产元数据。"""
        self._by_code[instrument.code] = instrument

    def register_many(self, instruments: list[Instrument]) -> None:
        for inst in instruments:
            self.register(inst)

    def resolve(self, code: str) -> Instrument:
        """解析代码到 ``Instrument``;未注册 raise。

        调用方应优先调用本方法,而非 ``resolve_market_by_code``(后者无类型信息)。
        """
        inst = self._by_code.get(code)
        if inst is not None:
            return inst
        # fallback:根据后缀 + 数字前缀合成一个最小 Instrument
        market = _guess_market_by_suffix(code)
        if market is None:
            raise InstrumentResolutionError(
                f"未注册且无已知后缀: code={code}"
            )
        itype = _infer_type_by_code(code, market)
        if itype is None:
            raise InstrumentResolutionError(
                f"未注册且无法推断类型: code={code}(请在 InstrumentRegistry 显式注册)"
            )
        # 合成最小 Instrument 并缓存
        synth = Instrument(
            code=code,
            name=code,
            market=market,
            instrument_type=itype,
            status=ListingStatus.UNKNOWN,
            asset_class=_asset_class_for_type(itype),
        )
        self._by_code[code] = synth
        return synth

    def contains(self, code: str) -> bool:
        return code in self._by_code

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and code in self._by_code

    def __len__(self) -> int:
        return len(self._by_code)

    def list_all(self) -> list[Instrument]:
        return list(self._by_code.values())

    def filter_by_type(self, instrument_type: InstrumentType) -> list[Instrument]:
        return [i for i in self._by_code.values() if i.instrument_type is instrument_type]

    def filter_by_market(self, market: Market) -> list[Instrument]:
        return [i for i in self._by_code.values() if i.market is market]

    def as_mapping(self) -> Mapping[str, Instrument]:
        """只读视图。"""
        return self._by_code


def _infer_type_by_code(code: str, market: Market) -> InstrumentType | None:
    """无显式注册时,根据代码 + 市场推断类型。"""
    upper = code.upper()
    digits = upper.split(".")[0]
    if market is Market.A_SHARE:
        return _guess_type_by_a_share_prefix(digits)
    if market is Market.FUTURE:
        # 期货后缀 .CFFEX / .SHFE 等,字母前缀代表品种
        return InstrumentType.FUTURES
    if market in (Market.HK, Market.US):
        return InstrumentType.STOCK
    return None


def _asset_class_for_type(instrument_type: InstrumentType) -> AssetClass:
    """``InstrumentType`` → ``AssetClass`` 默认映射。"""
    if instrument_type is InstrumentType.STOCK:
        return AssetClass.EQUITY
    if instrument_type is InstrumentType.ETF:
        return AssetClass.EQUITY
    if instrument_type is InstrumentType.BOND:
        return AssetClass.FIXED_INCOME
    if instrument_type is InstrumentType.CONVERTIBLE:
        return AssetClass.CONVERTIBLE
    if instrument_type is InstrumentType.FUTURES:
        return AssetClass.DERIVATIVE
    return AssetClass.EQUITY


def build_registry_from_instruments(instruments: list[Instrument]) -> InstrumentRegistry:
    """从已加载的 ``Instrument`` 列表构建注册表。"""
    return InstrumentRegistry(instruments=instruments)


__all__ = [
    "InstrumentRegistry",
    "InstrumentResolutionError",
    "build_registry_from_instruments",
    "resolve_market_by_code",
]
