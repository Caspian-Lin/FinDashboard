# OpenCode provider 持久化位置与模型目录合并行为

主题:排查「OpenCode Web 提供商/模型设置被重置到之前的时间点」(2026-09-01,issue #242)
时实测确认的 opencode v1.18.15 配置存储事实。后续排查 OpenCode 配置类问题先读这篇。

## 结论 / 事实

1. **「已连接的提供商」分两套存储**:UI 连接 + 填 API key 的 provider(zhipuai-coding-plan /
   opencode-go)写 data 卷 `/root/.local/share/opencode/auth.json`(named volume
   `opencode-data`,容器重建不丢);仓库 `.opencode/opencode.json` 声明的 provider(deepseek)
   在 UI 里徽标是「配置」而非「API 密钥」。DB 的 `credential`/`account` 表是新一代
   integrations 体系,v1.18.15 的 auth 流程不用,**恒为空**(排查时勿在此浪费时间)。
2. **agent 定义以 `.opencode/agent/*.md` 为准**:同名 agent 的 frontmatter(model /
   permission / description)覆盖 `opencode.json` 的 `agent` 块——json 里写 `model`
   是死配置(#242 已把死块删掉,.md 的 model 钉死也移除,模型跟随 UI/会话选择)。
3. **同 id 的 config provider 与内置目录合并,但内置目录是构建时快照**(#248 实测
   修正「目录自动更新」的误判):opencode 把 models.dev 目录**构建时打进镜像,
   运行时不刷新**——镜像 2026-08-07 构建时,活体 models.dev 里 zhipuai-coding-plan
   已有 10 个模型(含 8 月中旬的 glm-5.3/glm-5.3-flash)、opencode-go 有 33 个,
   容器只显示 7/18 个,缺失恰为构建后的新模型。auth 连接 provider 的模型清单
   唯一原生更新途径 = 升级镜像。#248 的修法:仓库 opencode.json 声明这两个
   provider(仅 baseURL,无 apiKey,凭证留 auth.json),渲染期同步经管理器用
   一次性容器 `:ro` cat data 卷 auth.json 预读 key 拉取 `/models` 只增不改合并。
   端点实测:Zhipu coding /models 需鉴权(401),opencode-go /models Bearer 可用,
   两者都返回 OpenAI 形 `{"data":[{"id"}]}`。
4. **config 卷 `opencode-config`(/root/.config/opencode)自 2026-08-09 初始化后零写入**:
   全局 config 只有 schema 空壳 + 一次性 node_modules 脚手架,auth 在 data 卷。该卷是
   rw 挂载,agent(root bash)理论上可写全局 config/plugin 提权——现存最小提权面,
   候选收紧(改 ro 渲染或不再挂载)。
5. **UI 里凡需要写配置文件的修改**(改 config provider 的模型、改 agent 默认模型)落在
   **只读** bind mount 上写不进去,重启后回落仓库文件状态——表现为「设置重置到之前的
   时间点」(当时=2026-08-17 #182 后的仓库文件冻结态)。`/workspace/.opencode` 与
   `.agents` 均 `:ro`,容器每次启停 `docker rm -f` 重建,容器层写入必丢。

## Why

- 容器内只有 named volume(data/config)与宿主机 bind mount 是持久的;config 面的唯一
  事实来源是仓库文件,UI 不是。混淆这两层是这次误判「provider 丢了/被覆盖」的根因
  (卷挂载本身没有「启动时旧数据覆盖新数据」的机制)。
- #242 修复:agent 解钉(.md 去 model + json 删死块)+ 渲染期对声明 baseURL 的 config
  provider `GET /models` 拉取合并(只增不改,缓存 `.opencode/runtime/provider-model-cache.json`
  回退,`opencode_model_sync_enabled` 开关,默认开)。

## How to apply

- 排查「配置丢失/重置」先分层:auth 面(data 卷,能持久)还是 config 面(仓库文件,
  改仓库才有效)。UI 徽标「API 密钥」= auth.json;「配置」= opencode.json。
- 改 agent 行为(模型/权限/prompt)改 `.opencode/agent/*.md`;改 provider/MCP 改
  `.opencode/opencode.json`;都随下次容器启动(重渲染)生效。
- 验证模型同步链路:重启 FinBoard API 后看 `.opencode/runtime/provider-model-cache.json`
  的 `fetched_at`,与 provider `/models` 实际返回对比;拉取失败回退缓存,不影响启动。
