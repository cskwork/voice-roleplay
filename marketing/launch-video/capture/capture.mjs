// Captures real UI states of the running app (./app start, http://127.0.0.1:8710) for the launch video.
//
// Real stack, real models, unmodified app. The only substitute is the microphone source: the E2E suite's
// FAKE-MIC-SOURCE init script (tests/e2e/lib/fakeMic.js) feeds learner lines made with macOS `say` into
// getUserMedia. Nothing of that learner audio ends up in the video soundtrack.
//
// Output (assets/captures/):
//   mobile/*.png   390x844 @3x   named states (home, talk, hint levels, summary, result)
//   desktop/*.png  1440x900 @2x  home, talk, result
//   burst/*.jpg    rapid screenshots during the live conversation (partial captions, AI speaking, barge-in)
//   manifest.json  per image: CSS-px rects of key elements, so the video can zoom and call out real UI
//
// Usage: node capture/capture.mjs [--only mobile|desktop]
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

const HERE = dirname(fileURLToPath(import.meta.url))
const PKG = resolve(HERE, '..')
const ROOT = resolve(PKG, '../..')
const BASE = process.env.VR_URL ?? 'http://127.0.0.1:8710'
const OUT = join(PKG, 'assets/captures')
const BURST = join(PKG, '.cache/burst') // raw burst frames (gitignored); keepers are copied into OUT/burst
const GEN = join(PKG, '.cache/fixtures')
const FIXTURE_DIRS = [join(ROOT, 'tests/fixtures/audio'), GEN]
const only = process.argv.includes('--only') ? process.argv[process.argv.indexOf('--only') + 1] : null

const manifest = existsSync(join(OUT, 'manifest.json')) ? JSON.parse(readFileSync(join(OUT, 'manifest.json'), 'utf8')) : {}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const log = (...a) => console.log(new Date().toISOString().slice(11, 19), ...a)

// Learner lines (FAKE-MIC-SOURCE). Existing E2E fixtures where they fit, the rest made on demand.
const LINES = {
  e2e_cafe_01: null, // "Hi, can I get a large latte, please?"
  e2e_cafe_02: null, // "Could I have oat milk instead of whole milk?"
  e2e_barge: null, // "Sorry, wait, I have a quick question about the menu."
  e2e_cafe_03: null, // "How much is that in total?"
  lv_pay: 'I want pay with my phone.', // a typical learner slip, so the summary has something to drill
  e2e_read_01: null, // "I'd like a large cappuccino with oat milk, please." (reading task target)
}

function makeFixture(name, text) {
  const wav = join(GEN, `${name}.wav`)
  if (existsSync(wav) && existsSync(join(GEN, `${name}.txt`)) && readFileSync(join(GEN, `${name}.txt`), 'utf8').trim() === text) return
  mkdirSync(GEN, { recursive: true })
  const tmp = mkdtempSync(join(tmpdir(), 'lv-fx-'))
  try {
    execFileSync('say', ['-v', 'Samantha', '-o', join(tmp, 'a.aiff'), text])
    execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-i', join(tmp, 'a.aiff'), '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', wav])
  } finally {
    rmSync(tmp, { recursive: true, force: true })
  }
  writeFileSync(join(GEN, `${name}.txt`), `${text}\n`)
}

// ------------------------------------------------------------------ stack

async function health() {
  for (let i = 0; ; i++) {
    try {
      const r = await fetch(`${BASE}/api/health`)
      return await r.json()
    } catch (e) {
      if (i > 120) throw e
      await sleep(2000) // the stack may be restarting; retry
    }
  }
}

/** Every worker ready and no other realtime session running (never end someone else's session). */
async function waitStackFree(maxMin = 30) {
  const until = Date.now() + maxMin * 60_000
  for (;;) {
    const h = await health()
    const ready = h.gateway?.ready && ['asr', 'tts', 'llm', 'vad', 'pron'].every((k) => h.workers?.[k]?.ready)
    if (ready && h.modes?.realtime?.available && !h.modes.realtime.session_active) return h
    if (Date.now() > until) throw new Error(`stack not free after ${maxMin} min: ${JSON.stringify(h.modes)}`)
    log('waiting for stack (ready=%s, session_active=%s)', ready, h.modes?.realtime?.session_active)
    await sleep(5000)
  }
}

// ------------------------------------------------------------------ page helpers

