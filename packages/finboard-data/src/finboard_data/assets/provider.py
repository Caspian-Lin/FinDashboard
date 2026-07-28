"""资产元数据 Provider Protocol(issue #58)。

定义跨资产元数据获取契约,**不**泄漏 akshare/tushare SDK 类型。
具体实现由后续 issue(#27 扩展)注入;回测 / 测试用 FakeProvider。
"""

from __future__ import annotations

from datetime import date
from typing import Protocol, runtime_checkable

from finboard_shared.instruments import (
    BondMetadata,
    ConvertibleMetadata,
    DatasetManifest,
    EtfMetadata,
    FuturesContract,
    Instrument,
    LifecycleEvent,
)


class InstrumentMetadataProviderError(RuntimeError):
    """Provider 异常的基类。"""


@runtime_checkable
class InstrumentMetadataProvider(Protocol):
    """资产元数据 Provider Protocol。

    所有方法都返回 frozen dataclass,与具体供应商 SDK 解耦。
    ``available_at`` 字段严格区分业务时间与可知时间,防止未来信息泄漏。
    """

    async def fetch_instruments(
        self,
        *,
        instrument_type: str | None = None,
        as_of: date | None = None,
    ) -> list[Instrument]:
        """批量获取标的元数据。``as_of=None`` 表示当前快照。"""
        ...

    async def fetch_instrument(self, code: str) -> Instrument:
        """获取单个标的。未找到 raise ``InstrumentMetadataProviderError``。"""
        ...

    async def fetch_etf_metadata(self, fund_code: str) -> EtfMetadata | None:
        """获取 ETF 子描述。"""
        ...

    async def fetch_bond_metadata(self, code: str) -> BondMetadata | None:
        """获取债券子描述。"""
        ...

    async def fetch_convertible_metadata(self, code: str) -> ConvertibleMetadata | None:
        """获取可转债子描述(转股价 / 强赎 / 下修条件)。"""
        ...

    async def fetch_futures_chain(
        self,
        series_id: str,
        *,
        as_of: date | None = None,
    ) -> list[FuturesContract]:
        """获取期货合约链(某品种所有在交易合约)。"""
        ...

    async def fetch_lifecycle_events(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[LifecycleEvent]:
        """获取标的的生命周期事件(分红 / 强赎 / 下修 / 换月 / 退市)。

        ``available_at <= as_of`` 的事件才会返回(防未来信息)。
        """
        ...

    async def fetch_dataset_manifest(
        self,
        dataset_name: str,
        *,
        version: str | None = None,
    ) -> DatasetManifest:
        """获取数据集发布清单(覆盖率 / 校验和 / 质量状态)。"""
        ...


__all__ = [
    "InstrumentMetadataProvider",
    "InstrumentMetadataProviderError",
]
