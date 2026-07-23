# finboard-scheduler

进程内 asyncio 定时任务调度器。

## 功能

- **交易日历**:A股交易日判断(周末 + 节假日排除)
- **定时调度**:每日定时(at HH:MM)和固定间隔(interval)两种模式
- **内置任务**:盘前检查 / 收盘撤单 / 日终核对 / 连接心跳

## 设计

不引入 APScheduler / Celery / Redis,完全基于 asyncio 原生实现。

```
Scheduler
  ├── TradingCalendar  — 交易日判断
  └── ScheduledTask[]  — 任务列表
        ├── time-based     — 每日定时(Asia/Shanghai)
        └── interval-based — 固定间隔(秒)
```
