"""Tushare 研究数据 Provider。

SDK 在显式构造 Provider 且未注入 client 时才加载。外部响应会先完整规范化,
任意一行不满足契约都会拒绝整批结果,避免把部分坏数据伪装成有效快照。
"""

from __future__ import annotations

import asyncio
import importlib
import math
import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, cast
from zoneinfo import ZoneInfo

from finboard_data.research import (
    DailySecurityMetrics,
    FinancialIndicator,
    IndustryMembership,
    InstrumentProfile,
    ResearchDataConfigurationError,
    ResearchDataContractError,
    ResearchDataDependencyError,
    ResearchDataUpstreamError,
)
from finboard_data.tushare_budget import TushareBudget, shared_tushare_budget

_SOURCE = "tushare"
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SYMBOL_PATTERN = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")
_VALID_LIST_STATUSES = frozenset({"L", "D", "P", "G"})
_TEN_THOUSAND = Decimal("10000")
_ONE_HUNDRED = Decimal("100")

_STOCK_BASIC_FIELDS = "ts_code,name,industry,market,exchange,list_status,list_date,delist_date"
_DAILY_BASIC_FIELDS = (
    "ts_code,trade_date,close,turnover_rate,turnover_rate_f,volume_ratio,"
    "pe,pe_ttm,pb,ps,ps_ttm,dv_ratio,dv_ttm,total_share,float_share,"
    "free_share,total_mv,circ_mv,limit_status"
)
_FINANCIAL_FIELDS = (
    "ts_code,ann_date,end_date,update_flag,eps,dt_eps,bps,ocfps,roe,roe_waa,"
    "grossprofit_margin,netprofit_margin,debt_to_assets,tr_yoy,netprofit_yoy,ocf_yoy"
)
_INDUSTRY_FIELDS = (
    "l1_code,l1_name,l2_code,l2_name,l3_code,l3_name,ts_code,name,in_date,out_date,is_new"
)


class TushareClient(Protocol):
    """Provider 所需的最小 Tushare client 形状,便于离线测试注入。"""

    def stock_basic(self, **kwargs: str) -> object:
        """调用 ``stock_basic``。"""
        ...

    def daily_basic(self, **kwargs: str) -> object:
        """调用 ``daily_basic``。"""
        ...

    def fina_indicator(self, **kwargs: str) -> object:
        """调用 ``fina_indicator``。"""
        ...

    def index_member_all(self, **kwargs: str) -> object:
        """调用 ``index_member_all``。"""
        ...


