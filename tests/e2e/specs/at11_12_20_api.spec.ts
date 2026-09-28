// AT-11 invalid / too-long WAV, AT-12 44.1 kHz stereo and 48 kHz uploads, AT-20 origin/auth/frame limits
// (PRD §16, §18). Real stack; HTTP through the page's cookie jar, WebSocket checks through raw upgrades.
import { execFileSync } from 'node:child_process'
import { readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { expect, request, test } from '@playwright/test'
import { api, BASE_URL, watchTempFiles, endConversation, fixtureText, fixtureWav, logLen, logSince, nextReply, preparePage, ROOT, say, stackPids, startConversation, waitFor, waitRealtimeFree, wav, wsHandshake } from '../lib/harness'

/** Files (recursive, bounded) under `dir` modified at or after `since` (ms epoch). */
function newFiles(dir: string, since: number, depth = 3): string[] {
  const out: string[] = []
  let entries: string[] = []
  try {
    entries = readdirSync(dir)
  } catch {
    return out
  }
  for (const name of entries) {
    const p = join(dir, name)
    let st
    try {
      st = statSync(p)
    } catch {
      continue
    }
    if (st.isDirectory()) {
      if (depth > 0) out.push(...newFiles(p, since, depth - 1))
    } else if (st.mtimeMs >= since) out.push(p)
  }
  return out
}

function rssMb(pid: number): number {
  return Number(execFileSync('ps', ['-o', 'rss=', '-p', String(pid)], { encoding: 'utf8' }).trim()) / 1024
}

test('AT-11 WAV validation: garbage / 8-bit / 22.05 kHz / >120 s / >32 MiB rejected, 120 s accepted, no temp files', async ({ page }) => {
  await preparePage(page)
  await page.goto('/#/home')
  await waitRealtimeFree(page.request)
  const a = await api(page)
  const gatewayPid = stackPids()[0]!
  const rss0 = rssMb(gatewayPid)
  const since = Date.now() - 1000
  const tempWatch = watchTempFiles(stackPids())
  const attempt = async () => (await (await a.post('/api/attempts', { exercise_type: 'drill', target_text: 'I would like a medium latte, please.' })).json()).attempt_id as string

  const cases: [string, Buffer, number, string][] = []
  cases.push(['garbage', Buffer.from('this is not a wav file at all'.repeat(100)), 422, 'AUDIO_INVALID'])
  const pcm8 = wav(new Int16Array(16000), 16000)
  pcm8.writeUInt16LE(8, 34) // bits per sample 8 (block align/byte rate now inconsistent too)
  cases.push(['8-bit', pcm8, 422, 'AUDIO_INVALID'])
  cases.push(['22.05 kHz', wav(new Int16Array(22050), 22050), 422, 'AUDIO_INVALID'])
  cases.push(['truncated data chunk', wav(new Int16Array(16000), 16000).subarray(0, 1000), 422, 'AUDIO_INVALID'])
  cases.push(['120.5 s', wav(new Int16Array(16000 * 120.5), 16000), 413, 'AUDIO_TOO_LONG'])
  cases.push(['33 MiB', Buffer.alloc(33 * 1024 * 1024, 1), 413, 'AUDIO_TOO_LARGE'])
  for (const [name, body, status, code] of cases) {
    const res = await a.put(`/api/attempts/${await attempt()}/audio`, body)
    expect(res.status(), name).toBe(status)
    const err = await res.json()
    expect(err.error.code, name).toBe(code)
    expect(JSON.stringify(err), 'no local paths in errors').not.toMatch(/\/Users\/|models\/|\.py\b|Traceback/)
  }
  // Exactly 120 s (low-level noise so it is not digital silence) is accepted.
  const n = 16000 * 120
  const noise = new Int16Array(n)
  for (let i = 0; i < n; i++) noise[i] = Math.round((Math.random() * 2 - 1) * 30)
  const ok = await a.put(`/api/attempts/${await attempt()}/audio`, wav(noise, 16000))
  expect(ok.status()).toBe(200)
  expect((await ok.json()).duration_ms).toBe(120_000)

  // Nothing was spooled to disk: no stack process had a file open under the temp dirs during the uploads, and no
  // new file appeared in var/ (except the SQLite WAL, logs and service caches). /tmp and $TMPDIR are shared
  // with every other program on the machine, so new files there are not attributable; open files are.
  const temp = await tempWatch.stop()
  expect(temp.samples).toBeGreaterThan(0) // best effort: the uploads take well under a second on loopback
  // onnxruntime (gateway VAD, TTS tokenizer) opens an empty `$TMPDIR/mat-debug-<pid>.log` when imported on macOS;
  // allowed only while it stays empty.
  const unexpected = temp.files.filter((f) => !(/\/T\/mat-debug-\d+\.log$/.test(f) && statSync(f).size === 0))
  expect(unexpected, 'temp files open in stack processes').toEqual([])
  const created = newFiles(join(ROOT, 'var'), since).filter((p) => !/var\/log\/|app\.sqlite3(-wal|-shm)?$|var\/cache\/(tts|model_hashes)|var\/run\//.test(p))
  expect(created).toEqual([])
  const rss1 = rssMb(gatewayPid)
  console.log(`gateway RSS ${rss0.toFixed(0)} MB -> ${rss1.toFixed(0)} MB after the uploads`)
  expect(rss1 - rss0, 'gateway memory after 33 MiB + 120 s uploads (MB)').toBeLessThan(300)
})

test('AT-12 16 kHz / 44.1 kHz stereo / 48 kHz uploads give the same length and transcript', async ({ page }) => {
  await preparePage(page)
  await page.goto('/#/home')
  await waitRealtimeFree(page.request)
  const a = await api(page)
  const target = fixtureText('short_answer')
  const results: Record<string, { info: Record<string, number>; transcript: string; span: number }> = {}
  for (const name of ['short_answer', 'short_answer_44k_stereo', 'short_answer_48k']) {
    const id = (await (await a.post('/api/attempts', { exercise_type: 'drill', target_text: target })).json()).attempt_id
    const up = await a.put(`/api/attempts/${id}/audio`, fixtureWav(name))
    expect(up.status(), name).toBe(200)
    const info = await up.json()
    const job = await (await a.post(`/api/attempts/${id}/submit`, {}, { 'Idempotency-Key': `at12-${name}-${Date.now()}` })).json()
    const done = await a.waitJob(job.job_id)
    expect(done.state, name).toBe('completed')
    const r = await (await a.get(`/api/attempts/${id}/result`)).json()
    results[name] = { info, transcript: r.transcript, span: r.metrics?.speech_span_s }
  }
  console.log(JSON.stringify(Object.fromEntries(Object.entries(results).map(([k, v]) => [k, { ...v.info, span: v.span }]))))
  const base = results.short_answer!
  expect(results.short_answer_44k_stereo!.info.source_rate).toBe(44100)
  expect(results.short_answer_44k_stereo!.info.source_channels).toBe(2)
  expect(results.short_answer_48k!.info.source_rate).toBe(48000)
  for (const r of Object.values(results)) {
    // Correct length after resampling: same number of 16 kHz samples (± 2 ms) and duration.
    expect(Math.abs(r.info.samples_16k - base.info.samples_16k)).toBeLessThanOrEqual(32)
    expect(Math.abs(r.info.duration_ms - base.info.duration_ms)).toBeLessThanOrEqual(2)
    // Correct pitch/speed: the ASR hears the same sentence and the voiced span matches.
    expect(r.transcript.toLowerCase().replace(/[^a-z ]/g, '')).toBe(base.transcript.toLowerCase().replace(/[^a-z ]/g, ''))
    expect(Math.abs(r.span - base.span)).toBeLessThanOrEqual(0.1)
  }
  expect(base.transcript.toLowerCase()).toContain('medium latte')
})

test('AT-20 bad Origin / Host, missing auth or CSRF, oversize frames are rejected without leaking paths', async ({ page, browser }) => {
  await preparePage(page)
  await page.goto('/#/home')
  await waitRealtimeFree(page.request)
  const a = await api(page)
  const cookie = (await page.context().cookies()).find((c) => c.name === 'vr_sid')!
  const bodies: string[] = []

  // HTTP: no cookie -> 401; bad Origin -> 403; no CSRF -> 403; foreign Host (DNS rebinding) -> 403.
  const anon = await request.newContext({ baseURL: BASE_URL })
  let r = await anon.post('/api/attempts', { data: { exercise_type: 'drill', target_text: 'Hello there.' }, headers: { Origin: BASE_URL } })
  expect(r.status()).toBe(401)
  bodies.push(await r.text())
  r = await anon.get('/api/settings')
  expect(r.status()).toBe(401)
  await anon.dispose()
  r = await a.req.post('/api/attempts', { data: { exercise_type: 'drill', target_text: 'Hello there.' }, headers: { Origin: 'http://evil.example', 'X-VR-CSRF': a.csrf } })
  expect(r.status()).toBe(403)
  expect((await r.json()).error.code).toBe('ORIGIN_DENIED')
  bodies.push(await r.text())
  r = await a.req.post('/api/attempts', { data: { exercise_type: 'drill', target_text: 'Hello there.' }, headers: { Origin: BASE_URL } })
  expect(r.status()).toBe(403)
  expect((await r.json()).error.code).toBe('CSRF_INVALID')
  r = await a.req.get('/api/settings', { headers: { Host: 'evil.example:8710' } })
  expect(r.status()).toBe(403)
  bodies.push(await r.text())
  r = await a.req.post('/api/sessions', { data: { mode: 'realtime', scenario_id: '../../etc/passwd' }, headers: a.headers() })
  expect([404, 422]).toContain(r.status())
  bodies.push(await r.text())
  r = await a.req.post('/api/attempts', { data: { exercise_type: 'drill', target_text: 'x'.repeat(300 * 1024) }, headers: a.headers() })
  expect(r.status()).toBe(413)
  bodies.push(await r.text())
  bodies.push(await (await a.get('/api/health')).text())
  for (const b of bodies) expect(b, 'no local file or model paths').not.toMatch(/\/Users\/|\/home\/|models\/|\.gguf|\.safetensors|\.py\b|Traceback/)

  // WebSocket upgrades: foreign Origin, missing Origin, no cookie, unknown session.
  const sess = await a.post('/api/sessions', { mode: 'realtime', scenario_id: 'cafe_order', difficulty: 'normal', history_opt_in: false, feedback_policy: 'session_end' })
  expect(sess.status()).toBe(201)
  const sid = (await sess.json()).session_id
  const wsPath = `/api/sessions/${sid}/realtime`
  const ck = `vr_sid=${cookie.value}`
  const badOrigin = await wsHandshake(wsPath, { Origin: 'http://evil.example', Cookie: ck })
  const noOrigin = await wsHandshake(wsPath, { Cookie: ck })
  const noCookie = await wsHandshake(wsPath, { Origin: BASE_URL })
  const unknown = await wsHandshake('/api/sessions/s_does_not_exist/realtime', { Origin: BASE_URL, Cookie: ck })
  console.log(JSON.stringify({ badOrigin, noOrigin, noCookie, unknown }))
  for (const x of [badOrigin, noOrigin, noCookie]) expect(x.status === 403 || x.closeCode === 4403 || x.closeCode === 4401, JSON.stringify(x)).toBeTruthy()
  expect(unknown.closeCode === 4404 || unknown.status === 403, JSON.stringify(unknown)).toBeTruthy()
  await a.post(`/api/sessions/${sid}/end`)

  // Oversize / malformed frames on a live session: FRAME_INVALID, dropped, and the conversation continues.
  await waitRealtimeFree(page.request)
  await startConversation(page)
  const from = await logLen(page)
  await page.evaluate(() => {
    const ws = (window.__e2e as unknown as { ws: WebSocket }).ws
    const header = (count: number) => new TextEncoder().encode(JSON.stringify({ v: 1, kind: 'input_audio', session_id: location.hash, seq: 1e9, sample_rate: 16000, sample_count: count }))
    const frame = (h: Uint8Array, payload: number) => {
      const b = new Uint8Array(4 + h.length + payload)
      new DataView(b.buffer).setUint32(0, h.length, true)
      b.set(h, 4)
      return b
    }
    ws.send(frame(header(40000), 80000)) // payload > 64 KiB (and wrong session)
    ws.send(frame(new TextEncoder().encode('x'.repeat(2000)), 640)) // header > 1 KiB
    ws.send(new Uint8Array([1, 2])) // too short
  })
  await waitFor(page, from, { kind: 'ws_in', type: 'error', code: 'FRAME_INVALID' }, 5_000)
  expect((await logSince(page, from, 'ws_in', 'error')).filter((e) => e.code === 'FRAME_INVALID').length).toBe(3)
  const at = await logLen(page)
  await say(page, 'e2e_cafe_01')
  const reply = await nextReply(page, at)
  expect(reply.firstPlayedAt).not.toBeNull()
  await endConversation(page)
})
