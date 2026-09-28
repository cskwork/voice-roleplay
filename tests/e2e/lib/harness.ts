import { execFileSync, spawn } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, type APIRequestContext, type Page } from '@playwright/test'

export const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '../../..')
export const FIXTURES = join(ROOT, 'tests/fixtures/audio')
/** Spec-specific fixtures made on demand by `makeFixture` (gitignored). */
export const GEN_FIXTURES = join(ROOT, 'tests/e2e/.out/fixtures')
export const BASE_URL = process.env.VR_E2E_URL ?? 'http://127.0.0.1:8710'
const INIT_SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'fakeMic.js')

export interface LogEntry {
  t: number
  kind: string
  type?: string
  [k: string]: unknown
}

export interface MicPlay {
  name: string
  startPerf: number
  firstVoicedPerf: number
  lastVoicedPerf: number
  endPerf: number
}

const fixturePath = (file: string) => [join(FIXTURES, file), join(GEN_FIXTURES, file)].find((p) => existsSync(p))

export function fixtureText(name: string): string {
  return readFileSync(fixturePath(`${name}.txt`) ?? join(FIXTURES, `${name}.txt`), 'utf8').trim()
}

/** FAKE-MIC-SOURCE fixture made like tests/fixtures/make_fixtures.sh (macOS `say` Samantha → 16 kHz mono PCM16). */
export function makeFixture(name: string, text: string): void {
  const wavFile = join(GEN_FIXTURES, `${name}.wav`)
  const txtFile = join(GEN_FIXTURES, `${name}.txt`)
  if (existsSync(wavFile) && existsSync(txtFile) && readFileSync(txtFile, 'utf8').trim() === text) return
  mkdirSync(GEN_FIXTURES, { recursive: true })
  const tmp = mkdtempSync(join(tmpdir(), 'vr-e2e-fx-'))
  try {
    execFileSync('say', ['-v', process.env.FIXTURE_VOICE ?? 'Samantha', '-o', join(tmp, 'a.aiff'), text])
    execFileSync('ffmpeg', ['-loglevel', 'error', '-y', '-i', join(tmp, 'a.aiff'), '-ar', '16000', '-ac', '1', '-sample_fmt', 's16', wavFile])
  } finally {
    rmSync(tmp, { recursive: true, force: true })
  }
  writeFileSync(txtFile, `${text}\n`)
}

/** Instrument a page: fake mic source, worklet/WS logging, fixture route, off-host request tracking. */
export async function preparePage(page: Page): Promise<{ offHost: string[] }> {
  const offHost: string[] = []
  await page.addInitScript({ path: INIT_SCRIPT })
  await page.route('**/__e2e/fixtures/*.wav', async (route) => {
    const name = decodeURIComponent(new URL(route.request().url()).pathname.split('/').pop()!)
    const file = fixturePath(name)
    if (!file) return route.fulfill({ status: 404, body: 'missing fixture' })
    return route.fulfill({ status: 200, contentType: 'audio/wav', body: readFileSync(file) })
  })
  page.on('request', (r) => {
    const u = new URL(r.url())
    if (!['127.0.0.1', 'localhost', '[::1]'].includes(u.hostname) && !u.protocol.startsWith('data') && !u.protocol.startsWith('blob')) {
      offHost.push(r.url())
    }
  })
  return { offHost }
}

// ------------------------------------------------------------------ page log helpers

export const logLen = (page: Page) => page.evaluate(() => window.__e2e.log.length)

export async function logSince(page: Page, from = 0, kind?: string, type?: string): Promise<LogEntry[]> {
  return page.evaluate(([f, k, t]) => window.__e2e.since(f as number, k as string, t as string), [from, kind, type] as const)
}

/** Wait until an entry matching `match` (partial object) appears at or after index `from`. */
export async function waitFor(page: Page, from: number, match: Record<string, unknown>, timeoutMs = 30_000): Promise<LogEntry> {
  const handle = await page.waitForFunction(
    ([f, m]) => window.__e2e.log.slice(f as number).find((e: Record<string, unknown>) => Object.entries(m as object).every(([k, v]) => e[k] === v)) ?? null,
    [from, match] as const,
    { timeout: timeoutMs, polling: 20 },
  )
  return (await handle.jsonValue()) as LogEntry
}

