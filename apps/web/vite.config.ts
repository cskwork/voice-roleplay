import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const GATEWAY = 'http://127.0.0.1:8710'

// Dev server proxies the gateway. The gateway only accepts its own origin, so the
// proxy rewrites Origin; `/__vr_init` lets the dev page obtain the vr_sid cookie from `GET /`.
export default defineConfig({
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: GATEWAY,
        ws: true,
        configure: (proxy) => {
          proxy.on('proxyReq', (req) => req.setHeader('origin', GATEWAY))
          proxy.on('proxyReqWs', (req) => req.setHeader('origin', GATEWAY))
        },
      },
      '/__vr_init': { target: GATEWAY, rewrite: () => '/' },
    },
  },
  build: { outDir: 'dist', sourcemap: false, target: 'es2022' },
  worker: { format: 'es' },
  test: { environment: 'node', include: ['tests/**/*.test.ts'] },
})
