import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

/**
 * 内核默认监听 127.0.0.1:8765。
 * 开发期由 dev server 代理过去，浏览器只面对同源地址——这样 X-Workerbee-Token
 * 头与 WebSocket 的 ?token= 都不必处理跨域。
 */
const KERNEL = process.env.WORKERBEE_KERNEL ?? 'http://127.0.0.1:8765';

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
  test: {
    // jsdom 而不是 node：组件测试与 dompurify 消毒都需要真实 DOM。
    environment: 'jsdom',
    // 不污染全局命名空间：测试里显式 import { describe, it, expect } from 'vitest'。
    globals: false,
    setupFiles: './src/test/setup.ts',
    // 就近放置的 *.test.ts(x)；dist 与 node_modules 默认已排除，这里只是显式声明。
    include: ['src/**/*.test.{ts,tsx}'],
  },
});
