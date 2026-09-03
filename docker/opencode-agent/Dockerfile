# OpenCode 研究容器 JSON 工具衍生镜像(issue #313)。
#
# 基于官方 ghcr.io/anomalyco/opencode(实测 base:Alpine Linux 3.24 + apk,
# musl libc),只加装 jq 与 python3 —— 研究容器内默认无 python/jq/node,agent
# 排查 JSON 只能用 rg 硬抠;本镜像补上这两个工具,其余一切与官方镜像一致:
# ENTRYPOINT(["opencode"])、USER(root)、ENV、挂载约定、env 白名单与
# #242/#248 模型同步逻辑全部透明继承。settings `opencode_image` 指向本镜像
# 即启用,指回官方镜像名即回滚(零代码改动)。
#
# 版本锁定:BASE_IMAGE 默认 pin 官方版本 tag(1.18.15,发布时与 `latest`
# 所指一致;tag 存在性已核实,docker buildx imagetools inspect 可取 index
# digest 做进一步锁定,见 docs/opencode-agent-image.md)。官方镜像升级 →
# bump BASE_IMAGE → make opencode-agent-image 重建 → 重启容器。apk 包版本随
# Alpine v3.24 分支仓库解析(分支内 point 版本滚动、旧版会被裁剪,精确 pin
# `-rN` 会让后续重建失败,故不 pin);整体可复现性由 base 镜像 pin 承载。
#
# 构建(构建上下文 = docker/opencode-agent/ 自身,不用仓库根 —— 本镜像无
# COPY,小上下文避免把仓库拖进构建):
#   make opencode-agent-image
#   # 或等价:
#   docker build -f docker/opencode-agent/Dockerfile \
#     -t finboard-opencode-agent:1.18.15 docker/opencode-agent/
# CI 在 sandbox-image job 之外单独构建 + smoke(见 .github/workflows/ci.yml)。

ARG BASE_IMAGE=ghcr.io/anomalyco/opencode:1.18.15
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="finboard-opencode-agent" \
      org.opencontainers.image.description="ghcr.io/anomalyco/opencode + jq/python3 (FinDashboard issue #313)"

# 只加装 jq + python3(Alpine 包名;Debian 系的 python3-minimal 在 Alpine
# v3.24 仓库不存在,已实测)。不设 ENTRYPOINT/CMD/USER/ENV —— 容器命令
# `docker run <image> web --hostname 0.0.0.0 ...`(process_manager
# build_docker_run_command)依赖官方镜像的 ENTRYPOINT ["opencode"],必须原样继承。
RUN apk add --no-cache jq python3
