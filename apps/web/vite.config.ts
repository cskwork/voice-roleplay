import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const GATEWAY = 'http://127.0.0.1:8710'
// Path the app is served under. '/' (default) at the gateway's own origin; '/voice/' when a reverse proxy
// (Content Forge) serves it at https://<site>/voice/. The app then calls `${base}api/...` (src/lib/base.ts).
const BASE = normalizeBase(process.env.VR_WEB_BASE)

function normalizeBase(value: string | undefined): string {
  const parts = (value ?? '/').split('/').filter(Boolean)
  if (parts.some((p) => !/^[A-Za-z0-9_-]+$/.test(p))) throw new Error(`invalid VR_WEB_BASE: ${value}`)
  return parts.length ? `/${parts.join('/')}/` : '/'
}

// Dev server proxies the gateway. The gateway only accepts its own origin, so the
// proxy rewrites Origin; `/__vr_init` lets the dev page obtain the vr_sid cookie from `GET /`.
// Under a base path the proxy strips it, so the dev gateway needs no prefix configured.
export default defineConfig({
  base: BASE,
  plugins: [react()],
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      [`${BASE}api`]: {
        target: GATEWAY,
        ws: true,
        rewrite: (path) => path.slice(BASE.length - 1),
        configure: (proxy) => {
          proxy.on('proxyReq', (req) => req.setHeader('origin', GATEWAY))
          proxy.on('proxyReqWs', (req) => req.setHeader('origin', GATEWAY))
        },
      },
      [`${BASE}__vr_init`]: { target: GATEWAY, rewrite: () => '/' },
    },
  },
  build: { outDir: 'dist', sourcemap: false, target: 'es2022' },
  worker: { format: 'es' },
  test: { environment: 'node', include: ['tests/**/*.test.ts'] },
})
