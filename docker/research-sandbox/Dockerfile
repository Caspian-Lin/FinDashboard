# 研究代码沙箱镜像(issue #216)。
#
# 与开发/生产统一形态(单形态 Docker,用户决策 2026-08-28)。镜像内容 =
# 因子执行协议 v1 的全部运行时:pinned CPython + pandas / numpy / polars /
# pyarrow + finboard-research-kit(harness 入口)。容器外无任何 FinBoard
# 凭证 / DB / broker 组件;运行参数由服务端 ResearchSandboxRunner 注入:
#   --network none --read-only --cap-drop ALL --user 65532
#   --pids-limit/--cpus/--memory(+memory-skip 同值)/ 墙钟超时 docker kill
#
# 版本绑定:tag = finboard-research-kit 的 __version__(settings
# research_sandbox_image 默认 finboard-research-sandbox:0.3.0);run 记录
# 另存镜像 digest。改 kit 版本时同步 bump tag 与下方 KIT_VERSION。
#
# 构建(仓库根):docker build -f docker/research-sandbox/Dockerfile \
#   -t finboard-research-sandbox:0.3.0 .
# CI 在 lint-typecheck-test 之外单独构建本镜像(见 .github/workflows/ci.yml)。

FROM python:3.12-slim

# 数值栈版本与 uv.lock 解析结果对齐(可复现性优先;升级走 PR)。
ARG PANDAS_VERSION=3.0.5
ARG NUMPY_VERSION=2.5.1
ARG PYARROW_VERSION=25.0.0
ARG POLARS_VERSION=1.44.1
ARG KIT_VERSION=0.3.0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir \
    "pandas==${PANDAS_VERSION}" \
    "numpy==${NUMPY_VERSION}" \
    "pyarrow==${PYARROW_VERSION}" \
    "polars==${POLARS_VERSION}"

# 只装 kit 本体(--no-deps:数值栈已 pinned 安装,避免 pip 重解析漂移)。
COPY packages/finboard-research-kit /tmp/kit
RUN pip install --no-cache-dir --no-deps /tmp/kit \
    && rm -rf /tmp/kit \
    && python -c "import finboard_research_kit as k; assert k.__version__ == '${KIT_VERSION}', k.__version__"

# 非 root 运行用户(runner 以 --user 65532 显式指定,这里保证 uid 存在)。
RUN useradd --uid 65532 --no-create-home --shell /usr/sbin/nologin sandbox
USER 65532
WORKDIR /sandbox

# harness 由 runner 以参数化 command 启动(python -m finboard_research_kit.harness)。
ENTRYPOINT []