class TushareResearchDataProvider:
    """读取并规范化 Tushare 研究数据。

    :param token: 显式 token;省略时读取 ``FINBOARD_TUSHARE_TOKEN``。
    :param client: 可注入的 SDK client。注入时不需要 token,适合测试和离线适配。
    :param now: 可注入时钟,必须返回 timezone-aware ``datetime``。
    """

    def __init__(
        self,
        *,
        token: str | None = None,
        client: TushareClient | None = None,
        now: Callable[[], datetime] | None = None,
        budget: TushareBudget | None = None,
        requests_per_minute: int = 200,
        daily_request_limit: int = 100_000,
        usage_file: str = "data_cache/tushare_usage.json",
    ) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._client = client if client is not None else self._create_client(token)
        self._budget = budget or shared_tushare_budget(
            requests_per_minute=requests_per_minute,
            daily_request_limit=daily_request_limit,
            usage_file=usage_file,
        )

    def __repr__(self) -> str:
        """返回不含凭据的稳定表示。"""
        return "TushareResearchDataProvider(source='tushare')"

    async def fetch_instrument_profiles(
        self,
        *,
        list_status: str = "L",
    ) -> list[InstrumentProfile]:
        """读取股票档案;不把当前快照伪装成历史时点数据。"""
        normalized_status = list_status.strip().upper()
        if normalized_status not in _VALID_LIST_STATUSES:
            raise ResearchDataConfigurationError("list_status 必须是 L、D、P 或 G")
        observed_at = self._observed_at()
        rows = await self._call(
            "stock_basic",
            exchange="",
            list_status=normalized_status,
            fields=_STOCK_BASIC_FIELDS,
        )
        _reject_possible_truncation(rows, "stock_basic", limit=6000)
        profiles = [
            self._parse_instrument(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.list_status != normalized_status for item in profiles):
            raise ResearchDataContractError("Tushare stock_basic 返回了请求状态之外的记录")
        return sorted(profiles, key=lambda item: item.symbol)

    async def fetch_daily_metrics(
        self,
        trade_date: date,
    ) -> list[DailySecurityMetrics]:
        """读取全市场每日指标,最早可用时间固定为交易日 17:00(上海时区)。"""
        observed_at = self._observed_at()
        rows = await self._call(
            "daily_basic",
            ts_code="",
            trade_date=_format_date(trade_date),
            fields=_DAILY_BASIC_FIELDS,
        )
        _reject_possible_truncation(rows, "daily_basic", limit=6000)
        metrics = [
            self._parse_daily_metric(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.trade_date != trade_date for item in metrics):
            raise ResearchDataContractError("Tushare daily_basic 返回了请求交易日之外的记录")
        return sorted(metrics, key=lambda item: item.symbol)

    async def fetch_financial_indicators(
        self,
        symbol: str,
        *,
        start_period: date,
        end_period: date,
    ) -> list[FinancialIndicator]:
        """读取单只股票的财务指标,保留公告修订版本。"""
        normalized_symbol = _normalize_symbol(symbol)
        if start_period > end_period:
            raise ResearchDataConfigurationError("start_period 不能晚于 end_period")
        kwargs = {
            "ts_code": normalized_symbol,
            "fields": _FINANCIAL_FIELDS,
            "start_date": _format_date(start_period),
            "end_date": _format_date(end_period),
        }

        observed_at = self._observed_at()
        rows = await self._call("fina_indicator", **kwargs)
        _reject_possible_truncation(rows, "fina_indicator", limit=100)
        indicators = [
            self._parse_financial(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in indicators):
            raise ResearchDataContractError("Tushare fina_indicator 返回了请求标的之外的记录")
        return sorted(
            indicators,
            key=lambda item: (
                item.report_period,
                item.announcement_date,
                item.update_flag or "",
            ),
        )

    async def fetch_industry_memberships(
        self,
        *,
        symbol: str,
        current_only: bool = True,
    ) -> list[IndustryMembership]:
        """读取申万 2021 行业成员;无历史发布时间时按本次观察时间可用。"""
        normalized_symbol = _normalize_symbol(symbol)
        kwargs = {"fields": _INDUSTRY_FIELDS, "ts_code": normalized_symbol}
        if current_only:
            kwargs["is_new"] = "Y"

        observed_at = self._observed_at()
        rows = await self._call("index_member_all", **kwargs)
        _reject_possible_truncation(rows, "index_member_all", limit=2000)
        memberships = [
            self._parse_industry(row, index, observed_at) for index, row in enumerate(rows)
        ]
        if any(item.symbol != normalized_symbol for item in memberships):
            raise ResearchDataContractError("Tushare index_member_all 返回了请求标的之外的记录")
        return sorted(
            memberships,
            key=lambda item: (item.symbol, item.effective_from, item.level3_code),
        )

    def _create_client(self, explicit_token: str | None) -> TushareClient:
        token = (
            explicit_token if explicit_token is not None else os.getenv("FINBOARD_TUSHARE_TOKEN")
        )
        if token is None or not token.strip():
            raise ResearchDataConfigurationError(
                "未配置 Tushare token;请设置 FINBOARD_TUSHARE_TOKEN"
            )
        try:
            module = importlib.import_module("tushare")
        except ImportError:
            raise ResearchDataDependencyError(
                "未安装 Tushare SDK;请安装 finboard-data[tushare]"
            ) from None

        factory_object = getattr(module, "pro_api", None)
        if not callable(factory_object):
            raise ResearchDataDependencyError("已安装的 Tushare SDK 不提供 pro_api")
        factory = cast(Callable[[str], object], factory_object)
        try:
            client = factory(token.strip())
        except Exception:
            raise ResearchDataUpstreamError("Tushare client 初始化失败") from None
        return cast(TushareClient, client)

    def _observed_at(self) -> datetime:
        value = self._now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ResearchDataConfigurationError("Tushare Provider 时钟必须返回带时区的 datetime")
        return value

    async def _call(self, endpoint: str, **kwargs: str) -> list[Mapping[str, object]]:
        method_object = getattr(self._client, endpoint, None)
        if not callable(method_object):
            raise ResearchDataDependencyError(f"Tushare client 不支持 {endpoint}")
        method = cast(Callable[..., object], method_object)
        await self._budget.acquire()
        try:
            payload = await asyncio.to_thread(method, **kwargs)
        except Exception:
            raise ResearchDataUpstreamError(f"Tushare {endpoint} 调用失败") from None
        return _records(payload, endpoint)

    @staticmethod
    def _parse_instrument(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> InstrumentProfile:
        endpoint = "stock_basic"
        _require_fields(row, _STOCK_BASIC_FIELDS, endpoint, index)
        return InstrumentProfile(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            name=_required_text(row, "name", endpoint, index),
            exchange=_required_text(row, "exchange", endpoint, index),
            market=_required_text(row, "market", endpoint, index),
            list_status=_required_text(row, "list_status", endpoint, index),
            list_date=_required_date(row, "list_date", endpoint, index),
            delist_date=_optional_date(row, "delist_date", endpoint, index),
            industry=_optional_text(row, "industry"),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )

    @staticmethod
    def _parse_daily_metric(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> DailySecurityMetrics:
        endpoint = "daily_basic"
        _require_fields(row, _DAILY_BASIC_FIELDS, endpoint, index)
        business_date = _required_date(row, "trade_date", endpoint, index)
        return DailySecurityMetrics(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            trade_date=business_date,
            close=_optional_decimal(row, "close", endpoint, index),
            turnover_rate=_percentage(row, "turnover_rate", endpoint, index),
            turnover_rate_free=_percentage(row, "turnover_rate_f", endpoint, index),
            volume_ratio=_optional_decimal(row, "volume_ratio", endpoint, index),
            pe=_optional_decimal(row, "pe", endpoint, index),
            pe_ttm=_optional_decimal(row, "pe_ttm", endpoint, index),
            pb=_optional_decimal(row, "pb", endpoint, index),
            ps=_optional_decimal(row, "ps", endpoint, index),
            ps_ttm=_optional_decimal(row, "ps_ttm", endpoint, index),
            dividend_yield=_percentage(row, "dv_ratio", endpoint, index),
            dividend_yield_ttm=_percentage(row, "dv_ttm", endpoint, index),
            total_shares=_scaled_decimal(row, "total_share", _TEN_THOUSAND, endpoint, index),
            float_shares=_scaled_decimal(row, "float_share", _TEN_THOUSAND, endpoint, index),
            free_shares=_scaled_decimal(row, "free_share", _TEN_THOUSAND, endpoint, index),
            total_market_cap=_scaled_decimal(row, "total_mv", _TEN_THOUSAND, endpoint, index),
            circulating_market_cap=_scaled_decimal(row, "circ_mv", _TEN_THOUSAND, endpoint, index),
            limit_status=_optional_integer(row, "limit_status", endpoint, index),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=datetime.combine(
                business_date,
                time(hour=17),
                tzinfo=_SHANGHAI,
            ),
        )

    @staticmethod
    def _parse_financial(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> FinancialIndicator:
        endpoint = "fina_indicator"
        _require_fields(row, _FINANCIAL_FIELDS, endpoint, index)
        announcement_date = _required_date(row, "ann_date", endpoint, index)
        return FinancialIndicator(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            announcement_date=announcement_date,
            report_period=_required_date(row, "end_date", endpoint, index),
            update_flag=_optional_text(row, "update_flag"),
            eps=_optional_decimal(row, "eps", endpoint, index),
            diluted_eps=_optional_decimal(row, "dt_eps", endpoint, index),
            book_value_per_share=_optional_decimal(row, "bps", endpoint, index),
            operating_cash_flow_per_share=_optional_decimal(row, "ocfps", endpoint, index),
            return_on_equity=_percentage(row, "roe", endpoint, index),
            weighted_return_on_equity=_percentage(row, "roe_waa", endpoint, index),
            gross_profit_margin=_percentage(row, "grossprofit_margin", endpoint, index),
            net_profit_margin=_percentage(row, "netprofit_margin", endpoint, index),
            debt_to_assets=_percentage(row, "debt_to_assets", endpoint, index),
            revenue_yoy=_percentage(row, "tr_yoy", endpoint, index),
            net_profit_yoy=_percentage(row, "netprofit_yoy", endpoint, index),
            operating_cash_flow_yoy=_percentage(row, "ocf_yoy", endpoint, index),
            source=_SOURCE,
            observed_at=observed_at,
            available_at=datetime.combine(
                announcement_date + timedelta(days=1),
                time.min,
                tzinfo=_SHANGHAI,
            ),
        )

    @staticmethod
    def _parse_industry(
        row: Mapping[str, object],
        index: int,
        observed_at: datetime,
    ) -> IndustryMembership:
        endpoint = "index_member_all"
        _require_fields(row, _INDUSTRY_FIELDS, endpoint, index)
        current_flag = _required_text(row, "is_new", endpoint, index).upper()
        if current_flag not in {"Y", "N"}:
            raise _field_error(endpoint, index, "is_new", "必须是 Y 或 N")
        return IndustryMembership(
            symbol=_normalize_symbol(_required_text(row, "ts_code", endpoint, index)),
            security_name=_required_text(row, "name", endpoint, index),
            taxonomy="SW2021",
            level1_code=_required_text(row, "l1_code", endpoint, index),
            level1_name=_required_text(row, "l1_name", endpoint, index),
            level2_code=_required_text(row, "l2_code", endpoint, index),
            level2_name=_required_text(row, "l2_name", endpoint, index),
            level3_code=_required_text(row, "l3_code", endpoint, index),
            level3_name=_required_text(row, "l3_name", endpoint, index),
            effective_from=_required_date(row, "in_date", endpoint, index),
            effective_to=_optional_date(row, "out_date", endpoint, index),
            is_current=current_flag == "Y",
            source=_SOURCE,
            observed_at=observed_at,
            available_at=observed_at,
        )


def _records(payload: object, endpoint: str) -> list[Mapping[str, object]]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        raw_records: object = payload
    else:
        converter_object = getattr(payload, "to_dict", None)
        if not callable(converter_object):
            raise ResearchDataContractError(f"Tushare {endpoint} 返回值不是记录列表或 DataFrame")
        converter = cast(Callable[..., object], converter_object)
        try:
            raw_records = converter(orient="records")
        except Exception:
            raise ResearchDataContractError(f"Tushare {endpoint} 响应无法转换为记录") from None

    if not isinstance(raw_records, Sequence) or isinstance(raw_records, (str, bytes, bytearray)):
        raise ResearchDataContractError(f"Tushare {endpoint} 响应记录格式无效")
    result: list[Mapping[str, object]] = []
    for index, row in enumerate(raw_records):
        if not isinstance(row, Mapping):
            raise ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行不是字段映射")
        if not all(isinstance(key, str) for key in row):
            raise ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行包含非字符串字段名")
        result.append(cast(Mapping[str, object], row))
    return result


def _reject_possible_truncation(
    rows: Sequence[Mapping[str, object]],
    endpoint: str,
    *,
    limit: int,
) -> None:
    if len(rows) >= limit:
        raise ResearchDataContractError(
            f"Tushare {endpoint} 返回 {len(rows)} 行并达到接口上限 {limit};"
            "结果可能被截断,请缩小查询范围"
        )


def _field_names(fields: str) -> frozenset[str]:
    return frozenset(fields.split(","))


def _require_fields(
    row: Mapping[str, object],
    fields: str,
    endpoint: str,
    index: int,
) -> None:
    missing = sorted(_field_names(fields) - row.keys())
    if missing:
        raise ResearchDataContractError(
            f"Tushare {endpoint} 第 {index} 行缺少字段: {', '.join(missing)}"
        )


def _normalize_symbol(value: str) -> str:
    normalized = value.strip().upper()
    if not _SYMBOL_PATTERN.fullmatch(normalized):
        raise ResearchDataContractError("股票代码必须是 6 位数字并使用 .SH、.SZ 或 .BJ 后缀")
    return normalized


def _format_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _is_missing(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip() or value.strip().lower() in {"nan", "nat", "<na>"}
    if isinstance(value, float):
        return math.isnan(value)
    return isinstance(value, Decimal) and value.is_nan()


def _optional_text(row: Mapping[str, object], field: str) -> str | None:
    value = row[field]
    if _is_missing(value):
        return None
    return str(value).strip()


def _required_text(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> str:
    value = _optional_text(row, field)
    if value is None:
        raise _field_error(endpoint, index, field, "不能为空")
    return value


def _optional_date(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> date | None:
    value = row[field]
    if _is_missing(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError:
        raise _field_error(endpoint, index, field, "不是有效的 YYYYMMDD 日期") from None


def _required_date(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> date:
    value = _optional_date(row, field, endpoint, index)
    if value is None:
        raise _field_error(endpoint, index, field, "不能为空")
    return value


def _optional_decimal(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = row[field]
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        raise _field_error(endpoint, index, field, "不是有效数值")
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise _field_error(endpoint, index, field, "不是有效数值") from None
    if decimal_value.is_nan():
        return None
    if not decimal_value.is_finite():
        raise _field_error(endpoint, index, field, "必须是有限数值")
    return decimal_value


def _scaled_decimal(
    row: Mapping[str, object],
    field: str,
    multiplier: Decimal,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = _optional_decimal(row, field, endpoint, index)
    return None if value is None else value * multiplier


def _percentage(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> Decimal | None:
    value = _optional_decimal(row, field, endpoint, index)
    return None if value is None else value / _ONE_HUNDRED


def _optional_integer(
    row: Mapping[str, object],
    field: str,
    endpoint: str,
    index: int,
) -> int | None:
    value = _optional_decimal(row, field, endpoint, index)
    if value is None:
        return None
    if value != value.to_integral_value():
        raise _field_error(endpoint, index, field, "必须是整数")
    return int(value)


def _field_error(
    endpoint: str,
    index: int,
    field: str,
    message: str,
) -> ResearchDataContractError:
    return ResearchDataContractError(f"Tushare {endpoint} 第 {index} 行字段 {field} {message}")
