// Mobile (390x844) README screenshots from a real run (real models; FAKE-MIC-SOURCE learner audio, lib/fakeMic.js).
// Same flow as screenshots.spec.ts; writes docs/screenshots/mobile-{home,realtime,summary,result}.png.
import { join } from 'node:path'
import { expect, test, type Page } from '@playwright/test'
import { endConversation, preparePage, ROOT, say, speakTurn, startConversation, waitRealtimeFree, waitUntilPerf } from '../lib/harness'

test.use({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2, hasTouch: true })

const shot = (page: Page, name: string) => page.screenshot({ path: join(ROOT, 'docs/screenshots', `mobile-${name}.png`), fullPage: false })

test('README mobile screenshots', async ({ page }) => {
  test.setTimeout(10 * 60_000)
  await preparePage(page)
  await waitRealtimeFree(page.request)
  await page.goto('/#/home')
  await expect(page.locator('li.scenario').first()).toBeVisible()
  await expect(page.getByText('모델 준비됨')).toBeVisible({ timeout: 60_000 })
  await shot(page, 'home')

  await startConversation(page, 'cafe_order', 'normal:0')
  await speakTurn(page, 'e2e_cafe_01')
  await speakTurn(page, 'e2e_cafe_02')
  await page.getByRole('button', { name: /힌트 1\/3/ }).click()
  await page.waitForTimeout(1500)
  await shot(page, 'realtime')
  await endConversation(page)
  await expect(page.getByRole('heading', { level: 1 })).toBeVisible()
  await page.waitForTimeout(500)
  await shot(page, 'summary')

  await waitRealtimeFree(page.request)
  await page.goto('/#/practice/cafe_order/normal:0/reading')
  await page.evaluate(() => window.__e2e.micReady())
  await page.getByRole('button', { name: '녹음 시작' }).click()
  const mic = await say(page, 'e2e_read_01', 0.2)
  await waitUntilPerf(page, mic.endPerf + 400)
  await page.getByRole('button', { name: '녹음 정지' }).click()
  await page.getByRole('button', { name: '제출하고 분석받기' }).click()
  await page.waitForURL(/#\/result\//, { timeout: 120_000 })
  await expect(page.locator('p.transcript')).toBeVisible()
  await shot(page, 'result')
})
