# 测试基建关键坑:pytest-timeout 行为、SQLAlchemy URL 脱敏与守卫注入

- 日期:2026-08-16(issue #166 / #167,PR #177)
- 主题:挂死兜底超时与独立测试库实施中验证出的框架行为

## 结论 / 事实

1. **pytest-timeout 2.4 的 thread 方式超时后直接 `os._exit(1)`**(dump 堆栈后自杀进程),不是向测试注入异常;且线程注入无法打断阻塞在 C 层(select/IOCP)的事件循环 —— 对 asyncio 挂死它只能进程级兜底,给不出单测试失败。
2. **pytest-asyncio 1.4 无 `asyncio_timeout` 配置**(0.26 引入的该功能在 1.x 已移除),全局 async 超时需自包:`pytest_collection_modifyitems` 替换 `item.obj`(pytest-asyncio 的 `PytestAsyncioFunction.runtest` 运行时读 `self.obj`)。守卫超时须比 pytest-timeout 生效值小(实现取 -1s),否则 watchdog 竞态先杀进程。
3. **循环内闭包 late-binding 是守卫注入的经典坑**:`async def _guarded(): await orig()` 直接写在 for 循环里,所有闭包共享循环结束后的最后一个 `orig`(曾致挂死探针 0.03s「通过」——实际执行了别的测试);必须用工厂函数按参数绑定。
4. **`str(make_url(url).set(database=...))` 会脱敏密码输出 `***`**,create_async_engine 拿到错误凭据 → 「psycopg 直连成功但 SQLAlchemy 连不上」的假象;须 `render_as_string(hide_password=False)`。
5. 本机环境变更:WSL PostgreSQL 的 `findashboard` 角色已 `ALTER ROLE ... CREATEDB`,且已手动建 `findashboard_test` 库(测试库隔离所需;conftest 会自动建库,CI 的 postgres 服务用户是 superuser 可直接建)。

**Why:** #167 要同时覆盖 sync/async 两类挂死,Windows 无 signal,且测试要与 dev 库彻底隔离;上述框架行为不实测读源码无法预知。

**How to apply:** 后续动超时/守卫/测试库时:
- 探针验收(sync 进程级 + async 优雅)在 `tests/unit/test_hang_guard.py`,默认 `-m "not hang_guard"` 排除,验收命令见 README §3.1;
- 新增长测试用 `@pytest.mark.timeout(N)` 调大或 `timeout(None)` 豁免(同步改 asyncio 守卫的取值源);
- 测试库 URL 解析优先级 `FINBOARD_TEST_DB_URL` → `FINBOARD_DB_URL` → 默认 `findashboard_test`;共享库覆盖时才保留 `_PRESERVE_TABLES` 语义;
- 排查僵尸锁先用 `pg_stat_activity` 找 `idle in transaction` 指纹再 terminate,勿盲目重跑(见 [[pg-lock-hygiene]])。
