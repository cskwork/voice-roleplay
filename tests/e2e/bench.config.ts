import { defineConfig } from '@playwright/test'
import base from './playwright.config'

// Full-stack benchmark (benchmarks/run.sh): same browser setup as the E2E tests, one long test.
export default defineConfig({
  ...base,
  testDir: './bench',
  timeout: 4 * 60 * 60 * 1000,
  reporter: [['line']],
  use: { ...base.use, trace: 'off' },
})
