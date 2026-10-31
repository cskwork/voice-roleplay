/**
 * Path prefix the app is served under: '' at the gateway's own origin, '/voice' behind a reverse proxy
 * (Content Forge). It comes from the build's `base` (env VR_WEB_BASE at build time, see vite.config.ts);
 * every API request and the realtime WebSocket go under it.
 */
export const PREFIX = import.meta.env.BASE_URL.replace(/\/+$/, '')

/** '/api/x' -> '/voice/api/x' under a prefix; anything else unchanged. */
export function withPrefix(path: string, prefix: string = PREFIX): string {
  return path.startsWith('/') ? prefix + path : path
}