export async function loadFixtures(page: Page, names: string[]): Promise<void> {
  for (const n of names) await page.evaluate((x) => window.__e2e.load(x), n)
}

export async function say(page: Page, name: string, delayS = 0.1): Promise<MicPlay> {
  await page.evaluate((x) => window.__e2e.load(x), name)
  return page.evaluate(([n, d]) => window.__e2e.play(n as string, d as number), [name, delayS] as const)
}

/** Resolves when the page clock passes `perf` (ms, performance.now()). */
export async function waitUntilPerf(page: Page, perf: number): Promise<void> {
  await page.waitForFunction((p) => performance.now() >= p, perf, { polling: 10, timeout: 120_000 })
}

// ------------------------------------------------------------------ realtime flow

export interface Reply {
  responseId: string
  startedAt: number // response.started received
  firstAudioFrameAt: number | null // first output_audio frame received
  firstPlayedAt: number | null // playback worklet: segment 0 started
  completedAt: number // last segment completed (or response.done if nothing played)
  segments: string[]
  playGaps: number[] // ms between one segment's completion and the next segment's start
}

/** Open the Talk screen for a scenario, press 대화 시작, wait for the opening line to finish playing. */
export async function startConversation(page: Page, scenario = 'cafe_order', opts = 'normal:0', viaHome = false): Promise<Reply> {
  if (viaHome) {
    await page.goto('/#/home')
    await page.getByRole('radio', { name: /실시간 회화/ }).check()
    const card = page.locator('li.scenario', { hasText: scenarioTitle(scenario) })
    await expect(card.getByRole('button', { name: /이 상황으로 연습/ })).toBeEnabled({ timeout: 60_000 })
    await card.getByRole('button', { name: /이 상황으로 연습/ }).click()
  } else {
    await page.goto(`/#/talk/${scenario}/${opts}`)
  }
  await page.evaluate(() => window.__e2e.micReady())
  const from = await logLen(page)
  const start = page.getByRole('button', { name: '대화 시작' })
  await expect(start).toBeEnabled({ timeout: 60_000 })
  await start.click()
  const started = await waitFor(page, from, { kind: 'ws_in', type: 'response.started', opening: true }, 30_000)
  return waitReply(page, from, started.response_id as string)
}

/** Recorded practice: 녹음 시작 → speak a fixture → 녹음 정지, until the take preview is there. */
export async function recordTake(page: Page, fixture: string): Promise<void> {
  const rec = page.getByRole('button', { name: /^(녹음 시작|다시 녹음)$/ })
  await rec.click()
  await expect(page.getByRole('button', { name: '녹음 정지' })).toBeVisible()
  const mic = await say(page, fixture, 0.2)
  await waitUntilPerf(page, mic.endPerf + 400)
  await page.getByRole('button', { name: '녹음 정지' }).click()
  await expect(page.locator('.take audio')).toHaveCount(1)
}

