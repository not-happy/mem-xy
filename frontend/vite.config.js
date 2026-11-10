import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// ★ dev 用代理把 /v1 转到后端，省掉 CORS：生产环境前后端同源，也不需要后端加中间件。
export default defineConfig({
  plugins: [react()],
  server: {
    // ★ 5273 专属端口：5173 是 Vite 默认端口，已被 dingding 项目占用。
    //   strictPort 防止被顶到 5174+ 导致 service.ps1 按端口找 PID 时误杀别家进程。
    port: 5273,
    strictPort: true,
    proxy: {
      '/v1': { target: 'http://127.0.0.1:8787', changeOrigin: true },
      '/healthz': { target: 'http://127.0.0.1:8787', changeOrigin: true },
    },
  },
})
