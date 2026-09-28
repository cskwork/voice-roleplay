import { defineConfig } from '@playwright/test'
import base from './playwright.config'

// Regenerates docs/screenshots/*.png for the README (real stack, same browser setup as the E2E tests).
export default defineConfig({ ...base, testDir: './docs', reporter: [['line']], use: { ...base.use, trace: 'off' } })
