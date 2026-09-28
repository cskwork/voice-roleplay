// AT-02 barge-in during AI playback and AT-03 stale output after cancel (PRD §6.3, §18).
// Real stack + real browser audio path; FAKE-MIC-SOURCE fixtures (lib/fakeMic.js).
import { expect, test, type Page } from '@playwright/test'
import { endConversation, logLen, logSince, nextReply, preparePage, say, startConversation, waitFor, waitRealtimeFree, waitUntilPerf } from '../lib/harness'

/**
 * Barge in `afterMs` into the playback of `responseId`; returns speech-start -> local audio stop.
 * `cancelled` is the server's response.cancelled, sent only while the reply is still being generated or has
 * unplayed segments (a fully generated, locally stopped reply has nothing left to cancel).
 */
async function bargeIn(page: Page, from: number, responseId: string, afterMs = 700) {
  const played = await waitFor(page, from, { kind: 'play', type: 'started', key: responseId }, 60_000)
  await waitUntilPerf(page, played.t + afterMs)
  const at = await logLen(page)
  const mic = await say(page, 'e2e_barge')
  // Local stop: the playback worklet reports `flushed` (queue dropped, output silent from the next quantum).
  const flushed = await waitFor(page, at, { kind: 'play', type: 'flushed' }, 10_000)
  const speech = await waitFor(page, at, { kind: 'ws_in', type: 'speech.started' }, 10_000)
  const final = await waitFor(page, at, { kind: 'ws_in', type: 'asr.final', turn_id: speech.turn_id }, 30_000)
  const cancelled = (await logSince(page, at, 'ws_in', 'response.cancelled')).find((e) => e.response_id === responseId) ?? null
  return { mic, at, flushed, cancelled, speech, final, stopMs: flushed.t - mic.firstVoicedPerf }
}
const ms = (x: number | null | undefined) => (x == null ? 'n/a' : `${Math.round(x)} ms`)

test('AT-02 barge-in stops AI audio (opening line and a streamed reply) and the new turn proceeds', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  // Opening line: barge in while it plays (cached audio, all frames already queued in the browser).
  await page.goto('/#/talk/cafe_order/normal:0')
  await page.evaluate(() => window.__e2e.micReady())
  const from0 = await logLen(page)
  await page.getByRole('button', { name: '대화 시작' }).click()
  const opening = await waitFor(page, from0, { kind: 'ws_in', type: 'response.started', opening: true }, 30_000)
  const b1 = await bargeIn(page, from0, opening.response_id as string)
  console.log(`barge-in (opening): speech start -> local audio stop ${ms(b1.stopMs)}, -> response.cancelled ${ms(b1.cancelled && b1.cancelled.t - b1.mic.firstVoicedPerf)}`)

  // The new turn gets its own reply, which plays.
  expect(b1.final.text.toLowerCase(), 'first syllable kept (pre-roll)').toMatch(/^sorry/)
  const r1 = await nextReply(page, b1.at)
  expect(r1.responseId).not.toBe(opening.response_id)
  expect(r1.firstPlayedAt).not.toBeNull()

  // Nothing of the cancelled opening plays after the stop.
  const after = await logSince(page, b1.at, 'play')
  expect(after.filter((e) => e.key === opening.response_id && e.type === 'started' && e.t > b1.flushed.t)).toEqual([])

  // Streamed reply: barge in while the TTS stream is producing audio.
  const from1 = await logLen(page)
  await say(page, 'e2e_cafe_05')
  const started = await waitFor(page, from1, { kind: 'ws_in', type: 'response.started' }, 60_000)
  const b2 = await bargeIn(page, from1, started.response_id as string, 500)
  console.log(`barge-in (streamed reply): speech start -> local audio stop ${ms(b2.stopMs)}, -> response.cancelled ${ms(b2.cancelled && b2.cancelled.t - b2.mic.firstVoicedPerf)}`)
  expect(b2.final.text.toLowerCase()).toMatch(/^sorry/)
  const r2 = await nextReply(page, b2.at)
  expect(r2.firstPlayedAt).not.toBeNull()
  // No audio frame of the cancelled response reaches the browser after response.cancelled (gateway guarantee).
  if (b2.cancelled) {
    const late = (await logSince(page, b2.at, 'ws_audio')).filter((e) => e.response_id === started.response_id && e.t > b2.cancelled!.t)
    expect(late).toEqual([])
  }
  // Nothing of the stopped reply plays again.
  const replayed = (await logSince(page, b2.at, 'play')).filter((e) => e.key === started.response_id && e.type === 'started' && e.t > b2.flushed.t)
  expect(replayed).toEqual([])
  // The UI marks the interrupted reply.
  await expect(page.locator('.bubble.ai.cancelled').first()).toBeVisible()
  await endConversation(page)
})

