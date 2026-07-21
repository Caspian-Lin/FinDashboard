# finboard-persistence

SQLAlchemy 2.0 ORM 模型 + Repository + Alembic 迁移。

## 数据库选型

* **PostgreSQL 16**(开发/CI 用 docker-compose 起的容器);
* **psycopg v3**(`postgresql+psycopg` 方言),原生支持 sync + async,避免引入两个驱动;
* 业务运行走 ``AsyncEngine`` / ``AsyncSession``,Alembic 迁移走 sync 连接(在 env.py 内创建)。

## 表设计(P0 范围)

| 表 | 说明 | 真实来源 |
|----|------|---------|
| ``accounts`` | 账户资金快照(本地缓存) | 券商查询 |
| ``orders`` | 本地订单全生命周期 | 本地 |
| ``fills`` | 每笔成交明细 | 券商回报 |
| ``positions`` | 持仓快照,分 ``local`` / ``broker`` 两行 | 本地推导 + 券商查询 |
| ``reconciliation_logs`` | 每次核对的差异记录 | 本地 |
| ``audit_logs`` | 关键状态变化、人工操作的审计行 | 本地 |

## 关键索引

* ``orders.client_order_id`` UNIQUE — **防重复下单的最后一道闸**(应用层 + DB 双重);
* ``(account_id, symbol, position_side, source)`` UNIQUE — 持仓按维度聚合;
* ``fills.broker_fill_id`` UNIQUE — 防止回报重放导致重复入账。

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
