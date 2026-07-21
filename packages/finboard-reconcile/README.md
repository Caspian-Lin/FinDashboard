# finboard-reconcile

本地 ↔ 券商核对(Reconciliation)。

P0 阶段提供最小核对能力,以 **券商查询结果** 为真值,把差异写入
``reconciliation_logs`` 表;发现不一致时上层应触发 Kill Switch REDUCE_ONLY。

## 范围

* 订单核对:本地活动订单 vs broker 活动订单,状态 / 成交数量是否一致;
* 持仓核对:本地 local 源 vs broker 源,数量 / 均价差异是否在容差内;
* 资金核对:本地账户快照 vs broker 查询,cash / frozen_cash 是否一致;
* 每次核对生成 :class:`ReconciliationReport`,详细差异写入 DB。

## 不在 P0 范围

* 自动修复本地状态(需要用户确认 + 审计)
* 历史 replay(成交回放重算)
* 跨日核对、T+1 处理
