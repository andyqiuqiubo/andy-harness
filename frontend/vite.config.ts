/// <reference types="vitest/config" />
import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// 后端地址/端口可由环境变量覆盖：默认与一键启动脚本一致（8000/5173），
// 便于在 8000 已被占用时用独立端口跑演示或隔离环境（如 VITE_BACKEND_TARGET）。
// 用 127.0.0.1 而非 localhost：后端 uvicorn 以 --host 127.0.0.1 绑定（纯 IPv4），
// 而 Node 18+ 解析 localhost 会优先尝试 IPv6 ::1 —— 代理每次请求先对 ::1 吃一个
// ECONNREFUSED（start-all.bat 场景下表现为 vited 控制台反复 `http proxy error: /api/health`），
// 浏览器虽经 Happy Eyeballs 双栈回退能连上、看起来"正常"，实际每个请求都被拖延。钉死 IPv4 消除整类误配。
const backendTarget = process.env.VITE_BACKEND_TARGET || 'http://127.0.0.1:8000'
const devPort = Number(process.env.VITE_PORT || 5173)

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    port: devPort,
    proxy: {
      '/api': {
        target: backendTarget,
        changeOrigin: true,
      },
      '/ws': {
        target: backendTarget.replace(/^http/, 'ws'),
        ws: true,
      },
    },
  },
  test: {
    // E8：前端测试。jsdom 提供 DOM，globals 省去每个文件 import。
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.{test,spec}.ts'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'html'],
      include: ['src/utils/**', 'src/stores/**', 'src/composables/**'],
    },
  },
})
