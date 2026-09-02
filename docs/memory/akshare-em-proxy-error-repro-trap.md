# 本机 akshare 复现实验的 ProxyError 陷阱(2026-09-02,#257)

主题:本机对 `push2his.eastmoney.com`(EM 行情 host)经系统代理偶发/持续
ProxyError,复现 akshare 接口行为时容易被误导。

## 结论 / 事实

- `uv run python` 调 `ak.stock_zh_a_hist(...)` 稳定报
  `ProxyError HTTPSConnectionPool(host='push2his.eastmoney.com')`,连 600519
  真股票也失败;而 `ak.fund_etf_hist_em(...)` 能正常返回(不同子域/路径,代理
  表现不同)。env 无 `HTTP_PROXY`,但 requests 会读 Windows 系统代理。
- 因此「接口 A 失败、接口 B 成功」不能作为 A 代码有问题的证据;判定接口行为
  以**源码 + 请求 URL 参数**为准(如 `stock_zh_a_hist("510300")` 的 URL 直接
  可见 `secid=0.510300`,沪市被拼成深市前缀,无需等响应)。
- 集成测试(如 `tests/integration/test_etf_bar_chain.py`)一律 mock akshare
  接口,不依赖真实网络。

## Why

#257 复现实验时,`stock_zh_a_hist` 的 ProxyError 差点掩盖「secid 前缀拼错」
这一真实证据;如果是别的接口组合,可能把网络问题误判成代码缺陷,或把代码缺陷
误判成网络抖动。

## How to apply

- 在本机做 akshare/EM 接口行为验证:先看 akshare 源码里 secid/参数拼接,再看
  请求 URL(错误消息里通常带完整 URL),最后才是响应结果;不要用「能不能连通」
  下结论。
- 需要真实取数时重试几次或换网络/关系统代理;写测试时永远 mock。