async function newPage(browser, viewport, dpr) {
  const ctx = await browser.newContext({ viewport, deviceScaleFactor: dpr, locale: 'ko-KR', hasTouch: viewport.width < 600, baseURL: BASE })
  const page = await ctx.newPage()
  await page.addInitScript({ path: join(ROOT, 'tests/e2e/lib/fakeMic.js') })
  await page.route('**/__e2e/fixtures/*.wav', async (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop())
    const file = FIXTURE_DIRS.map((d) => join(d, name)).find((p) => existsSync(p))
    if (!file) return route.fulfill({ status: 404, body: 'missing fixture' })
    return route.fulfill({ status: 200, contentType: 'audio/wav', body: readFileSync(file) })
  })
  // Hide the text caret and scrollbars so stills are clean (capture only; app code untouched).
  await page.addInitScript(() => {
    addEventListener('DOMContentLoaded', () => {
      const s = document.createElement('style')
      s.textContent = '*{caret-color:transparent!important} ::-webkit-scrollbar{display:none} :focus-visible{outline:none!important}'
      document.head.appendChild(s)
    })
  })
  return page
}

const logLen = (page) => page.evaluate(() => window.__e2e.log.length)
async function waitLog(page, from, match, timeout = 60_000) {
  const h = await page.waitForFunction(
    ([f, m]) => window.__e2e.log.slice(f).find((e) => Object.entries(m).every(([k, v]) => e[k] === v)) ?? null,
    [from, match],
    { timeout, polling: 30 },
  )
  return h.jsonValue()
}
async function say(page, name, delay = 0.1) {
  await page.evaluate((x) => window.__e2e.load(x), name)
  return page.evaluate(([n, d]) => window.__e2e.play(n, d), [name, delay])
}
const waitPerf = (page, p) => page.waitForFunction((x) => performance.now() >= x, p, { polling: 20, timeout: 120_000 })

/** Wait until every audio segment of a response has finished playing (or it was cancelled). */
async function waitPlayed(page, from, id, timeout = 90_000) {
  await waitLog(page, from, { kind: 'ws_in', type: 'response.done', response_id: id }, timeout)
  await page.waitForFunction(
    ([f, rid]) => {
      const L = window.__e2e.log.slice(f)
      const segs = new Set(L.filter((e) => e.kind === 'ws_audio' && e.response_id === rid).map((e) => String(e.segment_id)))
      const done = new Set(L.filter((e) => e.kind === 'play' && e.type === 'completed' && e.key === rid).map((e) => e.seg))
      return L.some((e) => e.kind === 'ws_in' && e.type === 'response.cancelled' && e.response_id === rid) || [...segs].every((s) => done.has(s))
    },
    [from, id],
    { timeout, polling: 30 },
  )
}

const RECT_SELECTORS = {
  topbar: '.topbar',
  healthDot: '.health-dot',
  modeSwitch: '[role="radiogroup"]',
  scenario: 'li.scenario',
  talkHead: '.talk-head',
  stateChip: '.state-chip',
  goals: '.goals',
  goalMeta: '.page-meta',
  captions: '.captions',
  bubble: '.captions .bubble',
  hint: '.hint-panel',
  keywords: '.hint-panel .keywords',
  example: '.hint-panel .example',
  orb: '.mic-orb',
  dock: '.control-dock',
  dockTools: '.dock-tools',
  stopBtn: '.btn-stop',
  summaryH1: 'main h1, h1',
  said: 'ol.said',
  feedback: 'article.feedback',
  drillBtn: 'article.feedback button',
  pron: 'section.pron',
  pronWord: '.pron-word',
  contour: 'section.pron svg',
  legend: '.pron-legend',
  transcript: 'p.transcript',
  heading: 'h2',
}

async function rects(page) {
  return page.evaluate((sel) => {
    const out = {}
    for (const [k, s] of Object.entries(sel)) {
      const els = [...document.querySelectorAll(s)].filter((e) => e.getClientRects().length)
      if (!els.length) continue
      out[k] = els.slice(0, 24).map((e) => {
        const r = e.getBoundingClientRect()
        return { x: Math.round(r.left + scrollX), y: Math.round(r.top + scrollY), w: Math.round(r.width), h: Math.round(r.height), text: (e.innerText || '').slice(0, 80) }
      })
    }
    return out
  }, RECT_SELECTORS)
}

async function shot(page, group, name, { fullPage = false } = {}) {
  mkdirSync(join(OUT, group), { recursive: true })
  const file = `${group}/${name}.png`
  await page.screenshot({ path: join(OUT, file), fullPage, animations: 'disabled' })
  const vp = page.viewportSize()
  const dims = await page.evaluate(() => ({ docH: document.documentElement.scrollHeight, scrollY }))
  manifest[file] = { viewport: vp, fullPage, ...dims, rects: await rects(page) }
  writeFileSync(join(OUT, 'manifest.json'), JSON.stringify(manifest, null, 1))
  log('shot', file)
}

