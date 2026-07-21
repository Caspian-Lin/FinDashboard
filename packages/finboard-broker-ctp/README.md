# finboard-broker-ctp

CTP 期货券商适配器,**目标市场:期货**(支持 openctp 仿真环境用于无门槛联调)。

## 当前状态:stub

P0 阶段不实现真实下单,本包仅占位 + 文档。真实 CTP 接入属于 P1。

## 计划实现要点(后续填充)

* 通过 ``thosttraderapi`` (上期 CTP API) + ``ctp`` 封装层建交易通道;
* 行情走 ``MdApi``;交易走 ``TraderApi``;两个独立前台地址;
* 期货特有概念:开仓 / 平仓、保证金、持仓多空、夜盘交易时段;
* ``client_order_id`` 映射 ``OrderRef`` 字段(字符串,系统内自增);
* 回调线程 → ``asyncio.Queue`` 桥接,与 QMT 实现思路一致;
* openctp 仿真可用于完整 CI 集成测试,但部署在容器中。

## 部署

CTP SDK 由券商 / openctp 提供,实盘部署时安装到 ``.venv``。
