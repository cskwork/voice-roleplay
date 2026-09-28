// PA-1/PA-4/PA-5 on the real stack (PRD §8.4, AT-23): a reading attempt gets word timings from the pronunciation
// worker, the result page offers word-by-word compare playback (my take vs the model audio) and a pitch contour
// labelled as a reference measure; with VR_PRON_EXPERIMENTAL unset there are no bands and no numbers about quality.
// FAKE-MIC-SOURCE fixture through the app's recorder; everything else is real (gateway, ASR, TTS, aligner, pyworld).
import { expect, test, type Page } from '@playwright/test'
import { api, preparePage, say, waitUntilPerf } from '../lib/harness'

/** Records every HTMLMediaElement.play() (the app plays word slices as WAV blobs) with the element's duration. */
async function trackPlays(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as unknown as { __plays: { src: string; duration: number | null }[] }
    w.__plays = []
    const orig = HTMLMediaElement.prototype.play
    HTMLMediaElement.prototype.play = function (this: HTMLMediaElement) {
      const rec = { src: this.src, duration: null as number | null }
      w.__plays.push(rec)
      const set = () => (rec.duration = this.duration)
      if (this.readyState >= 1) set()
      else this.addEventListener('loadedmetadata', set, { once: true })
      return orig.call(this)
    }
  })
}

const plays = (page: Page) => page.evaluate(() => (window as unknown as { __plays: { src: string; duration: number | null }[] }).__plays)

