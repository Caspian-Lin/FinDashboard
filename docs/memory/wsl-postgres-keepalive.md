# WSL PostgreSQL 空闲自动关机——本机集成测试「卡住」的根因

**主题**:本机(Windows)的 PostgreSQL 跑在 WSL 里,WSL2 VM 空闲约 60 秒自动关机,
导致 DB 相关测试表现为「卡住 / 超时」。

**事实**:`pg_isready` 在 WSL 内部探测的是 WSL 自己的 loopback,不能证明 Windows
侧可达;Windows→WSL 的 localhost TCP 端口转发**不会唤醒**已关机的 VM,psycopg
只会 ConnectionTimeout 长时间重试(2026-08-15 排查确认,曾误判为「契约测试在
数据库调用测试处超时」)。

**Why**:psycopg 默认没有快速失败,连接超时前看起来像卡死。

**How to apply**(跑集成测试前):

1. 唤醒:`wsl.exe -u root -e sh -lc "systemctl start postgresql; pg_isready -h 127.0.0.1 -p 5432"`
2. 保活(后台):`wsl.exe -u root -e sh -lc "sleep 900"`(命令间隙 VM 可能又睡)
3. Windows 侧用 `.env` 里的真实密码验证 psycopg 可连,才算真的通。

**本机其它环境坑**(同一次排查发现):

- pytest `tmp_path` 权限问题(`pytest-of-<user>` 目录被锁,fixture setup 全体
  PermissionError):加 `--basetemp=C:/Users/28491/AppData/Local/Temp/finboard-pytest-tmp -p no:cacheprovider` 绕过。
- `data_releases/` 下有锁定目录导致 `mypy .` 失败:改跑 `mypy packages/ tests/ migrations/`。
- Windows 上 `asyncio` 直接连 psycopg 需 `WindowsSelectorEventLoopPolicy`(否则
  Proactor 报错)。

相关:[[issue-157-status]]