/** Screenshot loop in the background (JPEG, fast) until stop(); each frame tagged with elapsed ms and UI state. */
function burst(page, tag, everyMs = 180) {
  const dir = join(BURST, tag)
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  const frames = []
  let run = true
  const t0 = Date.now()
  const loop = (async () => {
    let i = 0
    while (run) {
      const t = Date.now() - t0
      const f = `${String(i).padStart(4, '0')}.jpg`
      try {
        await page.screenshot({ path: join(dir, f), type: 'jpeg', quality: 92 })
        const st = await page.evaluate(() => ({
          chip: document.querySelector('.state-chip')?.innerText ?? '',
          bubbles: document.querySelectorAll('.captions .bubble').length,
          partial: !!document.querySelector('.bubble.user.partial'),
          goals: document.querySelector('.page-meta, .goal-count')?.innerText ?? '',
        }))
        frames.push({ f, t, ...st })
        i++
      } catch {
        /* page navigating */
      }
      await sleep(everyMs)
    }
  })()
  return {
    async stop() {
      run = false
      await loop
      writeFileSync(join(dir, 'frames.json'), JSON.stringify(frames, null, 1))
      log(`burst ${tag}: ${frames.length} frames`)
      return frames
    },
  }
}

// ------------------------------------------------------------------ flows

async function homeShots(page, group) {
  await page.goto('/#/home')
  await page.locator('li.scenario').first().waitFor()
  await page.getByText('모델 준비됨').waitFor({ timeout: 120_000 })
  await sleep(600)
  await shot(page, group, 'home')
  await shot(page, group, 'home-full', { fullPage: true })
}

