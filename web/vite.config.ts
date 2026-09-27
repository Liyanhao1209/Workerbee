import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

/**
 * 内核默认监听 127.0.0.1:8787（见后端 auth.py / main.py 的默认值）。
 * 开发期由 dev server 代理过去，浏览器只面对同源地址——这样 X-Workerbee-Token
 * 头与 WebSocket 的 ?token= 都不必处理跨域。
 */
const KERNEL = process.env.WORKERBEE_KERNEL ?? 'http://127.0.0.1:8787';

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': {
        target: KERNEL,
        changeOrigin: false,
        // 内核的 401 与 409/422 都是契约的一部分，不能被代理改写
        ws: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    sourcemap: false,
    chunkSizeWarningLimit: 1200,
  },
});