/** 종료하고 요약 보기 → summary screen. */
export async function endConversation(page: Page): Promise<void> {
  const end = page.getByRole('button', { name: /종료하고 요약 보기/ })
  if (await end.isVisible().catch(() => false)) {
    await end.click()
    await page.waitForURL(/#\/summary/, { timeout: 60_000 })
  }
}

const TITLES: Record<string, string> = { cafe_order: '카페에서 주문하기', directions: '길 묻기', hotel_checkin: '호텔 체크인', job_interview: '면접 연습' }
export const scenarioTitle = (id: string) => TITLES[id] ?? id

/** Wait for the next (non-opening) reply after `from` to be generated and fully played. */
export async function nextReply(page: Page, from: number, timeoutMs = 60_000): Promise<Reply> {
  const started = await waitFor(page, from, { kind: 'ws_in', type: 'response.started' }, timeoutMs)
  return waitReply(page, from, started.response_id as string, timeoutMs)
}

export async function waitReply(page: Page, from: number, responseId: string, timeoutMs = 60_000): Promise<Reply> {
  await waitFor(page, from, { kind: 'ws_in', type: 'response.done', response_id: responseId }, timeoutMs)
  // Every segment that got audio must finish playing (completed) before the reply counts as heard.
  await page.waitForFunction(
    ([f, id]) => {
      const log = window.__e2e.log.slice(f as number)
      const segs = new Set(log.filter((e: { kind: string; response_id?: string }) => e.kind === 'ws_audio' && e.response_id === id).map((e: { segment_id?: number }) => String(e.segment_id)))
      const done = new Set(log.filter((e: { kind: string; type?: string; key?: string }) => e.kind === 'play' && e.type === 'completed' && e.key === id).map((e: { seg?: string }) => e.seg))
      const cancelled = log.some((e: { kind: string; type?: string; response_id?: string }) => e.kind === 'ws_in' && e.type === 'response.cancelled' && e.response_id === id)
      return cancelled || [...segs].every((s) => done.has(s))
    },
    [from, responseId] as const,
    { timeout: timeoutMs, polling: 20 },
  )
  const log = await logSince(page, from)
  const mine = log.filter((e) => e.response_id === responseId || e.key === responseId)
  const startedAt = mine.find((e) => e.kind === 'ws_in' && e.type === 'response.started')!.t
  const frames = mine.filter((e) => e.kind === 'ws_audio')
  const plays = mine.filter((e) => e.kind === 'play')
  const gaps: number[] = []
  let lastCompleted: number | null = null
  for (const p of plays) {
    if (p.type === 'started' && lastCompleted !== null) gaps.push(p.t - lastCompleted)
    if (p.type === 'completed') lastCompleted = p.t
  }
  const doneAt = mine.find((e) => e.kind === 'ws_in' && e.type === 'response.done')!.t
  return {
    responseId,
    startedAt,
    firstAudioFrameAt: frames[0]?.t ?? null,
    firstPlayedAt: plays.find((p) => p.type === 'started')?.t ?? null,
    completedAt: lastCompleted ?? doneAt,
    segments: mine.filter((e) => e.kind === 'ws_in' && e.type === 'response.text').map((e) => String(e.text)),
    playGaps: gaps,
  }
}

export interface Turn {
  mic: MicPlay
  turnId: string
  partials: { t: number; text: string }[]
  final: { t: number; text: string }
  reply: Reply
}

/** Speak a fixture as the learner, wait for the final transcript and the full AI reply playback. */
export async function speakTurn(page: Page, fixture: string, timeoutMs = 90_000): Promise<Turn> {
  await page.evaluate((x) => window.__e2e.load(x), fixture)
  const from = await logLen(page)
  const mic = await say(page, fixture)
  const speech = await waitFor(page, from, { kind: 'ws_in', type: 'speech.started' }, 20_000)
  const turnId = speech.turn_id as string
  const final = await waitFor(page, from, { kind: 'ws_in', type: 'asr.final', turn_id: turnId }, 30_000)
  const reply = await nextReply(page, from, timeoutMs)
  const partials = (await logSince(page, from, 'ws_in', 'asr.partial')).filter((e) => e.turn_id === turnId).map((e) => ({ t: e.t, text: String(e.text) }))
  return { mic, turnId, partials, final: { t: final.t, text: String(final.text) }, reply }
}

// ------------------------------------------------------------------ HTTP API (same cookie jar as the page)

export async function api(page: Page): Promise<Api> {
  const req = page.request
  if (!(await page.context().cookies()).some((c) => c.name === 'vr_sid')) await req.get('/') // GET / hands out vr_sid
  const boot = await req.get('/api/bootstrap')
  expect(boot.ok()).toBeTruthy()
  const { csrf_token } = await boot.json()
  return new Api(req, csrf_token)
}

export class Api {
  constructor(
    readonly req: APIRequestContext,
    readonly csrf: string,
  ) {}
  headers(extra: Record<string, string> = {}) {
    return { Origin: BASE_URL, 'X-VR-CSRF': this.csrf, ...extra }
  }
  get(path: string) {
    return this.req.get(path)
  }
  post(path: string, data?: unknown, extra: Record<string, string> = {}) {
    return this.req.post(path, { data: data ?? {}, headers: this.headers(extra) })
  }
  put(path: string, body: Buffer, extra: Record<string, string> = {}) {
    return this.req.put(path, { data: body, headers: this.headers({ 'Content-Type': 'audio/wav', ...extra }) })
  }
  patch(path: string, data: unknown, extra: Record<string, string> = {}) {
    return this.req.patch(path, { data, headers: this.headers(extra) })
  }
  delete(path: string) {
    return this.req.delete(path, { headers: this.headers() })
  }
  async waitJob(jobId: string, timeoutMs = 120_000): Promise<Record<string, unknown>> {
    const deadline = Date.now() + timeoutMs
    for (;;) {
      const j = await (await this.get(`/api/jobs/${jobId}`)).json()
      if (['completed', 'failed', 'cancelled', 'expired'].includes(j.state)) return j
      if (Date.now() > deadline) throw new Error(`job ${jobId} still ${j.state}`)
      await new Promise((r) => setTimeout(r, 200))
    }
  }
}

/**
 * Wait until realtime mode is available. A realtime session left over from the previous test no longer blocks the
 * next one (starting something new ends it, PRD §7 v0.2.1), so it is not an error: it gets up to `settleMs` to end
 * on its own (a spec then starts from a quiet stack), and after that the next start ends it.
 */
export async function waitRealtimeFree(req: APIRequestContext, timeoutMs = 30_000, settleMs = 5_000): Promise<void> {
  const started = Date.now()
  for (;;) {
    const h = await (await req.get('/api/health')).json()
    const now = Date.now()
    if (h.modes.realtime.available && (!h.modes.realtime.session_active || now - started > settleMs)) return
    if (now - started > timeoutMs) throw new Error('realtime mode not available')
    await new Promise((r) => setTimeout(r, 250))
  }
}

// ------------------------------------------------------------------ WAV helpers

export function wav(samples: Int16Array, rate: number, channels = 1): Buffer {
  const data = Buffer.from(samples.buffer, samples.byteOffset, samples.byteLength)
  const h = Buffer.alloc(44)
  h.write('RIFF', 0)
  h.writeUInt32LE(36 + data.length, 4)
  h.write('WAVE', 8)
  h.write('fmt ', 12)
  h.writeUInt32LE(16, 16)
  h.writeUInt16LE(1, 20)
  h.writeUInt16LE(channels, 22)
  h.writeUInt32LE(rate, 24)
  h.writeUInt32LE(rate * channels * 2, 28)
  h.writeUInt16LE(channels * 2, 32)
  h.writeUInt16LE(16, 34)
  h.write('data', 36)
  h.writeUInt32LE(data.length, 40)
  return Buffer.concat([h, data])
}

export const fixtureWav = (name: string) => readFileSync(join(FIXTURES, `${name}.wav`))

// ------------------------------------------------------------------ stack processes

/** Pids of the running stack: gateway + workers from var/run, plus all their descendants. */
export function stackPids(): number[] {
  const run = join(ROOT, 'var/run')
  const roots: number[] = []
  for (const name of ['gateway', 'asr', 'tts', 'llm', 'pron']) {
    const f = join(run, `${name}.pid`)
    if (existsSync(f)) roots.push(Number(readFileSync(f, 'utf8').split(/\s/)[0]))
  }
  const all = new Set<number>(roots)
  const queue = [...roots]
  while (queue.length) {
    const pid = queue.pop()!
    let out = ''
    try {
      out = execFileSync('pgrep', ['-P', String(pid)], { encoding: 'utf8' })
    } catch {
      /* no children */
    }
    for (const c of out.split('\n').filter(Boolean).map(Number)) if (!all.has(c)) (all.add(c), queue.push(c))
  }
  return [...all]
}

const LOOPBACK = /^(127\.\d+\.\d+\.\d+|\[::1\]|::1|localhost|\*)$/

/**
 * Polls `lsof -a -i -nP -p <pids>` every 500 ms. A violation is any socket whose remote end (or listening
 * address) is not loopback. `stop()` returns violations and the number of samples taken.
 */
export function watchNetwork(pids: number[]): { stop(): Promise<{ violations: string[]; samples: number; sockets: number }> } {
  let running = true
  const violations = new Set<string>()
  let samples = 0
  let sockets = 0
  const loop = (async () => {
    while (running) {
      const out = await new Promise<string>((res) => {
        const p = spawn('lsof', ['-a', '-i', '-nP', '-p', pids.join(',')])
        let s = ''
        p.stdout.on('data', (d) => (s += d))
        p.on('close', () => res(s))
      })
      samples++
      for (const line of out.split('\n').slice(1).filter(Boolean)) {
        sockets++
        const name = line.trim().split(/\s+/).slice(8).join(' ') // NAME column (+ state)
        const addr = name.split(' ')[0]!
        const [local, remote] = addr.split('->')
        const host = (s: string) => s.replace(/:\d+$|:\*$/, '')
        if (remote !== undefined) {
          if (!LOOPBACK.test(host(remote))) violations.add(line)
        } else if (/LISTEN/.test(name) && !/^(127\.|\[::1\])/.test(local!)) {
          violations.add(line)
        } else if (/UDP/.test(line) && !/^(127\.|\[::1\])/.test(local!)) {
          violations.add(line)
        }
      }
      await new Promise((r) => setTimeout(r, 500))
    }
  })()
  return {
    async stop() {
      running = false
      await loop
      return { violations: [...violations], samples, sockets }
    },
  }
}

/** Count lines matching `re` in a stack log file (var/log/<name>.log). */
export function countLog(name: string, re: RegExp): number {
  const f = join(ROOT, 'var/log', `${name}.log`)
  if (!existsSync(f)) return 0
  return readFileSync(f, 'utf8').split('\n').filter((l) => re.test(l)).length
}

export function fileSize(path: string): number {
  return existsSync(path) ? statSync(path).size : 0
}

declare global {
  interface Window {
    __e2e: {
      log: LogEntry[]
      realMic: boolean
      capFrames: number
      outFrames: number
      sentFrames?: number
      micReady(): Promise<string>
      load(name: string): Promise<{ duration: number; firstVoiced: number; lastVoiced: number }>
      play(name: string, delayS?: number, gain?: number): MicPlay
      setNoise(dbfs: number | null): void
      since(from: number, kind?: string, type?: string): LogEntry[]
      outputLatencyMs(): number | null
    }
  }
}

/**
 * Raw WebSocket upgrade with exact headers (browsers cannot forge Origin). Returns the HTTP status of the
 * handshake and, if the server accepted and then closed, the close code of its first frame.
 */
export async function wsHandshake(path: string, headers: Record<string, string>): Promise<{ status: number; closeCode: number | null }> {
  const http = await import('node:http')
  const { randomBytes } = await import('node:crypto')
  const u = new URL(path, BASE_URL)
  return new Promise((resolve, reject) => {
    const req = http.request({
      host: u.hostname, port: u.port, path: u.pathname, method: 'GET',
      headers: { Connection: 'Upgrade', Upgrade: 'websocket', 'Sec-WebSocket-Version': '13', 'Sec-WebSocket-Key': randomBytes(16).toString('base64'), ...headers },
    })
    req.on('response', (res) => {
      res.resume()
      resolve({ status: res.statusCode ?? 0, closeCode: null })
    })
    req.on('upgrade', (res, socket) => {
      let buf = Buffer.alloc(0)
      const done = (code: number | null) => {
        socket.destroy()
        resolve({ status: res.statusCode ?? 101, closeCode: code })
      }
      socket.on('data', (d: Buffer) => {
        buf = Buffer.concat([buf, d])
        if (buf.length >= 4 && (buf[0]! & 0x0f) === 0x8) done(buf.readUInt16BE(2))
      })
      socket.on('close', () => done(null))
      setTimeout(() => done(null), 3000)
    })
    req.on('error', reject)
    req.end()
  })
}

/**
 * Polls `lsof -p <pids> -Fn` every 100 ms and collects regular files the stack has open under temp dirs
 * (/tmp, $TMPDIR) — e.g. a request body spooled to disk.
 */
export function watchTempFiles(pids: number[]): { stop(): Promise<{ files: string[]; samples: number }> } {
  let running = true
  const files = new Set<string>()
  let samples = 0
  const loop = (async () => {
    while (running) {
      const out = await new Promise<string>((res) => {
        const p = spawn('lsof', ['-p', pids.join(','), '-Fn'])
        let s = ''
        p.stdout.on('data', (d) => (s += d))
        p.on('close', () => res(s))
      })
      samples++
      for (const line of out.split('\n')) {
        // Temp dirs only (/tmp and $TMPDIR = /var/folders/<x>/<y>/T); the OS-managed caches next to it (C/: Metal
        // shader caches, 0/: LaunchServices) hold no user data.
        if (line.startsWith('n') && /^n(\/private)?(\/tmp\/|\/var\/folders\/[^/]+\/[^/]+\/T\/)/.test(line)) files.add(line.slice(1))
      }
      await new Promise((r) => setTimeout(r, 100))
    }
  })()
  return {
    async stop() {
      running = false
      await loop
      return { files: [...files], samples }
    },
  }
}