test('PA-1/PA-4/AT-23 reading: word compare playback, contour as reference, no bands or scores without the flag', async ({ page }) => {
  await preparePage(page)
  await trackPlays(page)
  const health = await (await page.request.get('/api/health')).json()
  test.skip(!health.workers.pron?.ready, 'pronunciation worker not running (optional component)')
  expect(health.pronunciation_assessment, 'default mode without VR_PRON_EXPERIMENTAL').toBe('timing_only')
  expect(health.workers.pron.bands_enabled).toBe(false)
  expect(health.modes.realtime.session_active).toBe(false)
  // The model-voice half of the comparison needs the TTS worker; without it that half must be visibly off.
  const ttsReady = health.workers.tts?.ready === true
  if (!ttsReady) test.info().annotations.push({ type: 'degraded', description: 'TTS worker not ready: model-audio comparison checked as disabled only' })

  await page.goto('/#/practice/cafe_order/normal:0/reading')
  await page.evaluate(() => window.__e2e.micReady())
  await expect(page.locator('.prompt-en')).toContainText('cappuccino')
  await page.getByRole('button', { name: /^(녹음 시작|다시 녹음)$/ }).click()
  await expect(page.getByRole('button', { name: '녹음 정지' })).toBeVisible()
  const mic = await say(page, 'e2e_read_01', 0.2)
  await waitUntilPerf(page, mic.endPerf + 400)
  await page.getByRole('button', { name: '녹음 정지' }).click()
  await page.getByRole('button', { name: '제출하고 분석받기' }).click()
  await page.waitForURL(/#\/result\//, { timeout: 120_000 })
  const attemptId = decodeURIComponent(page.url().split('/').pop()!)

  // API: timing_only, every band null, nothing numeric about quality leaves the gateway.
  const a = await api(page)
  const r = await (await a.get(`/api/attempts/${attemptId}/result`)).json()
  const p = r.pronunciation
  expect(r.pronunciation_score).toBeNull()
  expect(r.pronunciation_status).toBe('timing_only')
  expect(p.status).toBe('timing_only')
  expect(p.mode).toBe('scripted')
  expect(p.words.map((w: { word: string }) => w.word)).toEqual(["I'd", 'like', 'a', 'large', 'cappuccino', 'with', 'oat', 'milk', 'please'])
  for (const w of p.words) {
    expect(w.band).toBeNull()
    expect(w.heard_ipa).toBeNull()
    expect(w.end_ms).toBeGreaterThanOrEqual(w.start_ms)
  }
  const text = JSON.stringify(r)
  expect(text).not.toMatch(/"(gop|word_gop|p|heard_candidates)"\s*:/)
  expect(text).not.toMatch(/"(pronunciation|phoneme|accent|fluency)_?score"\s*:\s*[0-9]/i)
  expect(p.prosody.learner.hop_ms).toBe(10)
  expect(p.prosody.learner.f0_hz.filter((v: number) => v > 0).length, 'voiced frames in the learner contour').toBeGreaterThan(50)
  const model = p.prosody.model
  if (ttsReady) {
    expect(model, 'model audio aligned').not.toBeNull()
    expect(model.words.map((w: { word: string }) => w.word)).toEqual(p.words.map((w: { word: string }) => w.word))
  } else {
    expect(model).toBeNull()
  }

  // Page: the card, compare playback of one word from both recordings.
  const card = page.locator('section.pron')
  await expect(card.getByRole('heading', { name: '단어별로 들어 보기' })).toBeVisible()
  await expect(card.getByText('발음을 채점하지 않습니다')).toBeVisible()
  const word = card.getByRole('button', { name: /^cappuccino/ })
  await word.click()
  await expect(word).toHaveAttribute('aria-pressed', 'true')
  const cap = p.words.find((w: { word: string }) => w.word === 'cappuccino')
  await card.getByRole('button', { name: '내 발음' }).click()
  await expect.poll(async () => (await plays(page)).at(-1)?.duration ?? null).not.toBeNull()
  const mine = (await plays(page)).at(-1)!
  expect(mine.src).toMatch(/^blob:/)
  // Slice = the word's interval plus 60 ms context each side (clamped to the take).
  expect(mine.duration!).toBeGreaterThan((cap.end_ms - cap.start_ms) / 1000)
  expect(mine.duration!).toBeLessThan((cap.end_ms - cap.start_ms + 130) / 1000)
  const modelButton = card.getByRole('button', { name: '모범 음성' })
  if (ttsReady) {
    const capModel = model.words.find((w: { i: number }) => w.i === cap.i)
    await modelButton.click()
    await expect.poll(async () => (await plays(page)).length).toBe(2)
    await expect.poll(async () => (await plays(page)).at(-1)?.duration ?? null).not.toBeNull()
    const theirs = (await plays(page)).at(-1)!
    expect(theirs.duration!).toBeGreaterThan((capModel.end_ms - capModel.start_ms) / 1000)
    expect(theirs.duration!).toBeLessThan((capModel.end_ms - capModel.start_ms + 130) / 1000)
  } else {
    await expect(modelButton).toBeDisabled()
    await expect(card.getByText('모범 음성의 단어 위치가 없어요.')).toBeVisible()
  }

  // Contour chart labelled as a reference measure, with a legend for both lines and a table alternative.
  await expect(card.getByText('참고 지표 · 점수 아님')).toBeVisible()
  await expect(card.getByRole('img', { name: /음높이/ })).toBeVisible()
  await expect(card.locator('.pron-legend li')).toHaveText(ttsReady ? ['내 녹음', '모범 음성'] : ['내 녹음'])
  await card.getByText('단어별 길이와 쉼 표로 보기').click()
  await expect(card.locator('.pron-table tbody tr')).toHaveCount(p.words.length)

  // Flag off: no bands, no experimental label, no numbers about quality anywhere on the page.
  await expect(page.locator('.pron-word[class*="band-"]')).toHaveCount(0)
  await expect(page.getByText('실험적 — 한국어 학습자 검증 전')).toHaveCount(0)
  const body = await page.locator('main').innerText()
  expect(body).not.toMatch(/\d+\s*%|\d+\s*점\b|발음\s*점수\s*[:：]?\s*\d|native[- ]like|원어민\s*유사도|정확도\s*\d/i)
  await expect(page.getByText('발음 평가는 제공되지 않습니다')).toBeVisible()
  await page.screenshot({ path: 'test-results/pronunciation-result.png', fullPage: true })
})
