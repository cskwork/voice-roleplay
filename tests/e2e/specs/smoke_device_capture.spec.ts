// Smoke through the browser's real device path: getUserMedia is NOT overridden here. Chromium's fake capture
// device plays tests/fixtures/audio/e2e_fake_capture_48k.wav (8 s lead-in, one learner line, 4 s tail) once.
import { join } from 'node:path'
import { expect, test } from '@playwright/test'
import { CHROMIUM_ARGS } from '../playwright.config'
import { endConversation, FIXTURES, logLen, nextReply, preparePage, waitFor, waitRealtimeFree } from '../lib/harness'

test.use({
  launchOptions: {
    args: [...CHROMIUM_ARGS, '--use-fake-device-for-media-stream', `--use-file-for-fake-audio-capture=${join(FIXTURES, 'e2e_fake_capture_48k.wav')}%noloop`],
  },
})

test('device path: Chromium fake capture device -> app capture worklet -> gateway -> reply plays', async ({ page }) => {
  await preparePage(page)
  await page.addInitScript(() => {
    window.__e2e.realMic = true
  })
  await waitRealtimeFree(page.request)
  await page.goto('/#/talk/cafe_order/normal:0')
  const from = await logLen(page)
  await page.getByRole('button', { name: '대화 시작' }).click()
  const speech = await waitFor(page, from, { kind: 'ws_in', type: 'speech.started' }, 30_000)
  const final = await waitFor(page, from, { kind: 'ws_in', type: 'asr.final', turn_id: speech.turn_id }, 30_000)
  expect(String(final.text).toLowerCase()).toContain('latte')
  const gum = await page.evaluate(() => window.__e2e.log.filter((e) => e.kind === 'gum').length)
  expect(gum, 'the fake MediaStream override was not used').toBe(0)
  const reply = await nextReply(page, from)
  expect(reply.firstPlayedAt).not.toBeNull()
  await endConversation(page)
})
