# 本地 PostgreSQL 僵尸锁卡死测试的根因与防护

- 日期:2026-08-15(诊断于 issue #160 本地验证期间)
- 主题:集成测试无限挂起 / CPU 接近零 / 进度不动

## 结论 / 事实

全量 `pytest tests/` 挂死、pytest 进程 CPU 极低的根因**不是测试慢,而是
PostgreSQL 锁等待**:库里存在数小时前的 `idle in transaction` 僵尸会话,
后续测试 fixture 的 `clean_tables()`(`DELETE FROM accounts` 等)排在它
持有的锁后面无限等待。本机实锤:一个 4 小时的僵尸事务(查询为
`research_dataset_releases` 的 SELECT,时间与 `finboard dev` 启动吻合,
疑似 dev server 侧请求路径泄漏的事务)把 kernel 的 `UPDATE accounts`
堵了 3 小时,又把测试的清表堵了 15+ 分钟。

**Why:**

- Windows 上杀掉测试的外层 shell 不会杀干净 python 进程树,被杀瞬间
  未提交的事务在服务端残留;PG 对死连接的回收依赖 TCP 探测,可能很慢;
- 长跑的 `finboard dev` 也可能因请求异常路径泄漏 `idle in transaction`;
- 集成测试共享同一个开发库,任何残留锁都会传导到 `clean_tables()`。

**How to apply(已落地的防护 + 排查命令):**

1. `tests/integration/conftest.py` 的 `_engine` 已设
   `lock_timeout=10s`(connect_args options):等锁超时直接报错,测试
   可见地失败而不是挂死。
2. 本地角色级保险丝(对**新连接**生效,可 `ALTER ROLE findashboard
   RESET idle_in_transaction_session_timeout` 撤销):
   `ALTER ROLE findashboard SET idle_in_transaction_session_timeout='15min';`
   僵尸事务 15 分钟后被服务端自动回收,不再需要人工清。
3. 卡住时先诊断再动手(不要盲目重跑):
   ```sql
   select pid, state, wait_event_type, wait_event, left(query,80) as q,
          now()-xact_start as xact_age
   from pg_stat_activity
   where datname is not null and pid <> pg_backend_pid()
   order by xact_start nulls last;
   ```
   `idle in transaction` + `xact_age` 很大的就是元凶,
   `select pg_terminate_backend(<pid>);` 清掉后其余等锁会话自动解锁。
4. 遗留线索:dev server 疑似泄漏事务的端点(候选:research 数据发布
   相关 GET)值得单独 issue 排查——请求异常路径上 session 未
   rollback/commit。
