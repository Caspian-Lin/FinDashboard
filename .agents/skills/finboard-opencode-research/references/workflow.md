# 研究工作流(详细)

## 决策树

```
收到研究请求
  │
  ├─ 是实盘交易 / 下单 / 持仓 / Kill Switch?
  │    → 拒绝。这些能力永久不在工具集中。
  │
  ├─ 要生成 Python / 可执行代码?
  │    → 拒绝。策略是无代码版本化规格。
  │
  ├─ 查询现有 ResearchRun / artifact?          ✅ 已实现
  │    → finboard.run.list / .get / .artifacts (只读,直接调用)
  │
  ├─ 记住 / 纠正 / 查询研究上下文?              ✅ 已实现
  │    → finboard.memory.* (直接执行)
  │
  ├─ 查询标的 / 数据集发布 / 缓存状态 / 数据质量 / Tushare 配额?  ✅ 已实现
  │    → finboard.instrument.* / .dataset.* / .data.* / .tushare.* (#124 只读)
  │
  ├─ 查询 / 构建因子目录 / 特征快照 / 因子信号 / 因子实验?  ✅ 已实现
  │    → finboard.factor.* / .feature_snapshot.* (#125,7 只读 + 4 写)
  │      catalog / snapshot list/get/create/job_start/job_status /
  │      signal list/get / experiment list/get/create/sync_validation
  │
  ├─ 查询 / 构建策略规格(无代码版本化)?       ✅ 已实现
  │    → finboard.strategy.* / .preset.* (#126,8 只读 + 8 写)
  │      registry / template / list / history / version_get / diff
  │      (只读);validate(纯计算预览)/ draft_create / supersede /
  │      publish / rollback、preset create/update/delete(写)
  │      反复 validate 预览 → draft → publish 闭环
  │
  ├─ 查询 / 管理 ResearchRun 生命周期?           ✅ #127
  │    → finboard.run.*(7:list/get/artifacts 只读 + queue/cancel/replay/lineage 写)
  │      queue 冻结输入 + 登记 queued(不执行,由离线 worker 完成);
  │      cancel / replay / lineage 覆盖生命周期
  │
  ├─ 运行回测(行情回放 + 纸面撮合)?            ✅ #127
  │    → finboard.backtest.*(5:strategy_list 只读、run 写、history_list/get 只读、
  │      history_delete 写)。同步运行返回 metrics/equity/fills/snapshots
  │
  ├─ 查询 / 启动模拟盘?                          ✅ #127
  │    → finboard.sim.*(17:account/session 生命周期、decision_submit、
  │      order_cancel 写;orders/fills/positions/ledger/audit/report 只读)
  │      decision_submit 提交结构化目标仓位 → 生成订单(agent 不直接创建订单)
  │
  └─ portfolio 计算(allocate/sizing/feasibility/attribution)?  ✅ #128
       → finboard.portfolio.*(4:allocate 目标权重分配 + 约束 + 风险报告、
         sizing 离散手数 + 费用/保证金、feasibility 10万/20万/50万档位可行性、
         attribution 绩效归因分解)。纯计算,无 DB 写入,agent 自主执行
```

## 会话启动协议(#268)

每轮会话在理解问题**之前**按序执行,目的:不重复造轮子、不重测已证伪假设。

1. **读研究文档**(只读挂载 `/workspace/docs/research/`):
   - `ROADMAP.md` —— 研究目标阶梯 / 路线 A-D 状态 / 当前证据门 / 数据边界;
   - `FINDINGS.md` —— 结论注册表(结论 / 置信度 / 证据 ID / 失效条件 /
     状态:有效 / 待验证 / 已证伪)。
   - 挂载缺失(目录不存在)→ 跳过本步,并在最终回答注明「研究文档不可见」。
2. **扫记忆**:`finboard_memory_list(status=active)`;量大按保留 tag 过滤
   (`research-plan` / `research-round` / `finding-confirmed` /
   `finding-refuted`)。