/** Live conversation: opening line, 2 turns, barge-in, hints 1-3, total price, a slip for the summary. */
async function conversation(page, group, { bargeIn = true, bursts = true } = {}) {
  await waitStackFree()
  await page.goto('/#/talk/cafe_order/normal:0')
  await page.evaluate(() => window.__e2e.micReady())
  const start = page.getByRole('button', { name: '대화 시작' })
  await start.waitFor()
  await page.waitForFunction(() => !document.querySelector('button[aria-disabled="true"]') || true)
  await sleep(500)
  await shot(page, group, 'talk-ready')
  const b = bursts ? burst(page, `${group}-talk`) : null
  let from = await logLen(page)
  await start.click()
  const opening = await waitLog(page, from, { kind: 'ws_in', type: 'response.started', opening: true }, 60_000)
  await waitLog(page, from, { kind: 'play', type: 'started', key: opening.response_id }, 30_000)
  await sleep(1800)
  await shot(page, group, 'talk-ai-speaking')
  await waitPlayed(page, from, opening.response_id)

  const turn = async (name, label) => {
    const f = await logLen(page)
    const mic = await say(page, name)
    await waitLog(page, f, { kind: 'ws_in', type: 'asr.partial' }, 20_000)
    await sleep(250)
    await shot(page, group, `${label}-partial`)
    await waitLog(page, f, { kind: 'ws_in', type: 'asr.final' }, 30_000)
    await shot(page, group, `${label}-final`)
    const st = await waitLog(page, f, { kind: 'ws_in', type: 'response.started' }, 60_000)
    await waitLog(page, f, { kind: 'play', type: 'started', key: st.response_id }, 60_000)
    await sleep(1200)
    await shot(page, group, `${label}-reply`)
    return { f, mic, id: st.response_id }
  }

  let t = await turn('e2e_cafe_01', 't1')
  await waitPlayed(page, t.f, t.id)
  await shot(page, group, 't1-done')
  t = await turn('e2e_cafe_02', 't2')
  if (bargeIn) {
    // Barge in while the reply is still playing.
    const f = await logLen(page)
    await say(page, 'e2e_barge')
    await waitLog(page, f, { kind: 'ws_in', type: 'response.cancelled' }, 20_000).catch(() => log('no cancel (reply already done)'))
    await sleep(300)
    await shot(page, group, 'barge-cancelled')
    await waitLog(page, f, { kind: 'ws_in', type: 'asr.final' }, 30_000)
    const st = await waitLog(page, f, { kind: 'ws_in', type: 'response.started' }, 60_000)
    await waitLog(page, f, { kind: 'play', type: 'started', key: st.response_id }, 60_000)
    await sleep(1000)
    await shot(page, group, 'barge-reply')
    await waitPlayed(page, f, st.response_id)
  } else {
    await waitPlayed(page, t.f, t.id)
  }
  await shot(page, group, 't2-done')

  // Hint ladder 1 -> 3 (expand the panel where it is collapsible).
  for (const lvl of [1, 2, 3]) {
    await page.getByRole('button', { name: new RegExp(`힌트 ${lvl}/3`) }).click()
    await sleep(400)
    const toggle = page.locator('.hint-toggle[aria-expanded="false"]')
    if (await toggle.count()) await toggle.click()
    await sleep(2500) // gloss arrives from the LLM
    await shot(page, group, `hint-${lvl}`)
    if (lvl >= 2) {
      // The panel scrolls on its own; bring the keywords / example sentence into view.
      await page.evaluate((sel) => {
        const el = document.querySelector(sel)
        for (let p = el?.parentElement; p; p = p.parentElement) if (p.scrollHeight > p.clientHeight + 4) p.scrollTop = p.scrollHeight
      }, lvl === 2 ? '.hint-panel .keywords' : '.hint-panel .example')
      await sleep(400)
      await shot(page, group, `hint-${lvl}-detail`)
    }
  }

  t = await turn('e2e_cafe_03', 't3')
  await waitPlayed(page, t.f, t.id)
  await shot(page, group, 't3-done')
  t = await turn('lv_pay', 't4')
  await waitPlayed(page, t.f, t.id)
  await shot(page, group, 't4-done')
  if (b) await b.stop()

  // End -> summary.
  await page.getByRole('button', { name: /종료하고 요약 보기/ }).click()
  await page.waitForURL(/#\/summary/, { timeout: 60_000 })
  await page.getByRole('heading', { level: 1 }).waitFor()
  await sleep(1500)
  await shot(page, group, 'summary')
  await shot(page, group, 'summary-full', { fullPage: true })
  const drill = page.locator('article.feedback').filter({ has: page.getByRole('button', { name: /다시 말하기/ }) }).first()
  if (await drill.count()) {
    await drill.scrollIntoViewIfNeeded()
    await sleep(400)
    await shot(page, group, 'summary-feedback')
  }
}

async function reading(page, group) {
  await waitStackFree()
  await page.goto('/#/practice/cafe_order/normal:0/reading')
  await page.evaluate(() => window.__e2e.micReady())
  await page.locator('.prompt-en').waitFor()
  await sleep(500)
  await shot(page, group, 'practice')
  await page.getByRole('button', { name: /^(녹음 시작|다시 녹음)$/ }).click()
  await page.getByRole('button', { name: '녹음 정지' }).waitFor()
  const mic = await say(page, 'e2e_read_01', 0.2)
  await sleep(1400)
  await shot(page, group, 'practice-recording')
  await waitPerf(page, mic.endPerf + 400)
  await page.getByRole('button', { name: '녹음 정지' }).click()
  await page.locator('.take audio').waitFor()
  await page.getByRole('button', { name: '제출하고 분석받기' }).click()
  await page.waitForURL(/#\/result\//, { timeout: 180_000 })
  await page.locator('p.transcript').waitFor({ timeout: 180_000 })
  await page.locator('section.pron').waitFor({ timeout: 180_000 }).catch(() => log('no pronunciation card'))
  await sleep(1500)
  await shot(page, group, 'result')
  await shot(page, group, 'result-full', { fullPage: true })
  const card = page.locator('section.pron')
  if (await card.count()) {
    const w = card.getByRole('button', { name: /^cappuccino/ })
    if (await w.count()) await w.click()
    await card.scrollIntoViewIfNeeded()
    await page.evaluate(() => {
      const el = document.querySelector('section.pron')
      window.scrollTo(0, el.getBoundingClientRect().top + scrollY - 12)
    })
    await sleep(600)
    await shot(page, group, 'result-pron')
  }
}

// ------------------------------------------------------------------ main

for (const [n, text] of Object.entries(LINES)) if (text) makeFixture(n, text)
mkdirSync(OUT, { recursive: true })
await waitStackFree()
const browser = await chromium.launch({
  headless: true,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio', '--use-fake-ui-for-media-stream'],
})
try {
  if (!only || only === 'mobile') {
    const m = await newPage(browser, { width: 390, height: 844 }, 3)
    await homeShots(m, 'mobile')
    await conversation(m, 'mobile')
    await reading(m, 'mobile')
    await m.context().close()
  }
  if (!only || only === 'desktop') {
    const d = await newPage(browser, { width: 1440, height: 900 }, 2)
    await homeShots(d, 'desktop')
    await conversation(d, 'desktop', { bargeIn: false, bursts: true })
    await reading(d, 'desktop')
    await d.context().close()
  }
} finally {
  await browser.close()
}
log('done')
