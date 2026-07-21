# finboard-broker-qmt

QMT (迅投 xtquant) 券商适配器,**目标市场:A 股**。

## 当前状态:stub

P0 阶段不实现真实下单,本包仅占位 + 文档。真实 QMT 接入属于 P1。

## 计划实现要点(后续填充)

* 通过 ``xtquant.xttrader.XtQuantTrader`` 建立交易通道,``xtquant.xtdata`` 拉行情;
* 必须在 Windows + QMT 客户端已登录环境运行;CI 中无法跑通,故实现仅做动态加载;
* ``connect()`` 注入 ``path`` (mini QMT 路径)与 ``session_id``;
* ``client_order_id`` 使用本系统 ``generate_client_order_id()``,映射到 QMT 的
  ``XtOrder.order_remark`` 字段用于反向追溯;
* 回报通过注册 ``XtQuantTraderCallback`` 在独立线程接收,内部 ``asyncio.Queue``
  + ``loop.call_soon_threadsafe`` 桥接到事件循环;
* **下单超时**严格走 ``BrokerTimeoutError`` 流程,绝不自动重发。

## 部署

```bash
uv sync --all-packages --extra qmt-runtime --extra ctp-runtime
```

或单独安装:

```bash
uv pip install xtquant
```
