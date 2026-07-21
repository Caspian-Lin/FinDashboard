# finboard-broker

券商接口的**统一抽象**与 **Mock 实现**。

* `base.py` 定义 ``BrokerAdapter`` —— 所有实现(QMT / CTP / Mock)必须遵守的接口契约。
* `events.py` 定义回报事件类型,OrderManager 通过 ``events()`` 异步流订阅。
* `mock.py` 是本地开发与 CI 用的内存 broker,完整模拟"下单→成交→撤单→回报"链路,
  不依赖任何外部 SDK。

真实的 QMT/CTP 实现分别在 ``finboard-broker-qmt`` / ``finboard-broker-ctp`` 包中,
按需动态导入,缺 SDK 时不会影响主进程启动。