3. **检索产物**:按问题域查相关 ResearchRun / 实验(`finboard.run.list` /
   `finboard.validation_experiment_list`),把既有证据拼进本轮上下文。

**重测禁令**:FINDINGS / 记忆中状态为「已证伪」的假设,未查索引前禁止重测;
重开必须满足全部条件——(a) 有新证据来源(新数据域 / 新构造 / 新频率 /
新 universe);(b) 显式引用旧结论及其证据 ID;(c) 说明本次与既往证伪的
差异;(d) 结论更新后同步改 FINDINGS 状态并留 `finding-refuted` /
`finding-confirmed` 记忆链。

## 标准研究循环

0. **会话启动** —— 执行上方「会话启动协议」,加载前情后再进入澄清。
1. **澄清** —— 复述研究问题,声明所需数据。数据不足时**先说**「数据不足」,
   不要猜测。
2. **检索** —— 用 `finboard.run.list` / `finboard.run.get` 查询相关 ResearchRun;
   用 `finboard.memory.list` 查询是否已有相关研究记忆。
3. **假设** —— 你自己(OpenCode LLM)直接给出结构化研究假设(经济机制 /
   输入字段 / 预期失败场景 / 方向 / 参考文献);需要机器验证时创建 #57
   验证实验(`finboard.validation_experiment.*`)或因子实验
   (`finboard.factor.experiment_*`)走 OOS 闭环。
4. **记忆** —— 用 `finboard.memory.remember` 记住关键发现,`source_refs` 关联
   具体产物(ResearchRun ID / 数据集版本 / 模拟盘 ID)。
5. **回答** —— 引用来源,区分个人假设与机器验证结论(OOS 终态)。

## 轮次收尾协议(#268)

每轮研究结束前,两步收尾:

**第 1 步:落一条固定模板记忆**(跨会话检索入口):

```
finboard_memory_remember(
  memory_type="insight",
  tags=["research-round"],          # 结论性发现追加 finding-confirmed / finding-refuted
  content="""
    目标:本轮要回答什么(引用 ROADMAP 目标阶梯 / 路线编号)
    动作:做了什么(建规格 / 入队 run / 建实验,附关键参数)
    证据:产物 ID 表(ResearchRun / 实验 / 数据集发布,逐行列结论相关性)
    结论+置信度:各假设的最新状态(高 / 中 / 低 + 一句话依据)
    开放问题:留给下一轮的清单
  """,
  source_refs=[{kind: "research_run", ref_id: "RR-..."}, ...],
)
```

**第 2 步:回答末尾附「轮次摘要」**(同模板)——容器对
`/workspace/docs/research` 只读,canonical 轮次文档
(`rounds/YYYY-MM-DD-<slug>.md`)由用户 / 主 coding agent 经 PR 落库;
你的记忆只是指针,**文档与记忆冲突时以文档为准**。摘要要让主 agent
能原样转录成轮次文档,不要省略证据 ID。

## 因子假设的结构化要求

每条假设必须包含:
- **经济机制** —— 为什么这个因子应该有效?
- **输入字段** —— 用哪些数据字段?
- **预期失败场景** —— 在什么情况下会失效?(可证伪)
- **方向** —— 正向 / 反向?
- **参考文献** —— 学术 / 项目内来源。

## 常见反模式(避免)

- ❌ 编造回测数字 —— 必须引用 ResearchRun ID。
- ❌ 把 `proposed` 草案当结论 —— 草案需机器验证后才可视为结论。
- ❌ 直接修改研究产物 —— 记忆 `source_refs` 只引用,不改产物。
- ❌ 跨域操作 —— 研究工具不触碰实盘订单 / 持仓 / Kill Switch。
- ❌ 把 portfolio 纯计算结果当实盘可执行 —— sizing/feasibility 输出是研究
  估算,不是实盘下单信号;需经完整研究流程才可上实盘。
- ❌ 未查结论索引就重测已证伪假设(#268)—— 先走会话启动协议;重开需
  新证据 + 显式引用旧结论。

> 完整研究流程(数据→因子→策略→回测→模拟→评估)详解见
> `references/research-workflow.md`。
