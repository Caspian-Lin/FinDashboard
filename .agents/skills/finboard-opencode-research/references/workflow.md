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
  ├─ 金融问答 / 因子假设 / 策略草案?            ✅ 已实现
  │    → finboard.ai.* (产出草案,标注 proposed,可追溯)
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
  ├─ 查询策略 / 回测 / 模拟 / portfolio?  🔒 planned
  │    → 当前 MCP 工具尚未覆盖(#126-#128)。
  │      告知用户「该能力尚未通过 MCP 暴露」,不要编造结果。
  │      可通过 finboard.ai.ask 以 AI 问答形式回答(引用来源)。
  │
  └─ 创建回测 / 模拟盘 / 因子 / 策略?           🔒 planned
       → 研究写工具尚未实现(扩展中)。告知用户当前限制。
```

## 标准研究循环

1. **澄清** —— 复述研究问题,声明所需数据。数据不足时**先说**「数据不足」,
   不要猜测。
2. **检索** —— 用 `finboard.run.list` / `finboard.run.get` 查询相关 ResearchRun;
   用 `finboard.memory.list` 查询是否已有相关研究记忆。
3. **假设** —— 用 `finboard.ai.propose_hypothesis` 生成结构化因子假设草案
   (经济机制 / 输入字段 / 预期失败场景 / 方向 / 参考文献)。
4. **记忆** —— 用 `finboard.memory.remember` 记住关键发现,`source_refs` 关联
   具体产物(ResearchRun ID / 数据集版本 / 模拟盘 ID)。
5. **回答** —— 引用来源,区分 `proposed`(草案)与已验证结论。

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
- ❌ 假装 planned 工具可用 —— #126-#128 尚未实现,如实告知用户限制。

> 完整研究流程(数据→因子→策略→回测→模拟→评估)详解见
> `references/research-workflow.md`。
