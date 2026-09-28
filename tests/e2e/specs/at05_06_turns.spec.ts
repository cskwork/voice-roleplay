// AT-05 hesitation / manual commit and AT-06 silence (PRD §6.2, §18). Real stack; FAKE-MIC-SOURCE fixtures.
import { expect, test } from '@playwright/test'
import { countLog, endConversation, logLen, logSince, nextReply, preparePage, say, startConversation, waitFor, waitRealtimeFree, waitUntilPerf } from '../lib/harness'

test('AT-05 a 0.6 s pause mid-utterance does not end the turn (normal = 900 ms); 말하기 완료 ends it at once', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  await startConversation(page, 'cafe_order', 'normal:0')

  // "I would like a large latte [600 ms pause] with oat milk, please."
  const from = await logLen(page)
  const mic = await say(page, 'e2e_hesitation')
  const final = await waitFor(page, from, { kind: 'ws_in', type: 'asr.final' }, 30_000)
  const reply = await nextReply(page, from)
  const log = await logSince(page, from, 'ws_in')
  const starts = log.filter((e) => e.type === 'speech.started' && e.t < reply.startedAt)
  const ends = log.filter((e) => e.type === 'speech.ended' && e.t < reply.startedAt)
  expect(starts, 'one utterance').toHaveLength(1)
  expect(ends).toHaveLength(1)
  expect(ends[0]!.reason).toBe('silence')
  expect(ends[0]!.t, 'turn ended only after the whole sentence').toBeGreaterThan(mic.lastVoicedPerf)
  const text = String(final.text).toLowerCase()
  expect(text).toContain('latte')
  expect(text).toContain('oat milk')

  // Manual end: press 말하기 완료 150 ms after the last voiced sample, well before the 900 ms silence rule.
  const from2 = await logLen(page)
  const mic2 = await say(page, 'e2e_cafe_02')
  await waitFor(page, from2, { kind: 'ws_in', type: 'speech.started' }, 10_000)
  await waitUntilPerf(page, mic2.lastVoicedPerf + 150)
  await page.getByRole('button', { name: '말하기 완료' }).click()
  const ended = await waitFor(page, from2, { kind: 'ws_in', type: 'speech.ended' }, 10_000)
  expect(ended.reason).toBe('commit')
  expect(ended.t - mic2.lastVoicedPerf, 'ended before the silence timeout would have').toBeLessThan(900)
  const final2 = await waitFor(page, from2, { kind: 'ws_in', type: 'asr.final', turn_id: ended.turn_id }, 30_000)
  expect(String(final2.text).toLowerCase()).toContain('oat milk')
  console.log(`manual commit: last voiced -> speech.ended ${Math.round(ended.t - mic2.lastVoicedPerf)} ms, -> asr.final ${Math.round(final2.t - mic2.lastVoicedPerf)} ms`)
  const r2 = await nextReply(page, from2)
  expect(r2.firstPlayedAt).not.toBeNull()
  // A second 말하기 완료 has nothing to finish: no extra turn, no extra reply.
  const from3 = await logLen(page)
  await page.keyboard.press('Space')
  await page.waitForTimeout(1500)
  expect((await logSince(page, from3, 'ws_in', 'response.started')).length).toBe(0)
  await endConversation(page)
})

test('AT-06 30 s of room-level noise: no speech turn, no LLM call, no feedback', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  await startConversation(page, 'cafe_order', 'normal:0')
  await page.waitForTimeout(1000) // session-start prefix warm-up (one LLM request) has finished by now
  const llmBefore = countLog('llm', /launch_slot_/)
  const gwBefore = countLog('gateway', /turn_ended|turn_discarded|response_done|goals_evaluated|summary_updated/)
  const from = await logLen(page)
  await page.evaluate(() => window.__e2e.setNoise(-60)) // quiet room, ~ -60 dBFS RMS
  await page.waitForTimeout(30_000)
  await page.evaluate(() => window.__e2e.setNoise(null))
  const events = await logSince(page, from, 'ws_in')
  expect(events.filter((e) => ['speech.started', 'asr.final', 'response.started', 'feedback.ready'].includes(String(e.type)))).toEqual([])
  expect(countLog('llm', /launch_slot_/) - llmBefore, 'LLM requests during 30 s of silence').toBe(0)
  expect(countLog('gateway', /turn_ended|turn_discarded|response_done|goals_evaluated|summary_updated/) - gwBefore).toBe(0)
  await expect(page.locator('.bubble.user')).toHaveCount(0)
  // Frames kept flowing the whole time (the mic path was live, not muted).
  const sent = await page.evaluate(() => window.__e2e.sentFrames ?? 0)
  expect(sent).toBeGreaterThan(1400)
  await endConversation(page)
})
