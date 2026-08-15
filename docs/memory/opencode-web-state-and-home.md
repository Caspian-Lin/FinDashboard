# OpenCode Web 工作台:SPA 状态与容器环境踩坑

主题:FinBoard 研究工作台(iframe 嵌入 Docker 内的 `opencode web`)「读不到 project / 历史会话空白」的根因与修复。

## 结论 / 事实

1. **项目列表与「最近会话」状态 100% 在浏览器侧**(IndexedDB,按 origin + iframe 分区隔离),服务端 `/api/project` 返回的是运行时内存注册表,DB `project` 表只是持久化层。直接往 DB 插 `project` 记录不生效(容器重启/`/api/project` 均不返回);服务端也没有注册项目的 API。**服务端无法预置 SPA 的项目状态**。
2. `?directory=/workspace` URL 参数**不影响 SPA 的「最近会话」显示**(v1.18.15 实测):它只被服务端 `jV()`(请求目录解析)消费,而服务端默认 location 本来就是 cwd(`/workspace`),参数是冗余的。SPA 只在 deep link(`opencode://open-project?directory=...`,经 `window.__OPENCODE__.deepLinks` 或 `opencode:deep-link` 事件注入)里读 directory;跨源 iframe 无法注入,故前端 URL 参数方案不可行。
3. **XDG_*_HOME 的取值是「数据根」**:opencode 会追加 `opencode/` 子目录(`XDG_DATA_HOME=/tmp/x` → 数据在 `/tmp/x/opencode/`)。若把 XDG_DATA_HOME 指向 volume 挂载点本身,数据会写进嵌套目录,旧数据读不到(表现为 `/api/session` 返回 0)。
4. **HOME 决定文件选择器初始目录**(服务端 `project.homedir`),且 UI 的「打开项目」列表**隐藏 homedir 目录**(HOME=/workspace → 列表里没有 workspace;HOME=/root → 没有 root)。因此 HOME=/workspace 时,打开 workspace 项目的操作是:添加项目 → 搜索框输入 `/` → 点击 `~`(主目录)。
5. **坏路径会话污染**:SPA 曾把宿主机路径(`C:/Users/...`)当 directory 发给容器,服务端拼出 `/workspace/C:/Users/...` 后 ENOENT,且该坏路径会作为会话落库;若 SPA 恢复到此会话会再次失败。清理方式:`docker stop` 后 `docker run --rm -v opencode-data:/root/.local/share/opencode --entrypoint opencode <镜像> db "DELETE FROM session WHERE directory LIKE '/workspace/C:%'"`(注意 volume 挂载点要与 opencode 数据路径一致,否则误操作新库)。

## Why

- 容器内 opencode 以 root 运行,默认 HOME=/root,文件选择器从 /root 开始;而 /workspace 是独立挂载点,不在 /root 下,用户「搜不到 workspace」。
- 每次容器重建/FinBoard API 重启后,SPA 若读到坏路径会话或全新分区,「最近会话」空白,用户误以为数据丢失(实际 volume 数据完好)。

## How to apply

- `OpenCodeProcessConfig` 已注入 `HOME=/workspace` + `XDG_DATA_HOME=/root/.local/share` + `XDG_CONFIG_HOME=/root/.config` + `XDG_STATE_HOME=/root/.local/state`(见 `process_manager.py` `_CONTAINER_ENV_DIRS`):数据落点 = volume 挂载点,文件选择器从 /workspace 开始。
- 前端 `ResearchWorkbench` 有首次使用引导:「添加项目 → 输入 `/` → 点 `~`」;引导文案与 `opencode-url.ts` 注释保持同步(openccode 行为变更时需复核)。
- 遇到「历史空白」先查:`/api/session` 条数(服务端数据是否完好)、浏览器分区是否打开过项目、DB 里是否有 `/workspace/C:%` 坏路径会话。
- 升级 opencode 镜像版本后重新验证:XDG 子目录语义、homedir 隐藏规则、deep link 机制(上游可能变化)。
