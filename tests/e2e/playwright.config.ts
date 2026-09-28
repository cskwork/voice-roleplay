import { defineConfig } from '@playwright/test'

// Real end-to-end tests: a running stack (`./app start`) with real models, a real Chromium, the app's real
// AudioWorklet capture/playback path. Nothing here is mocked except the microphone *source*
// (fixture WAVs fed through a MediaStream, see lib/fakeMic.js) — labelled FAKE-MIC-SOURCE in the specs.
export const BASE_URL = process.env.VR_E2E_URL ?? 'http://127.0.0.1:8710'

export const CHROMIUM_ARGS = [
  '--autoplay-policy=no-user-gesture-required',
  '--mute-audio', // rendering and worklets run as usual; nothing comes out of the speakers
  '--use-fake-ui-for-media-stream',
]

export default defineConfig({
  testDir: './specs',
  // One stack, one realtime session at a time (the gateway allows only one): run serially.
  workers: 1,
  fullyParallel: false,
  timeout: 240_000,
  expect: { timeout: 30_000 },
  reporter: [['list'], ['json', { outputFile: 'test-results/results.json' }]],
  use: {
    baseURL: BASE_URL,
    browserName: 'chromium',
    headless: true,
    launchOptions: { args: CHROMIUM_ARGS },
    viewport: { width: 1280, height: 860 },
    locale: 'ko-KR',
    trace: 'retain-on-failure',
  },
})
