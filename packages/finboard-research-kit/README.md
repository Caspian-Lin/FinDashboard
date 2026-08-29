# finboard-research-kit

研究代码沙箱工具包(issue #216)。这是**运行在沙箱容器内部**的唯一 FinBoard
代码:既定义因子执行协议 v1 的数据契约(``FactorContext`` / ``FactorResult``),
也提供把挂载数据装配成上下文、调用 agent 代码、校验并落盘输出的 harness
(``python -m finboard_research_kit.harness``)。

* 版本与镜像 tag 绑定:镜像 ``finboard-research-sandbox:<version>``,version 即
  本包 ``__version__``;run 记录同时归档镜像 digest 与 harness 自报的
  ``kit_version``。
* 只依赖 pandas / numpy / polars / pyarrow(与静态校验 import 白名单一致),
  不含网络 / 文件写入以外的任何 FinBoard 凭证或实盘组件。
