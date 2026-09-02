import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import path from "path";

// 多 worktree 并行开发(#289):dev server 端口与 API 代理目标可用环境变量覆盖
// (make dev 透传 FINBOARD_WEB_PORT / FINBOARD_WEB_API_PORT);未设置时行为与
// 历史完全一致(5173 / 8000)。显式指定端口被占用即报错——vite 默认静默换端口,
// 但代理目标不变,会让前端代理到别的 worktree 的后端。
const webPort = process.env.FINBOARD_WEB_PORT;
const apiPort = process.env.FINBOARD_WEB_API_PORT ?? "8000";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    maxWorkers: 4,
  },
  server: {
    port: Number(webPort ?? 5173),
    strictPort: Boolean(webPort),
    proxy: {
      "/api": `http://localhost:${apiPort}`,
      "/ws": {
        target: `ws://localhost:${apiPort}`,
        ws: true,
      },
    },
  },
});
