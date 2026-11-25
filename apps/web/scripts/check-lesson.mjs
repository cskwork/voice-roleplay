// Uses an existing CDP browser in a fresh isolated context; never launches/downloads a browser.
// PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node apps/web/scripts/check-lesson.mjs
import { mkdir, readFile, readdir, writeFile } from 'node:fs/promises'
import assert from 'node:assert/strict'
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE || 'playwright')
const output = process.env.VOICE_PROOF || '/private/tmp/forge-speaking-20261111/voice-proof'
await mkdir(output, { recursive: true })
const scenarioRoot = new URL('../../../content/scenarios/', import.meta.url)
const scenarios = await Promise.all((await readdir(scenarioRoot)).filter((p) => p.endsWith('.json')).map(async (p) => JSON.parse(await readFile(new URL(p, scenarioRoot), 'utf8'))))
const friend = scenarios.find((s) => s.scenario_id === 'friend_intro') ?? scenarios[0]
const expressions = friend.goals.slice(0, 3).map((g) => friend.model_expressions.find((e) => e.goal_id === g.goal_id))
const browser = await chromium.connectOverCDP(process.env.CDP_URL || 'http://127.0.0.1:9222')
const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' })
const page = await context.newPage()
const errors = [], attempts = new Map(), requests = [], checks = []
let counter = 0, responseMode = 'same'
page.on('pageerror', (e) => errors.push(e.message))
await page.addInitScript(() => {
  window.__micStarts = 0
  navigator.mediaDevices.getUserMedia = async () => {
    window.__micStarts++
    if (window.__denyMic) throw new DOMException('Denied', 'NotAllowedError')
    const ctx = new AudioContext(), oscillator = ctx.createOscillator(), dest = ctx.createMediaStreamDestination()
    oscillator.frequency.value = 200; oscillator.connect(dest); oscillator.start(); await ctx.resume()
    const track = dest.stream.getAudioTracks()[0], stop = track.stop.bind(track)
    track.stop = () => { stop(); oscillator.stop(); void ctx.close() }
    return dest.stream
  }
})
await page.route('**/api/**', async (route) => {
  const req = route.request(), path = new URL(req.url()).pathname
  requests.push({ method: req.method(), path })
  const json = (body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
  if (path === '/api/health') return json({ shared: true, mode: 'api', workers: { asr: { ready: true }, tts: { ready: true, voices: [{ voice_id: friend.default_voice_id, label: 'Test' }] }, llm: { ready: true }, vad: { ready: true } }, modes: { recorded: { available: true }, realtime: { available: true } } })
  if (path === '/api/scenarios') return json({ scenarios, capabilities: { guided_lessons: true } })
  if (path === '/api/settings') return json({ difficulty: 'normal', history_opt_in: true })
  if (path === '/api/bootstrap') return json({ csrf_token: 'test-csrf' })
  if (path === '/api/attempts' && req.method() === 'POST') {
    const body = req.postDataJSON(), id = `a${++counter}`
    assert.equal(body.history_opt_in, false); assert.equal(body.exercise_type, 'shadowing')
    attempts.set(id, { body, mode: responseMode })
    return json({ attempt_id: id })
  }
  const match = path.match(/^\/api\/attempts\/(a\d+)\/(audio|submit|result)$/)
  if (match) {
    const [, id, action] = match, attempt = attempts.get(id)
    if (action === 'audio') { assert.ok(req.postDataBuffer().length > 44); attempt.uploaded = true; return json({}) }
    if (action === 'submit') { assert.equal(attempt.uploaded, true); return json({ job_id: id, attempt_id: id, state: 'queued' }) }
    const exp = expressions.find((e) => e.text_id === attempt.body.text_id)
    const mode = attempt.mode
    return json({ attempt_id: id, exercise_type: 'shadowing', scenario_id: friend.scenario_id, transcript: mode === 'empty' ? '  ' : mode === 'no_speech' ? null : mode === 'different' ? 'Hi, I Minji.' : exp.en, no_speech: mode === 'no_speech', target_en: exp.en, feedback: [], feedback_status: null, target_diff: mode === 'unavailable' ? null : mode === 'different' ? [{ op: 'missing', target: "I'm", heard: null }, { op: 'missing', target: 'Nice', heard: null }] : [{ op: 'equal', target: exp.en, heard: exp.en }], pronunciation_score: null })
  }
  const poll = path.match(/^\/api\/jobs\/(a\d+)$/)
  if (poll) {
    if (req.method() === 'DELETE') return json({ job_id: poll[1], state: 'cancelled' })
    if (attempts.get(poll[1]).mode === 'poll_error' && !attempts.get(poll[1]).pollFailed) { attempts.get(poll[1]).pollFailed = true; return json({ error: { code: 'NETWORK' } }, 503) }
    return json({ job_id: poll[1], attempt_id: poll[1], state: 'completed' })
  }
  throw new Error(`Unexpected API ${req.method()} ${path}`)
})
const base = process.env.VOICE_URL || 'http://127.0.0.1:5174/'
const overflow = async () => { assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false) }
const screenshot = async (name) => { await overflow(); await page.screenshot({ path: `${output}/${name}.png`, fullPage: true }) }
const record = async (mode) => {
  responseMode = mode
  await page.getByRole('button', { name: '녹음 시작', exact: true }).click()
  await page.getByRole('button', { name: '녹음 정지', exact: true }).waitFor()
  await page.waitForTimeout(1350)
  await page.getByRole('button', { name: '녹음 정지', exact: true }).click()
  await page.getByRole('button', { name: '보내고 문장 확인하기', exact: true }).click()
}
try {
  await page.goto(`${base}#/home`)
  await page.getByRole('button', { name: '표현 3개 연습하기', exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__micStarts), 0)
  assert.equal(await page.getByRole('radio', { name: '짧은 말하기', exact: true }).isChecked(), true)
  await screenshot('home-1440'); await page.setViewportSize({ width: 390, height: 844 }); await screenshot('home-390')
  checks.push('home default lesson, no microphone before click, 1440/390 no overflow')
  await page.getByRole('button', { name: '표현 3개 연습하기', exact: true }).click()
  await page.getByRole('heading', { name: friend.goals[0].ko, exact: true }).waitFor()
  assert.ok(page.url().endsWith('/normal%3A0'))
  await screenshot('lesson-390'); await page.setViewportSize({ width: 1440, height: 900 }); await screenshot('lesson-1440')
  await page.getByRole('button', { name: '뜻 보기', exact: true }).click()
  assert.ok(await page.locator('#lesson-hint').innerText())
  await page.getByRole('button', { name: '문장 조각 보기', exact: true }).click()
  await page.getByRole('button', { name: '예문 도움 보기', exact: true }).click()
  await record('same'); await page.getByText('인식된 문장이 예문과 같아요.', { exact: true }).waitFor()
  await screenshot('lesson-same-1440')
  await page.getByRole('button', { name: '한 번 더 말하기', exact: true }).click()
  assert.equal(await page.getByRole('heading', { name: '이렇게 들렸어요', exact: true }).count(), 0)
  await record('different'); await page.getByText('예문에는', { exact: false }).waitFor()
  assert.equal(await page.getByText('“Nice”', { exact: true }).count(), 0)
  await page.getByRole('button', { name: '다음 표현', exact: true }).click()
  await record('no_speech'); await page.getByRole('heading', { name: '말한 내용을 듣지 못했어요', exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '다음 표현', exact: true }).count(), 0)
  await screenshot('lesson-no-speech-1440')
  await page.getByRole('button', { name: '한 번 더 말하기', exact: true }).click()
  await record('unavailable'); await page.getByText('예문과의 비교 자료가 없어', { exact: false }).waitFor()
  await page.getByRole('button', { name: '다음 표현', exact: true }).click()
  await record('empty'); await page.getByRole('heading', { name: '말한 내용을 듣지 못했어요', exact: true }).waitFor()
  assert.equal(await page.getByRole('button', { name: '이번 연습 마치기', exact: true }).count(), 0)
  await page.getByRole('button', { name: '한 번 더 말하기', exact: true }).click()
  await record('poll_error'); await page.getByRole('alert').waitFor()
  assert.equal(await page.getByRole('button', { name: '이번 연습 마치기', exact: true }).count(), 0)
  await screenshot('lesson-poll-error-1440')
  const micStartsBeforeRetry = await page.evaluate(() => window.__micStarts)
  const submitsBeforeRetry = requests.filter((r) => r.path.endsWith('/submit')).length
  await page.getByRole('button', { name: '보내고 문장 확인하기', exact: true }).click()
  await page.getByRole('button', { name: '이번 연습 마치기', exact: true }).waitFor()
  assert.equal(await page.evaluate(() => window.__micStarts), micStartsBeforeRetry)
  assert.equal(requests.filter((r) => r.path.endsWith('/submit')).length, submitsBeforeRetry)
  assert.equal(requests.filter((r) => r.method === 'DELETE').length, 0)
  await page.getByRole('button', { name: '이번 연습 마치기', exact: true }).click()
  await page.getByRole('heading', { name: '표현 3개를 말해 봤어요', exact: true }).waitFor()
  await screenshot('lesson-complete-1440'); await page.setViewportSize({ width: 390, height: 844 }); await screenshot('lesson-complete-390')
  assert.equal(await page.locator('.lesson-recap li').count(), 3)
  checks.push('real capture worklet with synthetic microphone; hints, match, one difference, retry, no_speech, empty, missing comparison, poll failure, explicit completion')
  await page.getByRole('button', { name: '다시 말하기', exact: true }).click()
  assert.equal(await page.locator('.lesson-recap').count(), 0)
  await page.evaluate(() => { window.__denyMic = true })
  await page.getByRole('button', { name: '녹음 시작', exact: true }).click()
  await page.getByRole('alert').waitFor(); await screenshot('lesson-mic-denied-390')
  await page.goto(`${base}#/talk/${friend.scenario_id}/normal:0`); await page.getByRole('button', { name: '자유 대화 시작하기', exact: true }).waitFor(); await screenshot('talk-390')
  await page.goto(`${base}#/practice/${friend.scenario_id}/normal:0`); await page.getByRole('tab', { name: '따라 말하기', exact: true }).waitFor(); await screenshot('practice-390')
  await page.goto(`${base}#/lesson/not-a-scenario/normal:0`)
  await page.getByRole('heading', { name: '이 상황의 짧은 연습을 준비하지 못했어요', exact: true }).waitFor()
  checks.push('completion restart clears session, microphone denial, existing talk/practice entry routes, unknown lesson safe fallback')
  assert.deepEqual(errors, [])
  assert.ok([...attempts.values()].every((a) => a.body.history_opt_in === false))
  await writeFile(`${output}/browser.json`, JSON.stringify({ pass: true, checks, attempts: attempts.size, errors, requests, limits: 'Synthetic microphone + mocked gateway only. No real speech recognition, TTS, providers, semantic or pronunciation assessment.' }, null, 2))
  console.log(JSON.stringify({ pass: true, checks, attempts: attempts.size, screenshots: output }))
} finally { await context.close(); await browser.close() }