test('AT-03 stale chunks and text injected after a cancel never play or show', async ({ page }) => {
  await preparePage(page)
  await waitRealtimeFree(page.request)
  // Proxy the realtime socket so the test can re-inject server frames after the cancel.
  const fromServer: { data: string | Buffer; responseId?: string }[] = []
  let toPage: ((m: string | Buffer) => void) | null = null
  await page.routeWebSocket(/\/realtime$/, (ws) => {
    const server = ws.connectToServer()
    toPage = (m) => ws.send(m)
    ws.onMessage((m) => server.send(m))
    server.onMessage((m) => {
      let responseId: string | undefined
      if (typeof m !== 'string') {
        const n = m.readUInt32LE(0)
        responseId = JSON.parse(m.subarray(4, 4 + n).toString('utf8')).response_id
      }
      fromServer.push({ data: m, responseId })
      ws.send(m)
    })
  })
  const opening = await startConversation(page)
  // Speak, then barge into the reply while it plays.
  const from = await logLen(page)
  await say(page, 'e2e_cafe_01')
  const started = await waitFor(page, from, { kind: 'ws_in', type: 'response.started' }, 60_000)
  const rid = started.response_id as string
  const b = await bargeIn(page, from, rid, 150)
  expect(b.cancelled, 'reply still generating when interrupted: server cancels and bumps the epoch').not.toBeNull()
  const stale = fromServer.filter((f) => f.responseId === rid || f.responseId === opening.responseId)
  expect(stale.length, 'captured frames of the cancelled reply').toBeGreaterThan(0)
  const staleEpoch = Number(b.cancelled!.epoch) - 1

  // Inject: the cancelled reply's audio frames again, old-epoch frames under a fresh response id, and stale
  // text. Control: the same audio under a fresh id at the current epoch must play (proves the injection
  // reaches the app; with routeWebSocket the page's socket is Playwright's, so only the worklet and the DOM
  // can observe injected messages).
  const reframe = (buf: Buffer, patch: Record<string, unknown>) => {
    const n = buf.readUInt32LE(0)
    const raw = Buffer.from(JSON.stringify({ ...JSON.parse(buf.subarray(4, 4 + n).toString('utf8')), ...patch }))
    const head = Buffer.alloc(4)
    head.writeUInt32LE(raw.length)
    return Buffer.concat([head, raw, buf.subarray(4 + n)])
  }
  const binary = stale.filter((x) => typeof x.data !== 'string').map((x) => x.data as Buffer)
  const mark = await logLen(page)
  for (const f of binary) toPage!(f)
  for (const f of binary.slice(0, 20)) toPage!(reframe(f, { response_id: 'r_injected_old_epoch', epoch: staleEpoch }))
  for (const [id, ep] of [[rid, staleEpoch], ['r_injected_old_epoch', staleEpoch]] as const) {
    toPage!(JSON.stringify({ type: 'response.text', event_id: 'x', session_id: b.cancelled!.session_id, epoch: ep, event_seq: 1,
      response_id: id, segment_id: 9, text: 'STALE INJECTED CAPTION' }))
  }
  await page.waitForTimeout(1500)
  const plays = (await logSince(page, mark, 'play')).filter((e) => e.type === 'started' && (e.key === rid || e.key === 'r_injected_old_epoch' || e.key === opening.responseId))
  expect(plays, 'no stale audio reached the playback worklet').toEqual([])
  await expect(page.getByText('STALE INJECTED CAPTION')).toHaveCount(0)
  const control = await logLen(page)
  for (const f of binary.slice(0, 5)) toPage!(reframe(f, { response_id: 'r_control_current', epoch: staleEpoch + 1 }))
  await waitFor(page, control, { kind: 'play', type: 'started', key: 'r_control_current' }, 5_000)

  // The conversation still works: the barge-in turn is answered and plays.
  const r = await nextReply(page, b.at)
  expect(r.firstPlayedAt).not.toBeNull()
  await endConversation(page)
})
