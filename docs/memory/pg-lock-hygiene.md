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
4. 遗留线索(2026-08-16 已由 issue #166 定位并修复):泄漏根因是
   `finboard_api/routes/instruments.py`(多资产元数据 + 研究数据发布,前端
   研究数据页)与 `audit.py` 的数据域路由挂在 kernel 共享的长生命周期
   session(`get_session`)上,只读 GET 从不 commit/rollback,事务在共享
   连接上永久悬挂 —— 与「僵尸事务最后查询是 research_dataset_releases
   的 SELECT、事务开始时间吻合 dev server 启动」完全对应。修复:数据域
   路由统一改用每请求独立 session(`get_db_session`,异常路径自动 rollback);
   回归测试 `tests/integration/test_session_hygiene.py` 断言请求(含异常
   路径)后 `pg_stat_activity` 无 `idle in transaction` 残留。另配套
   issue #167:集成测试改用独立测试库 `findashboard_test`
   (`FINBOARD_TEST_DB_URL`),测试与 dev server 不再共享库,僵尸锁不再
   传导到测试清表;pytest 挂死由 pytest-timeout + `asyncio.timeout` 守卫
   兜底(探针 `tests/unit/test_hang_guard.py`,见 README §3.1)。
