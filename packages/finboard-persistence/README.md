# finboard-persistence

SQLAlchemy 2.0 ORM 模型 + Repository + Alembic 迁移。

## 数据库选型

* **PostgreSQL 16**(开发/CI 用 docker-compose 起的容器);
* **psycopg v3**(`postgresql+psycopg` 方言),原生支持 sync + async,避免引入两个驱动;
* 业务运行走 ``AsyncEngine`` / ``AsyncSession``,Alembic 迁移走 sync 连接(在 env.py 内创建)。

## 表设计

| 表 | 说明 | 真实来源 |
|----|------|---------|
| ``accounts`` | 账户资金快照(本地缓存) | 券商查询 |
| ``orders`` | 本地订单全生命周期 | 本地 |
| ``fills`` | 每笔成交明细 | 券商回报 |
| ``positions`` | 持仓快照,分 ``local`` / ``broker`` 两行 | 本地推导 + 券商查询 |
| ``reconciliation_logs`` | 每次核对的差异记录 | 本地 |
| ``audit_logs`` | 关键状态变化、人工操作的审计行 | 本地 |
| ``research_sync_batches`` | 研究数据原始响应、参数、质量结果和发布状态 | 外部数据源 |
| ``research_instrument_profiles`` | 带来源和版本的标的档案快照 | 外部数据源 |
| ``research_daily_metrics`` | 每日估值、流动性、股本和市值 | 外部数据源 |
| ``research_financial_indicators`` | 按公告日和修订标识保留的财务指标 | 外部数据源 |
| ``research_industry_classifications`` | 版本化行业分类字典 | 外部数据源 |
| ``research_industry_memberships`` | 含三级编码和有效区间的行业成员历史 | 外部数据源 |
| ``factor_snapshots`` | T 日决策、T+1 生效的版本化候选池和质量状态 | 本地研究计算 |
| ``factor_values`` | 快照内规范化因子值及全局/行业排名 | 本地研究计算 |

## 关键索引

* ``orders.client_order_id`` UNIQUE — **防重复下单的最后一道闸**(应用层 + DB 双重);
* ``(account_id, symbol, position_side, source)`` UNIQUE — 持仓按维度聚合;
* ``fills.broker_fill_id`` UNIQUE — 防止回报重放导致重复入账。

## 研究数据同步与发布

`ResearchDataSyncService` 以 `(dataset, source, dataset_version)` 作为同步批次
幂等键。调用方在 Provider 边界同时传入规范化记录和 JSON 兼容的原始响应:

1. 批次参数、原始响应、代码版本和预期/实际行数先写入
   `research_sync_batches`。`token`、`password`、`secret` 等敏感键会被替换为
   `***`。
2. 质量门检查空集、覆盖率、重复键、来源混入、时区、陈旧度、业务日期、单位
   范围和异常值。
3. 只有 `passed` 批次才会在同一事务中写入规范化表并标记 `published`。
   `partial` / `failed` 批次不产生可查询数据,也不会替换最近发布版本。
4. 数据库写入中断会回滚整个规范化事务并把批次标记为 `failed`;调度器可用同一
   `dataset_version` 重试。已发布版本不可变,重复请求直接返回原批次。

Repository 查询必须显式给出 `source` 和带时区的 `decision_at`,并只返回
`available_at <= decision_at` 的记录。未指定 `dataset_version` 时只解析该来源
最近发布的完整版本,不会跨来源或跨版本拼接。行业成员另按
`valid_from <= business_date <= valid_to` 过滤;财务公告的不同修订分别保留。

```python
from finboard_persistence import ResearchDatasetRepository

repo = ResearchDatasetRepository(session)
metric = await repo.get_daily_metric_as_of(
    symbol="000001.SZ",
    trade_date=trade_date,
    decision_at=decision_at,
    source="tushare",
)
```

`ResearchDatasetRepository.load_factor_inputs()` 用于横截面批量读取。日指标按
业务日解析包含该日期的已发布批次,其他数据集按显式版本或最近发布版本解析;不会
逐标的选择不同版本。`FactorSnapshotRepository` 以 SHA-256 内容校验和幂等保存
快照及因子值,相同输入重复运行会复用原快照。

## 使用

```python
from finboard_persistence.engine import create_async_engine
from finboard_persistence.session import AsyncSessionMaker
from finboard_persistence.repo import OrderRepository

engine = create_async_engine("postgresql+psycopg://...")
async with engine.session() as session:
    repo = OrderRepository(session)
    await repo.add(order)
```
