import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    // 注意：不设 host 时 Vite 在 Windows 只绑 IPv6 的 ::1（实测浏览器访问
    // localhost:5173 正常，但 curl/其它工具用 127.0.0.1:5173 会 ECONNREFUSED）。
    // 需要 IPv4 也能访问就改成 host: true（会同时暴露到局域网）。
    proxy: {
      // 开发时走同源代理：cookie 的 SameSite=Strict 在跨端口下会被浏览器拦掉，
      // 所以不用 CORS，直接代理到后端。
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: false,
        configure(proxy) {
          // SSE 必须不压缩不缓冲，否则流式会变成最后一次性吐出
          proxy.on('proxyReq', (req) => {
            req.setHeader('accept-encoding', 'identity')
          })
        },
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    chunkSizeWarningLimit: 900,
  },
})
