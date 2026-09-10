"""同步范围四元组 —— 框架级公共参数(#385 过滤语义复用,issue #392)。

``exchange / listing_boards / instrument_type / symbols`` 四元组在此**一处归一**,
``dataset_sync`` 的 payload 契约、执行器与 ``bulk_download``(#347/#385)共用同一
解析函数,不再各写一份大小写/去重语义:

* ``exchange`` —— 去空格大写(instruments.exchange 实际取值 SSE/SZSE/BSE/CFFEX);
* ``listing_boards`` —— 去空格小写、去重、排序(instruments.listing_board 实际
  取值 sse_main/szse_main/star/chinext/bse/cdr;排序保证 payload 确定性);
* ``instrument_type`` —— 去空格小写(stock/etf/index/convertible/futures);
* ``symbols`` —— 去空格大写、丢空串、保序去重(与 #385 发布标的归一口径一致;
  作为同步池时顺序不影响产物,保序仅为错误信息可读)。

未声明(``None`` / 空列表)即不过滤。空列表与未声明同义(dataset_sync 侧;
bulk_download 契约层对显式空 ``symbols`` 单独拒绝,#347 语义保持)。
"""

from __future__ import annotations

from dataclasses import dataclass


class ScopeValueError(ValueError):
    """scope 四元组取值非法(契约层映射为 invalid_field_value)。"""


@dataclass(frozen=True, slots=True)
class SyncScope:
    """归一后的同步范围四元组;``None`` / 空元组 = 该维度不过滤。"""

    exchange: str | None
    listing_boards: tuple[str, ...]
    instrument_type: str | None
    symbols: tuple[str, ...]

    @property
    def has_universe_filters(self) -> bool:
        """是否声明了标的宇宙过滤(exchange / listing_boards / instrument_type)。

        三者任一声明 → 逐标的同步池从 ``instruments`` 表按过滤条件解析
        (与 bulk_download 的 list_active 同一过滤语义);全部未声明 → 维持
        旧路径口径(payload.symbols 优先,缺省回落 profiles 同步结果)。
        """

        return (
            self.exchange is not None
            or bool(self.listing_boards)
            or self.instrument_type is not None
        )


def _optional_keyword(value: object, key: str, *, upper: bool) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ScopeValueError(f"{key} 必须是字符串,收到: {value!r}")
    normalized = value.strip()
    if not normalized:
        return None
    return normalized.upper() if upper else normalized.lower()


def _keyword_list(value: object, key: str, *, upper: bool) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise ScopeValueError(f"{key} 必须是字符串列表")
    normalized: dict[str, None] = {}
    for item in value:
        text = item.strip()
        if not text:
            continue
        normalized[text.upper() if upper else text.lower()] = None
    if upper:
        # 大写词表(symbols)保序去重;小写词表(listing_boards)排序定序。
        return tuple(normalized)
    return tuple(sorted(normalized))


def normalize_sync_scope(
    *,
    exchange: object = None,
    listing_boards: object = None,
    instrument_type: object = None,
    symbols: object = None,
) -> SyncScope:
    """把 payload 里的 scope 四元组归一为 :class:`SyncScope`(唯一解析入口)。

    非法类型 / 非法值抛 :class:`ScopeValueError`;payload 契约层捕获后映射为
    ``invalid_field_value`` 入队即拒,执行器重放同一函数覆盖旁路入队。
    """

    return SyncScope(
        exchange=_optional_keyword(exchange, "exchange", upper=True),
        listing_boards=_keyword_list(listing_boards, "listing_boards", upper=False),
        instrument_type=_optional_keyword(
            instrument_type, "instrument_type", upper=False
        ),
        symbols=_keyword_list(symbols, "symbols", upper=True),
    )


__all__ = ["ScopeValueError", "SyncScope", "normalize_sync_scope"]
