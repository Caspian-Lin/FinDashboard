# 研究记忆使用规则(详细)

## 为什么需要记忆

研究 Agent 的会话是短暂的,但研究上下文需要跨会话积累。`finboard.memory.*`
工具把关键发现、假设、教训持久化到 `research_memories` 表,下次会话可查询复用。

## 记忆类型(memory_type)

| 类型 | 用途 |
|------|------|
| `note` | 一般研究笔记(观察、数据特征、配置说明) |
| `insight` | 经过推理得出的洞见(因子表现、策略行为) |
| `correction` | 纠正先前错误记忆(通过 `correct` 自动产生) |
| `confirmation` | 经验证确认的结论(配合 `confirm`) |

## 生命周期

```
remember → active
  │
  ├─ forget   → forgotten  (软删除,保留审计)
  ├─ archive  → archived   (归档,仍可查询)
  ├─ correct  → 新 active 记忆 + 旧的 forgotten (纠正链)
  └─ confirm  → active + confirmed_by/confirmed_at
```

## source_refs 规则

- **只引用,不修改** —— `source_refs` 记录记忆关联的产物 ID,绝不修改产物本身。
- 结构:`{kind, ref_id, label?}`。
- `kind` 应与产物类型一致(如 `research_run` / `simulation` / `dataset`)。
- `ref_id` 用产物的业务 ID(如 `RR-xxx` / `SIM-xxx`)。

## 何时记住

✅ **应该记住**:
- 关键研究发现(「RR-abc 在 2024Q1 样本外夏普 1.2,但 2023 有过拟合迹象」)
- 数据特征(「akshare 沪深300 成分股每半年调整,需注意存活偏差」)
- 策略行为洞见(「均线交叉策略在震荡市频繁假信号」)
- 失败教训(「该因子在期货贴水结构下失效」)

❌ **不应记住**:
- 临时计算中间结果(用完即弃)
- 可从产物直接查询的信息(直接查 Run)
- 敏感凭证(凭证不在记忆范围内)

## 纠正 vs 归档 vs 忘记

- **发现记忆有错** → `correct`:新建正确记忆,旧的自动 forgotten,
  经 `supersedes_id` 形成纠正链。**保留审计轨迹**。
- **记忆过时但有参考价值** → `archive`:归档不删除,仍可查询。
- **记忆确需移除(冗余/误记)** → `forget`:软删除,保留审计。
  **优先用 archive 而非 forget**,审计完整性优先。

## 权限边界

- 记忆工具只写 `research_memories` 独立表。
- **不写**实盘 orders / fills / positions / audit_logs。
- **不创建** ResearchRun / 回测 / 模拟盘。
- **不修改**被 `source_refs` 引用的产物。
- `created_by` 由工具自动标记(`agent:mcp` / `user:api`),不可伪造。
