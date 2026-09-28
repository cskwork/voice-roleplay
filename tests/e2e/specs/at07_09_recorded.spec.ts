// AT-07 preview/re-record/submit, AT-08 double submit + commit resend, AT-09 transcript edit, AT-18 no
// pronunciation score (PRD §7, §8, §18). Real stack; FAKE-MIC-SOURCE fixtures through the app's recorder.
import { expect, test } from '@playwright/test'
import { api, endConversation, logLen, logSince, nextReply, preparePage, recordTake, say, startConversation, waitFor, waitRealtimeFree, waitUntilPerf } from '../lib/harness'

test('AT-07/08/09/18 reading: preview, re-record, double submit, transcript edit, no pronunciation score', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  const submits: { status: number; body: { job_id: string }; key: string | null }[] = []
  page.on('response', async (r) => {
    if (r.request().method() === 'POST' && /\/api\/attempts\/[^/]+\/submit$/.test(r.url())) {
      submits.push({ status: r.status(), body: await r.json(), key: r.request().headers()['idempotency-key'] ?? null })
    }
  })
  await page.goto('/#/practice/cafe_order/normal:0/reading')
  await page.evaluate(() => window.__e2e.micReady())
  await expect(page.locator('.prompt-en')).toContainText('cappuccino')

  // Take 1 says a different sentence; take 2 (re-record) is the target sentence. Only take 2 may be analysed.
  await recordTake(page, 'e2e_read_02')
  const take1Url = await page.locator('.take audio').getAttribute('src')
  const dur1 = await page.locator('.take audio').evaluate((a: HTMLAudioElement) => new Promise<number>((res) => (a.readyState >= 1 ? res(a.duration) : (a.onloadedmetadata = () => res(a.duration)))))
  expect(dur1, 'preview holds the recorded take').toBeGreaterThan(1.5)
  await recordTake(page, 'e2e_read_01')
  const take2Url = await page.locator('.take audio').getAttribute('src')
  expect(take2Url).not.toBe(take1Url)
  // The first take's blob URL was revoked when re-recording.
  expect(await page.evaluate(async (u) => fetch(u!).then(() => 'alive', () => 'revoked'), take1Url)).toBe('revoked')

  // Double click on submit: the second click must not create a second job.
  const submit = page.getByRole('button', { name: '제출하고 분석받기' })
  await submit.dblclick()
  await page.waitForURL(/#\/result\//, { timeout: 120_000 })
  const jobIds = new Set(submits.map((s) => s.body.job_id))
  expect(jobIds.size, 'one job for the take').toBe(1)
  const attemptId = decodeURIComponent(page.url().split('/').pop()!)

  // Result: transcript of take 2 only.
  const transcript = page.locator('p.transcript')
  await expect(transcript).toContainText(/cappuccino/i)
  await expect(transcript).not.toContainText(/sweet/i)

  // AT-08 via the API: same Idempotency-Key -> 200 + same job; another key for the same (already submitted) take -> rejected.
  const a = await api(page)
  const again = await a.post(`/api/attempts/${attemptId}/submit`, {}, { 'Idempotency-Key': submits[0]!.key! })
  expect(again.status()).toBe(200)
  expect((await again.json()).job_id).toBe(submits[0]!.body.job_id)
  const other = await a.post(`/api/attempts/${attemptId}/submit`, {}, { 'Idempotency-Key': 'another-key' })
  expect(other.status()).toBe(409)

  // AT-18: the API result and the page carry no pronunciation score.
  const result = await (await a.get(`/api/attempts/${attemptId}/result`)).json()
  expect(result.pronunciation_score ?? null).toBeNull()
  expect(JSON.stringify(result)).not.toMatch(/"(pronunciation|phoneme|accent|fluency)_?score"\s*:\s*[0-9]/i)
  await expect(page.getByText('발음 평가는 제공되지 않습니다')).toBeVisible()
  const bodyText = await page.locator('main').innerText()
  expect(bodyText).not.toMatch(/발음\s*점수\s*[:：]?\s*\d|\d+\s*점\b|native[- ]like|원어민\s*유사도/i)

  // AT-09: edit the transcript -> revision 2, original kept, feedback tied to the new revision.
  const original = (await transcript.innerText()).trim()
  await page.getByRole('button', { name: /고치기/ }).click()
  const edited = "I'd like a large cappuccino with almond milk, please."
  await page.locator('textarea').fill(edited)
  await page.getByRole('button', { name: '수정본으로 다시 분석' }).click()
  await expect(page.getByText(/내가 고친 문장 \(수정본 2\)/)).toBeVisible({ timeout: 120_000 })
  const r2 = await (await a.get(`/api/attempts/${attemptId}/result`)).json()
  expect(r2.transcript_revision).toBe(2)
  expect(r2.revisions.map((v: { revision: number; source: string }) => [v.revision, v.source])).toEqual([[1, 'asr'], [2, 'user']])
  expect(r2.revisions[0].text).toBe(original)
  expect(r2.revisions[1].text).toBe(edited)
  for (const f of r2.feedback) expect(f.transcript_revision).toBe(2)
  // Metrics stay from revision 1 (the audio), not recomputed from edited text.
  expect(r2.metrics?.metrics_version ?? 'm1').toBe('m1')
})

test('AT-08 realtime: a resent input.commit gives the same asr.final and no duplicate reply', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  // Resend every input.commit the page sends: once right away (merged into the pending commit) and once after
  // the final transcript (must repeat the same asr.final, never start a second reply).
  await page.routeWebSocket(/\/realtime$/, (ws) => {
    const server = ws.connectToServer()
    ws.onMessage((m) => {
      server.send(m)
      if (typeof m === 'string' && m.includes('"input.commit"')) {
        setTimeout(() => server.send(m), 50)
        setTimeout(() => server.send(m), 2500)
      }
    })
  })
  await startConversation(page)
  const from = await logLen(page)
  const mic = await say(page, 'e2e_cafe_03')
  await waitFor(page, from, { kind: 'ws_in', type: 'speech.started' }, 10_000)
  await waitUntilPerf(page, mic.lastVoicedPerf + 150)
  await page.getByRole('button', { name: '말하기 완료' }).click()
  const reply = await nextReply(page, from)
  await page.waitForTimeout(3000)
  const log = await logSince(page, from, 'ws_in')
  const finals = log.filter((e) => e.type === 'asr.final')
  expect(finals.length, 'asr.final repeated for the resent commit').toBe(2)
  expect(finals[1]!.text).toBe(finals[0]!.text)
  expect(finals[1]!.turn_id).toBe(finals[0]!.turn_id)
  expect(log.filter((e) => e.type === 'response.started').map((e) => e.response_id)).toEqual([reply.responseId])
  await expect(page.locator('.bubble.user')).toHaveCount(1)
  await endConversation(page)
})
