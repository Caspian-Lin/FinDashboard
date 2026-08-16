"""严格时点化的按日因子选股引擎。"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal

from finboard_data.factor_lab import FeatureObservation
from finboard_data.factors import (
    FACTOR_CATALOG,
    FactorInputRecord,
    FactorName,
    FactorResearchReader,
    FactorSelectionConfig,
    FactorSnapshot,
    FactorSnapshotStatus,
    FactorSnapshotWriter,
    FactorValue,
    InputsMode,
    RankingScope,
)
from finboard_shared.models import Bar

_DAILY_DEPENDENT_FACTORS = frozenset(
    {
        FactorName.MARKET_CAP,
        FactorName.PB,
        FactorName.TURNOVER_RATE,
    }
)
_VOLATILITY_WINDOW = 20


class PointInTimeFactorSelector:
    """在 T 日收盘后生成 T+1 生效的冻结候选集。"""

    def __init__(
        self,
        *,
        reader: FactorResearchReader,
        writer: FactorSnapshotWriter | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer

    async def select(
        self,
        *,
        config: FactorSelectionConfig,
        static_universe: Sequence[str],
        business_date: date,
        decision_at: datetime,
        effective_date: date,
        price_history: Mapping[str, Sequence[Bar]],
    ) -> FactorSnapshot:
        """批量计算横截面,质量失败时返回不可发布的 skipped 快照。"""
        if not config.enabled:
            raise ValueError("因子选股未启用")
        if decision_at.tzinfo is None or decision_at.utcoffset() is None:
            raise ValueError("decision_at 必须带时区")
        if effective_date <= business_date:
            raise ValueError("因子快照只能在下一交易日或之后生效")

        universe = tuple(sorted({symbol.strip().upper() for symbol in static_universe}))
        batch = await self._reader.load_factor_inputs(
            symbols=universe,
            business_date=business_date,
            decision_at=decision_at,
            source=config.source,
            required_datasets=config.required_datasets,
            dataset_versions=config.dataset_versions,
        )
        if batch.source != config.source:
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason="source_mismatch",
            )
        # bars / snapshot 模式不依赖 research 数据表:dataset 未发布降级为
        # 警告(profile 过滤缺失由下方 profile 检查兜底,因子依赖缺失由
        # _missing_required_factor 兜底);research_db 模式保持 fail-closed。
        degraded = config.inputs_mode is not InputsMode.RESEARCH_DB
        warnings = _degradable_issues(batch.issues) if degraded else ()
        hard_issues = tuple(
            issue for issue in batch.issues if issue not in set(warnings)
        )
        if hard_issues:
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason=";".join(sorted(set(hard_issues))),
            )

        records = {record.symbol: record for record in batch.records}
        if set(records) != set(universe):
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason="candidate_coverage_incomplete",
            )

        eligibility_issue = _input_integrity_issue(
            records,
            business_date=business_date,
            decision_at=decision_at,
            source=config.source,
            required_factors=config.required_factors,
            inputs_mode=config.inputs_mode,
        )
        if eligibility_issue is not None:
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason=eligibility_issue,
            )
        if degraded and any(record.profile is None for record in records.values()):
            warnings += (
                "instrument_profiles_unavailable:"
                "ST/上市天数/退市过滤降级为不生效",
            )

        eligible = [
            symbol
            for symbol in universe
            if _is_eligible(
                records[symbol],
                config=config,
                business_date=business_date,
                current_bars=price_history.get(symbol, ()),
            )
        ]
        # snapshot 模式因子值来自冻结观测,daily 恒为 None,硬门只对
        # research_db / bars 模式生效;观测缺因子由 _missing_required_factor 兜底。
        if (
            config.inputs_mode is not InputsMode.SNAPSHOT
            and config.required_factors & _DAILY_DEPENDENT_FACTORS
        ):
            missing_daily = next(
                (
                    symbol
                    for symbol in eligible
                    if records[symbol].daily is None
                ),
                None,
            )
            if missing_daily is not None:
                return await self._skip(
                    config=config,
                    universe=universe,
                    business_date=business_date,
                    decision_at=decision_at,
                    effective_date=effective_date,
                    dataset_versions=batch.dataset_versions,
                    reason=f"daily_metrics_missing:{missing_daily}",
                )
        factor_values = _calculate_factor_values(
            eligible,
            records=records,
            price_history=price_history,
            momentum_lookback=config.momentum_lookback,
        )
        missing = _missing_required_factor(
            eligible,
            required=config.required_factors,
            factor_values=factor_values,
        )
        if missing is not None:
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason=missing,
            )

        if (
            config.ranking_scope is RankingScope.INDUSTRY or config.max_per_industry is not None
        ) and any(records[symbol].industry is None for symbol in eligible):
            return await self._skip(
                config=config,
                universe=universe,
                business_date=business_date,
                decision_at=decision_at,
                effective_date=effective_date,
                dataset_versions=batch.dataset_versions,
                reason="industry_coverage_incomplete",
            )

        ranked_values = _rank_factor_values(
            eligible,
            records=records,
            factor_values=factor_values,
        )
        filtered = [
            symbol
            for symbol in eligible
            if _passes_filters(symbol, config=config, values=factor_values)
        ]
        ordered = _order_candidates(
            filtered,
            config=config,
            records=records,
            factor_values=factor_values,
        )
        selected = _apply_limits(
            ordered,
            config=config,
            records=records,
        )
        if not set(selected).issubset(universe):
            raise RuntimeError("因子快照越过静态候选池")

        snapshot = _snapshot(
            config=config,
            universe=universe,
            business_date=business_date,
            decision_at=decision_at,
            effective_date=effective_date,
            dataset_versions=batch.dataset_versions,
            selected_symbols=tuple(selected),
            values=tuple(ranked_values),
            status=FactorSnapshotStatus.PUBLISHED,
            skip_reason=None,
            warnings=tuple(warnings),
        )
        return await self._persist(snapshot)

    async def _skip(
        self,
        *,
        config: FactorSelectionConfig,
        universe: tuple[str, ...],
        business_date: date,
        decision_at: datetime,
        effective_date: date,
        dataset_versions: dict[str, str],
        reason: str,
    ) -> FactorSnapshot:
        snapshot = _snapshot(
            config=config,
            universe=universe,
            business_date=business_date,
            decision_at=decision_at,
            effective_date=effective_date,
            dataset_versions=dataset_versions,
            selected_symbols=(),
            values=(),
            status=FactorSnapshotStatus.SKIPPED,
            skip_reason=reason,
        )
        return await self._persist(snapshot)

    async def _persist(self, snapshot: FactorSnapshot) -> FactorSnapshot:
        if self._writer is None:
            return snapshot
        snapshot_id = await self._writer.save_factor_snapshot(snapshot)
        return replace(snapshot, snapshot_id=snapshot_id)


def _degradable_issues(issues: Sequence[str]) -> tuple[str, ...]:
    """bars / snapshot 模式下可降级为警告的 reader issue。"""
    return tuple(
        issue
        for issue in issues
        if issue.startswith("dataset_unpublished:")
    )


def _input_integrity_issue(
    records: Mapping[str, FactorInputRecord],
    *,
    business_date: date,
    decision_at: datetime,
    source: str,
    required_factors: frozenset[FactorName],
    inputs_mode: InputsMode,
) -> str | None:
    for symbol in sorted(records):
        record = records[symbol]
        if record.symbol != symbol:
            return f"symbol_mismatch:{symbol}"
        if inputs_mode is InputsMode.RESEARCH_DB and record.profile is None:
            return f"profile_missing:{symbol}"
        if (
            record.daily is not None
            and record.daily.trade_date != business_date
            and bool(required_factors & _DAILY_DEPENDENT_FACTORS)
        ):
            return f"daily_metrics_stale:{symbol}"
        inputs = (
            record.profile,
            record.daily,
            record.financial,
            record.industry,
        )
        if any(item is not None and item.available_at > decision_at for item in inputs):
            return f"future_data:{symbol}"
        if any(item is not None and item.source != source for item in inputs):
            return f"record_source_mismatch:{symbol}"
    return None


def _is_eligible(
    record: FactorInputRecord,
    *,
    config: FactorSelectionConfig,
    business_date: date,
    current_bars: Sequence[Bar],
) -> bool:
    profile = record.profile
    if profile is not None:
        if profile.list_status.upper() != "L":
            return False
        if profile.list_date > business_date:
            return False
        if profile.delist_date is not None and profile.delist_date <= business_date:
            return False
        if (business_date - profile.list_date).days < config.min_listing_days:
            return False
        if config.exclude_st and "ST" in profile.name.upper():
            return False
    if config.exclude_suspended:
        if not current_bars:
            return False
        latest = current_bars[-1]
        if latest.timestamp.date() != business_date or latest.volume <= 0:
            return False
    return True


def _calculate_factor_values(
    symbols: Sequence[str],
    *,
    records: Mapping[str, FactorInputRecord],
    price_history: Mapping[str, Sequence[Bar]],
    momentum_lookback: int,
) -> dict[FactorName, dict[str, Decimal]]:
    result: dict[FactorName, dict[str, Decimal]] = {
        factor_name: {} for factor_name in FACTOR_CATALOG
    }
    for symbol in symbols:
        record = records[symbol]
        if record.features:
            # snapshot 模式:因子值直接来自冻结快照观测,不重复计算。
            _apply_snapshot_features(result, symbol, record.features)
            continue
        daily = record.daily
        if daily is not None:
            _put(result, FactorName.MARKET_CAP, symbol, daily.total_market_cap)
            _put(result, FactorName.PB, symbol, daily.pb)
            _put(result, FactorName.TURNOVER_RATE, symbol, daily.turnover_rate)

        bars = price_history.get(symbol, ())
        if len(bars) > momentum_lookback:
            start = bars[-(momentum_lookback + 1)].close
            end = bars[-1].close
            if start > 0:
                _put(result, FactorName.MOMENTUM, symbol, end / start - Decimal(1))
        volatility = _volatility_20d(bars)
        if volatility is not None:
            _put(result, FactorName.VOLATILITY_20D, symbol, volatility)

        financial = record.financial
        if financial is not None:
            _put(result, FactorName.ROE, symbol, financial.return_on_equity)
            _put(
                result,
                FactorName.GROSS_PROFIT_MARGIN,
                symbol,
                financial.gross_profit_margin,
            )
            _put(result, FactorName.REVENUE_YOY, symbol, financial.revenue_yoy)
    return result


def _apply_snapshot_features(
    target: dict[FactorName, dict[str, Decimal]],
    symbol: str,
    observations: Sequence[FeatureObservation],
) -> None:
    """把快照观测的因子值写入因子矩阵(未知因子名静默跳过)。"""
    for observation in observations:
        try:
            factor_name = FactorName(observation.feature_name)
        except ValueError:
            continue
        _put(target, factor_name, symbol, Decimal(str(observation.value)))


def _volatility_20d(bars: Sequence[Bar]) -> Decimal | None:
    """最近 20 个交易日收益率的样本标准差(ddof=1),口径与 extract.py 一致。"""
    if len(bars) < _VOLATILITY_WINDOW + 1:
        return None
    returns: list[Decimal] = []
    for i in range(len(bars) - _VOLATILITY_WINDOW, len(bars)):
        previous = bars[i - 1].close
        if previous <= 0:
            return None
        returns.append(bars[i].close / previous - Decimal(1))
    mean = sum(returns, Decimal(0)) / len(returns)
    variance = sum(
        ((r - mean) ** 2 for r in returns), Decimal(0)
    ) / (len(returns) - 1)
    return variance.sqrt()


def _put(
    target: dict[FactorName, dict[str, Decimal]],
    factor_name: FactorName,
    symbol: str,
    value: Decimal | None,
) -> None:
    if value is not None:
        target[factor_name][symbol] = value


def _missing_required_factor(
    symbols: Sequence[str],
    *,
    required: frozenset[FactorName],
    factor_values: Mapping[FactorName, Mapping[str, Decimal]],
) -> str | None:
    for factor_name in sorted(required):
        missing = sorted(set(symbols) - set(factor_values[factor_name]))
        if missing:
            return f"factor_missing:{factor_name.value}:{missing[0]}"
    return None


def _rank_factor_values(
    symbols: Sequence[str],
    *,
    records: Mapping[str, FactorInputRecord],
    factor_values: Mapping[FactorName, Mapping[str, Decimal]],
) -> list[FactorValue]:
    global_ranks: dict[tuple[FactorName, str], int] = {}
    industry_ranks: dict[tuple[FactorName, str], int] = {}
    for factor_name, values in factor_values.items():
        available = [symbol for symbol in symbols if symbol in values]
        ordered = sorted(available, key=lambda symbol: (-values[symbol], symbol))
        global_ranks.update(
            {(factor_name, symbol): rank for rank, symbol in enumerate(ordered, start=1)}
        )

        grouped: dict[str, list[str]] = defaultdict(list)
        for symbol in available:
            industry = records[symbol].industry
            if industry is not None:
                grouped[industry.level3_code].append(symbol)
        for members in grouped.values():
            industry_order = sorted(
                members,
                key=lambda symbol: (-values[symbol], symbol),
            )
            industry_ranks.update(
                {(factor_name, symbol): rank for rank, symbol in enumerate(industry_order, start=1)}
            )

    result: list[FactorValue] = []
    for factor_name in sorted(factor_values):
        for symbol in sorted(factor_values[factor_name]):
            if symbol not in symbols:
                continue
            industry = records[symbol].industry
            result.append(
                FactorValue(
                    symbol=symbol,
                    factor_name=factor_name,
                    value=factor_values[factor_name][symbol],
                    global_rank=global_ranks.get((factor_name, symbol)),
                    industry_rank=industry_ranks.get((factor_name, symbol)),
                    industry_code=industry.level3_code if industry is not None else None,
                )
            )
    return result


def _passes_filters(
    symbol: str,
    *,
    config: FactorSelectionConfig,
    values: Mapping[FactorName, Mapping[str, Decimal]],
) -> bool:
    checks = (
        _in_bounds(
            values[FactorName.MARKET_CAP].get(symbol),
            config.min_market_cap,
            config.max_market_cap,
        ),
        _in_bounds(
            values[FactorName.PB].get(symbol),
            config.min_pb,
            config.max_pb,
        ),
        _in_bounds(
            values[FactorName.TURNOVER_RATE].get(symbol),
            config.min_turnover_rate,
            config.max_turnover_rate,
        ),
        _at_least(
            values[FactorName.MOMENTUM].get(symbol),
            config.min_momentum,
        ),
        _at_least(values[FactorName.ROE].get(symbol), config.min_roe),
        _at_least(
            values[FactorName.GROSS_PROFIT_MARGIN].get(symbol),
            config.min_gross_profit_margin,
        ),
        _at_least(
            values[FactorName.REVENUE_YOY].get(symbol),
            config.min_revenue_yoy,
        ),
    )
    return all(checks)


def _in_bounds(
    value: Decimal | None,
    minimum: Decimal | None,
    maximum: Decimal | None,
) -> bool:
    if minimum is None and maximum is None:
        return True
    if value is None:
        return False
    return (minimum is None or value >= minimum) and (maximum is None or value <= maximum)


def _at_least(value: Decimal | None, minimum: Decimal | None) -> bool:
    return minimum is None or (value is not None and value >= minimum)


def _order_candidates(
    symbols: Sequence[str],
    *,
    config: FactorSelectionConfig,
    records: Mapping[str, FactorInputRecord],
    factor_values: Mapping[FactorName, Mapping[str, Decimal]],
) -> list[str]:
    values = factor_values[config.ranking_factor]
    if config.ranking_scope is RankingScope.GLOBAL:
        return sorted(
            symbols,
            key=lambda symbol: (
                values[symbol] if config.ranking_ascending else -values[symbol],
                symbol,
            ),
        )

    grouped: dict[str, list[str]] = defaultdict(list)
    for symbol in symbols:
        industry = records[symbol].industry
        assert industry is not None
        grouped[industry.level3_code].append(symbol)

    ranks: dict[str, int] = {}
    industry_codes: dict[str, str] = {}
    for members in grouped.values():
        ordered = sorted(
            members,
            key=lambda symbol: (
                values[symbol] if config.ranking_ascending else -values[symbol],
                symbol,
            ),
        )
        ranks.update({symbol: rank for rank, symbol in enumerate(ordered, start=1)})
        for symbol in members:
            industry = records[symbol].industry
            assert industry is not None
            industry_codes[symbol] = industry.level3_code
    return sorted(
        symbols,
        key=lambda symbol: (
            ranks[symbol],
            industry_codes[symbol],
            symbol,
        ),
    )


def _apply_limits(
    symbols: Sequence[str],
    *,
    config: FactorSelectionConfig,
    records: Mapping[str, FactorInputRecord],
) -> list[str]:
    selected: list[str] = []
    industry_counts: dict[str, int] = defaultdict(int)
    for symbol in symbols:
        industry = records[symbol].industry
        industry_code = industry.level3_code if industry is not None else ""
        if (
            config.max_per_industry is not None
            and industry_counts[industry_code] >= config.max_per_industry
        ):
            continue
        selected.append(symbol)
        industry_counts[industry_code] += 1
        if len(selected) >= config.max_symbols:
            break
    return selected


def _snapshot(
    *,
    config: FactorSelectionConfig,
    universe: tuple[str, ...],
    business_date: date,
    decision_at: datetime,
    effective_date: date,
    dataset_versions: dict[str, str],
    selected_symbols: tuple[str, ...],
    values: tuple[FactorValue, ...],
    status: FactorSnapshotStatus,
    skip_reason: str | None,
    warnings: tuple[str, ...] = (),
) -> FactorSnapshot:
    config_data = config.as_dict()
    payload = {
        "decision_at": decision_at.isoformat(),
        "business_date": business_date.isoformat(),
        "effective_date": effective_date.isoformat(),
        "source": config.source,
        "dataset_versions": dict(sorted(dataset_versions.items())),
        "factor_version": config.factor_version,
        "static_universe": list(universe),
        "selected_symbols": list(selected_symbols),
        "values": [
            {
                "symbol": item.symbol,
                "factor_name": item.factor_name.value,
                "value": str(item.value),
                "global_rank": item.global_rank,
                "industry_rank": item.industry_rank,
                "industry_code": item.industry_code,
            }
            for item in values
        ],
        "status": status.value,
        "skip_reason": skip_reason,
        "warnings": list(warnings),
        "config": config_data,
    }
    checksum = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return FactorSnapshot(
        decision_at=decision_at,
        business_date=business_date,
        effective_date=effective_date,
        source=config.source,
        dataset_versions=dict(sorted(dataset_versions.items())),
        factor_version=config.factor_version,
        static_universe=universe,
        selected_symbols=selected_symbols,
        values=values,
        status=status,
        skip_reason=skip_reason,
        config=config_data,
        checksum=checksum,
        warnings=warnings,
    )
