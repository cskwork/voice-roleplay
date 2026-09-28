// CO-5: the session summary's "다시 말하기" drill through the real UI, real models, the app's real recorder.
// FAKE-MIC-SOURCE: learner speech is macOS `say` fixtures fed through a MediaStream (lib/fakeMic.js); the two
// fixtures below are made on demand (lib/harness.ts makeFixture) because tests/fixtures belongs to another task.
import { expect, test } from '@playwright/test'
import { api, endConversation, makeFixture, preparePage, say, speakTurn, startConversation, waitRealtimeFree, waitUntilPerf } from '../lib/harness'

const ERROR_TURN = 'e2e_grammar_01'
const DRILL_TAKE = 'e2e_drill_01'

test.beforeAll(() => {
  // A turn with two clear grammar errors so the session summary has improvements with suggestions.
  makeFixture(ERROR_TURN, 'Yesterday I go to the cafe and I buyed a coffee.')
  makeFixture(DRILL_TAKE, 'Yesterday I went to the cafe and I bought a coffee.')
})

test('CO-5 summary drill: grammar error -> improvement -> 다시 말하기 recorded -> result renders', async ({ page }, testInfo) => {
  const { offHost } = await preparePage(page)
  await waitRealtimeFree(page.request)
  await startConversation(page, 'cafe_order', 'normal:0')
  const turn = await speakTurn(page, ERROR_TURN)
  console.log(`asr.final: ${turn.final.text}`)
  expect(turn.final.text.toLowerCase()).toContain('yesterday')

  await endConversation(page)
  await expect(page.getByRole('heading', { name: /이렇게 말해 봤어요/ })).toBeVisible()
  await expect(page.locator('ol.said li').first()).toContainText(/yesterday/i)

  // At least one improvement with a suggestion and its drill button.
  const cards = page.locator('article.feedback')
  await expect(cards.first()).toBeVisible()
  const card = cards.filter({ has: page.getByRole('button', { name: /다시 말하기/ }) }).first()
  await expect(card).toBeVisible()
  const suggestion = (await card.locator('.suggestion p').innerText()).trim()
  const quote = (await card.locator('.quote q').innerText().catch(() => '')).trim()
  console.log(`improvements: ${await cards.count()}; drill target: ${suggestion}; evidence: ${quote}`)
  expect(suggestion.length).toBeGreaterThan(0)

  const created: { body: Record<string, unknown>; attemptId: string }[] = []
  page.on('response', async (r) => {
    if (r.request().method() === 'POST' && /\/api\/attempts$/.test(new URL(r.url()).pathname)) {
      created.push({ body: JSON.parse(r.request().postData() ?? '{}'), attemptId: (await r.json()).attempt_id })
    }
  })
  await card.getByRole('button', { name: /다시 말하기/ }).click()
  const drill = page.getByRole('region', { name: '다시 말하기 연습' })
  await expect(drill.locator('.drill-target')).toHaveText(suggestion)

  // Record the retry through the app's recorder, preview, submit.
  await drill.getByRole('button', { name: '녹음 시작' }).click()
  await expect(drill.getByRole('button', { name: '녹음 정지' })).toBeVisible()
  const mic = await say(page, DRILL_TAKE, 0.2)
  await waitUntilPerf(page, mic.endPerf + 400)
  await drill.getByRole('button', { name: '녹음 정지' }).click()
  await expect(drill.locator('.take audio')).toHaveCount(1)
  await drill.getByRole('button', { name: '제출하고 분석받기' }).click()

  // The drill result renders inline: first sentence, recognised sentence, word comparison against the target.
  const compare = drill.locator('.compare')
  await expect(compare).toBeVisible({ timeout: 120_000 })
  if (quote) await expect(compare.getByText('처음 말한 문장')).toBeVisible()
  await expect(compare.getByText('이번에 인식된 문장')).toBeVisible()
  const heard = (await compare.locator(':scope > div').filter({ hasText: '이번에 인식된 문장' }).locator('p').innerText()).trim()
  console.log(`drill transcript: ${heard}`)
  expect(heard.toLowerCase()).toMatch(/went/)
  await expect(compare.getByText('다르게 인식된 부분')).toBeVisible()

  // The attempt the drill created is a 'drill' attempt with the suggestion as target, and carries no score.
  expect(created.length, 'one drill attempt').toBe(1)
  expect(created[0]!.body.exercise_type).toBe('drill')
  expect(created[0]!.body.target_text).toBe(suggestion)
  const a = await api(page)
  const result = await (await a.get(`/api/attempts/${created[0]!.attemptId}/result`)).json()
  expect(result.transcript).toBe(heard)
  expect(result.pronunciation_score ?? null).toBeNull()
  expect(JSON.stringify(result)).not.toMatch(/"(pronunciation|phoneme|accent|fluency)_?score"\s*:\s*[0-9]/i)
  const bodyText = await drill.innerText()
  expect(bodyText).not.toMatch(/발음\s*점수\s*[:：]?\s*\d|\d+\s*점\b|native[- ]like|원어민\s*유사도/i)

  await page.screenshot({ path: testInfo.outputPath('co5-summary-drill.png'), fullPage: true })
  await page.screenshot({ path: new URL('../.out/co5-summary-drill.png', import.meta.url).pathname, fullPage: true })
  expect(offHost).toEqual([])
})
