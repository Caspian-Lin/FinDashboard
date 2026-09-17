"""数据集驱动统一同步框架(issue #392;后续数据集扩展的地基)。

两层结构:

* :mod:`spec` —— :class:`SyncSpec` 规格 + :data:`SYNC_SPECS` 注册表 +
  枚举形态 / 行级口径推导(#389 口径固化)与切片生成;
* :mod:`specs` —— 内置十一集(profiles / name_changes / convertible_profiles /
  daily_metrics / suspensions(#396)/ financial_indicators / industry_memberships /
  三表 income/balance/cashflow + dividends(#397))的规格注册;
* :mod:`runner` —— :class:`DatasetSyncExecutor`(``kind=dataset_sync``),
  框架统一消费注册表:进度上报 / #383 timing(worker 通用层)/ 行级口径
  分发 / TushareBudget 共享 / ``research_sync_batches`` 记账;
* :mod:`scope` —— scope 四元组(exchange / listing_boards / instrument_type /
  symbols)归一解析,#385 语义与 bulk_download 共享同一函数。

## 新增数据集 SyncSpec 接入指南(#394/#395/#397 照此接入;#396 suspensions
## 已按本指南接入,可作参照)

最小示例(= 已接入的 ``suspensions`` 停复牌,tushare ``suspend_d``,
按日全市场):

1. **声明规格**(在 :mod:`specs` 里加一段;通用逻辑全部复用框架,不要在
   Spec 里手写进度 / 限流 / 记账):

   .. code-block:: python

       async def _fetch_suspensions(
           provider: ResearchDataProvider, query: SliceQuery
       ) -> list[Any]:
           # query.row_policy 由框架按形态推导,原样透传给 provider。
           assert query.trade_date is not None
           return await provider.fetch_suspensions(
               query.trade_date, dirty_row_policy=query.row_policy
           )

       async def _persist_suspensions(
           context: PersistContext, records: list[Any]
       ) -> PersistResult:
           service = ResearchDataSyncService(context.session_maker)
           batch = await service.sync_suspensions(
               source=context.source,
               dataset_version=context.dataset_version,
               code_version=context.code_version,
               parameters={"trade_date": context.query.trade_date.isoformat()},
               raw_payload=None,
               records=list(records),
           )
           return PersistResult(accepted_rows=batch.accepted_rows)

       SyncSpec(
           name="suspensions",
           shape=EnumShape.DAILY_MARKET,     # 切片 / 行级口径由形态推导
           title="停复牌日历(tushare suspend_d,按交易日全市场)",
           fetch=_fetch_suspensions,
           persist=_persist_suspensions,
           slice_version=lambda query: f"suspensions:{query.trade_date.isoformat()}",
           slice_parameters=lambda query: {
               "trade_date": query.trade_date.isoformat()
           },
       ),

   ``SYNC_SPECS.register(spec)``(走 :func:`specs.register_default_specs` 的
   注册循环)即生效:payload ``datasets`` 白名单、缺省全集、进度切片、
   行级口径、幂等键、批次记账全部自动获得。

2. **provider 绑定**:给 ``TushareResearchDataProvider`` 加对应 fetch 方法,
   返回 :mod:`finboard_data.research` 的领域记录;解析遵守 Spec 形态对应的
   行级口径(``dirty_row_policy="skip"`` → ``_parse_rows_skipping_dirty``;
   ``"reject"`` → 单行违规抛 ``ResearchDataContractError`` 整批拒)。

3. **落点**:批次发布语义的新表经 ``ResearchDataSyncService.sync_*`` +
   ``ResearchDatasetRepository.upsert_*``(照抄 daily_metrics 四件套:领域
   记录 / validator / upsert / sync 方法);主数据语义(无批次)照抄
   name_changes / convertible_profiles 的 persist 直接写表。

4. **发布消费**(可选):数据要进冻结发布时,另在发布白名单
   (``dataset_release_publish`` 的 dataset kind 与字段白名单)登记 ——
   「新增一个数据集」= 注册一个 SyncSpec + 发布白名单两处。

5. **回归**:golden 集成对照(``tests/integration/test_issue_392_golden_dataset_sync.py``)
   加同款固定 mock 行场景,锁定 dataset_version 形状与批次 parameters。
"""

# 注册内置六集(import 副作用,幂等)。
from finboard_backtest.background_jobs.dataset_sync import specs  # noqa: F401
from finboard_backtest.background_jobs.dataset_sync.runner import DatasetSyncExecutor
from finboard_backtest.background_jobs.dataset_sync.scope import (
    ScopeValueError,
    SyncScope,
    normalize_sync_scope,
)
from finboard_backtest.background_jobs.dataset_sync.spec import (
    ROW_POLICY_BY_SHAPE,
    SYNC_SPECS,
    EnumShape,
    RowPolicy,
    SliceQuery,
    SyncSpec,
    SyncSpecRegistry,
    UnknownDatasetError,
    slices_for_spec,
)

__all__ = [
    "ROW_POLICY_BY_SHAPE",
    "SYNC_SPECS",
    "DatasetSyncExecutor",
    "EnumShape",
    "RowPolicy",
    "ScopeValueError",
    "SliceQuery",
    "SyncScope",
    "SyncSpec",
    "SyncSpecRegistry",
    "UnknownDatasetError",
    "normalize_sync_scope",
    "slices_for_spec",
]
