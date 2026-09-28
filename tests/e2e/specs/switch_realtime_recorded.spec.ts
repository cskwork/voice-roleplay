// Starting something new ends the running realtime conversation instead of refusing with LOCAL_BUSY
// (PRD §7 v0.2.1, PROTOCOL §6.3/§7). Real stack; FAKE-MIC-SOURCE fixtures through the app's recorder.
import { expect, test, type Page } from '@playwright/test'
import { api, logSince, preparePage, recordTake, scenarioTitle, speakTurn, startConversation, waitRealtimeFree } from '../lib/harness'

const CALM = '새 연습을 시작해 이전 회화를 종료했어요.'

/** Starts a cafe conversation and returns its session id. */
async function startCafe(page: Page, viaHome = false): Promise<string> {
  const created = page.waitForResponse((r) => r.request().method() === 'POST' && new URL(r.url()).pathname === '/api/sessions')
  await startConversation(page, 'cafe_order', 'normal:0', viaHome)
  return (await (await created).json()).session_id as string
}

async function submitReading(page: Page): Promise<void> {
  await expect(page.locator('.prompt-en')).toContainText('cappuccino')
  await recordTake(page, 'e2e_read_01')
  await page.getByRole('button', { name: '제출하고 분석받기' }).click()
  await page.waitForURL(/#\/result\//, { timeout: 120_000 })
  await expect(page.locator('p.transcript')).toContainText(/cappuccino/i)
}

test('user flow: one realtime turn, then 녹음형 연습 without 종료 → submit gives a result', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  const refused: string[] = []
  page.on('response', (r) => {
    if (r.status() === 409) refused.push(`${r.request().method()} ${new URL(r.url()).pathname}`)
  })
  const sessionId = await startCafe(page, true)
  await speakTurn(page, 'e2e_cafe_01')

  // Leave the conversation the way a learner would: brand link → 학습 홈 → 녹음형 연습 → the same scenario.
  await page.getByRole('link', { name: '말하기 연습 홈' }).click()
  await page.getByRole('radio', { name: /녹음형 연습/ }).check()
  await page.locator('li.scenario', { hasText: scenarioTitle('cafe_order') }).getByRole('button', { name: /이 상황으로 연습/ }).click()
  await page.waitForURL(/#\/practice\/cafe_order/)
  await page.evaluate(() => window.__e2e.micReady())
  await submitReading(page)

  expect(refused, 'no request was refused').toEqual([])
  await expect(page.getByText(/실시간 회화가 진행 중/)).toHaveCount(0)
  const a = await api(page)
  const session = await (await a.get(`/api/sessions/${sessionId}`)).json()
  expect(session.state).toBe('ended')
  // The conversation's summary is still there (in memory for 15 min).
  const ended = await (await a.post(`/api/sessions/${sessionId}/end`)).json()
  expect(ended.state).toBe('ended')
  expect(ended.summary.turns.some((t: { role: string; text: string }) => t.role === 'user' && /latte/i.test(t.text))).toBeTruthy()
})

test('a conversation still open in another tab ends calmly when recorded practice is submitted', async ({ context }) => {
  const talk = await context.newPage()
  await preparePage(talk)
  await waitRealtimeFree(talk.request)
  const sessionId = await startCafe(talk)

  const practice = await context.newPage()
  await preparePage(practice)
  await practice.goto('/#/practice/cafe_order/normal:0/reading')
  await practice.evaluate(() => window.__e2e.micReady())
  await submitReading(practice)

  // The old tab: closed by the gateway with 4001, a calm notice, no connection error.
  await expect(talk.getByText(CALM)).toBeVisible({ timeout: 15_000 })
  const closes = await logSince(talk, 0, 'ws_close')
  expect(closes.map((c) => c.code)).toEqual([4001])
  await expect(talk.getByText('서버와의 연결이 끊겼어요')).toHaveCount(0)
  await expect(talk.locator('.notice-danger')).toHaveCount(0)
  await expect(talk.getByText('종료됨').first()).toBeVisible()
  // Its summary can still be opened from there.
  await talk.getByRole('button', { name: '요약 보기', exact: true }).click()
  await talk.waitForURL(/#\/summary/, { timeout: 60_000 })
  const a = await api(practice)
  expect((await (await a.get(`/api/sessions/${sessionId}`)).json()).state).toBe('ended')
})

test('closing the tab ends the conversation (keepalive end request with the CSRF header)', async ({ context }) => {
  const talk = await context.newPage()
  await preparePage(talk)
  await waitRealtimeFree(talk.request)
  const sessionId = await startCafe(talk)
  const a = await api(talk)
  // A plain socket close would only start the 120 s reconnect grace; `ended` within seconds means the page's
  // own end request got through the gateway's cookie + Origin + CSRF checks.
  await talk.close({ runBeforeUnload: true })
  await expect
    .poll(async () => (await (await a.get(`/api/sessions/${sessionId}`)).json()).state, { timeout: 10_000 })
    .toBe('ended')
  const health = await (await a.get('/api/health')).json()
  expect(health.modes.realtime.session_active).toBe(false)
})
