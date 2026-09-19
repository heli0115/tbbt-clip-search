import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 开发期把 /api 与 /v 代理到 FastAPI，保持同源。
    // 为什么强调同源：Media Fragment 播放依赖 Range(206)，且浏览器对跨源媒体
    // 更严格。代理后 dev 与生产形态一致（AGENTS.md 第 4 节）。
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
      '/v': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
