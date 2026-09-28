// AT-01 cafe realtime conversation (PRD §18) through the real UI, real models, the app's real audio worklets.
// FAKE-MIC-SOURCE: learner speech is macOS `say` fixtures fed through a MediaStream (lib/fakeMic.js).
import { expect, test } from '@playwright/test'
import { endConversation, logSince, preparePage, speakTurn, startConversation, waitRealtimeFree } from '../lib/harness'

test('AT-01 cafe: 3+ turns with partial/final captions, short replies, goal updates', async ({ page }) => {
  const { offHost } = await preparePage(page)
  await waitRealtimeFree(page.request)
  const opening = await startConversation(page, 'cafe_order', 'normal:0', true)
  expect(opening.firstPlayedAt, 'opening line audio played').not.toBeNull()
  await expect(page.locator('.bubble.ai').first()).toContainText('Maple Street Coffee')

  const turns = []
  for (const fx of ['e2e_cafe_01', 'e2e_cafe_02', 'e2e_cafe_03']) {
    const t = await speakTurn(page, fx)
    turns.push(t)
    console.log(`${fx}: lastVoiced->firstPlayed ${Math.round(t.reply.firstPlayedAt! - t.mic.lastVoicedPerf)} ms, ` +
      `lastVoiced->final ${Math.round(t.final.t - t.mic.lastVoicedPerf)} ms, final->response.started ${Math.round(t.reply.startedAt - t.final.t)} ms, ` +
      `started->firstFrame ${Math.round(t.reply.firstAudioFrameAt! - t.reply.startedAt)} ms, partials ${t.partials.length}, segments ${t.reply.segments.length}, gaps ${t.reply.playGaps.map(Math.round)}`)
    // Final transcript is shown as a final user bubble; the reply is short (PRD: short AI answers).
    await expect(page.locator('.bubble.user.final').last()).toContainText(t.final.text.split(' ').slice(0, 2).join(' '))
    const words = t.reply.segments.join(' ').split(/\s+/).length
    expect(words, `reply length: ${t.reply.segments.join(' ')}`).toBeLessThanOrEqual(60)
    expect(t.reply.firstPlayedAt, 'AI audio actually played').not.toBeNull()
  }
  // Partial captions: at least one turn produced an asr.partial before its final (turns here are 1.4-2.9 s,
  // partials are re-decoded every 700 ms, so the shortest turn may legitimately have none).
  expect(turns.some((t) => t.partials.length > 0 && t.partials[0]!.t < t.final.t)).toBeTruthy()
  expect(turns[0]!.final.text.toLowerCase()).toContain('latte')
  expect(turns[1]!.final.text.toLowerCase()).toContain('oat milk')

  // Goals: goal.update events arrive and at least one goal is done after ordering + milk change + price.
  await expect
    .poll(async () => (await logSince(page, 0, 'ws_in', 'goal.update')).at(-1)?.goals as { status: string }[] | undefined, { timeout: 60_000 })
    .toEqual(expect.arrayContaining([expect.objectContaining({ status: 'done' })]))
  await expect(page.locator('ol.goals li.done')).not.toHaveCount(0)

  await endConversation(page)
  await expect(page.getByRole('heading').first()).toBeVisible()
  expect(offHost).toEqual([])
})
