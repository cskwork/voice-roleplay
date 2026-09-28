// Full-stack benchmark through the real browser path (PRD §15.1, §15.3, AT-14). Run by benchmarks/run.sh.
// FAKE-MIC-SOURCE: learner turns are macOS `say` fixtures fed through a MediaStream into the app's capture
// worklet (lib/fakeMic.js). All realtime timings use the browser's performance.now() clock:
//   - "last voiced sample" = scheduled time of the fixture's last voiced 10 ms window in the fake-mic context;
//   - "first AI audio played" = the app's vr-playback worklet reporting `started` for the reply (render
//     thread); the context's base+output latency is recorded separately (the speaker adds it on top).
import { execFileSync } from 'node:child_process'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'
import { test } from '@playwright/test'
import {
  api, endConversation, FIXTURES, logLen, logSince, nextReply, preparePage, say, speakTurn, stackPids, startConversation,
  waitFor, waitRealtimeFree, waitUntilPerf, wav, type Reply,
} from '../lib/harness'

const N = Number(process.env.BENCH_TURNS ?? 30)
const BARGE_EVERY = Number(process.env.BENCH_BARGE_EVERY ?? 3)
const SESSION_TURNS = Number(process.env.BENCH_SESSION_TURNS ?? 10)
const REC30 = Number(process.env.BENCH_REC30 ?? 5)
const REC120 = Number(process.env.BENCH_REC120 ?? 2)
const OUT = process.env.BENCH_OUT ?? join(process.cwd(), '.out/bench-raw.json')
const POOL = Array.from({ length: 12 }, (_, i) => `e2e_cafe_${String(i + 1).padStart(2, '0')}`)

/** Silence stalls inside a reply: wall time between first start and last completion minus audio played. */
function stallMs(log: { kind: string; type?: string; key?: unknown; t: number; playedMs?: unknown }[], id: string): number | null {
  const plays = log.filter((e) => e.kind === 'play' && e.key === id)
  const first = plays.find((e) => e.type === 'started')
  const done = plays.filter((e) => e.type === 'completed')
  if (!first || !done.length) return null
  const played = done.reduce((s, e) => s + Number(e.playedMs ?? 0), 0)
  return Math.max(0, done.at(-1)!.t - first.t - played)
}

function pcmOf(file: string): Int16Array {
  const b = readFileSync(join(FIXTURES, file))
  let pos = 12
  while (pos + 8 <= b.length) {
    const id = b.toString('ascii', pos, pos + 4)
    const size = b.readUInt32LE(pos + 4)
    if (id === 'data') return new Int16Array(b.buffer.slice(b.byteOffset + pos + 8, b.byteOffset + pos + 8 + size))
    pos += 8 + size + (size & 1)
  }
  throw new Error('no data chunk')
}

function padTo(pcm: Int16Array, seconds: number, repeat = 1): Int16Array {
  const out = new Int16Array(Math.round(seconds * 16000))
  let o = 0
  for (let r = 0; r < repeat && o < out.length; r++) {
    out.set(pcm.subarray(0, Math.min(pcm.length, out.length - o)), o)
    o += pcm.length
  }
  return out
}

function rss(pids: number[]): Record<number, number> {
  const out: Record<number, number> = {}
  try {
    for (const line of execFileSync('ps', ['-o', 'pid=,rss=', '-p', pids.join(',')], { encoding: 'utf8' }).split('\n').filter(Boolean)) {
      const [pid, kb] = line.trim().split(/\s+/).map(Number)
      out[pid!] = kb! / 1024
    }
  } catch {
    /* process gone */
  }
  return out
}

/** macOS phys_footprint (what Activity Monitor calls "Memory"; includes Metal/GPU buffers RSS misses), MB. */
function footprint(pids: number[]): Record<number, { mb: number; peak_mb: number }> {
  const out: Record<number, { mb: number; peak_mb: number }> = {}
  try {
    const text = execFileSync('footprint', ['--noCategories', '-f', 'bytes', ...pids.flatMap((p) => ['-p', String(p)])], { encoding: 'utf8' })
    for (const m of text.matchAll(/\[(\d+)\]: [\s\S]*?phys_footprint: (\d+) B\s+phys_footprint_peak: (\d+) B/g)) {
      out[Number(m[1])] = { mb: Number(m[2]) / 2 ** 20, peak_mb: Number(m[3]) / 2 ** 20 }
    }
  } catch {
    /* tool missing or process gone */
  }
  return out
}

test('full-stack benchmark', async ({ page, browser }) => {
  test.setTimeout(4 * 60 * 60 * 1000)
  await preparePage(page)
  await waitRealtimeFree(page.request)
  const pids = stackPids()
  const names = ['gateway', 'asr', 'tts', 'llm', 'pron']
  const memory: { t: number; rss: Record<number, number>; footprint?: ReturnType<typeof footprint> }[] = [
    { t: Date.now(), rss: rss(pids), footprint: footprint(pids) }]
  let tick = 0
  const sampler = setInterval(() => memory.push({ t: Date.now(), rss: rss(pids), ...(++tick % 6 === 0 ? { footprint: footprint(pids) } : {}) }), 5000)

  const turns: Record<string, unknown>[] = []
  const bargeIns: Record<string, unknown>[] = []
  const postBarge: Record<string, unknown>[] = []
  const openings: Record<string, unknown>[] = []
  let warmup: Record<string, unknown> | null = null
  const turnRecord = (i: number, session: number, fixture: string, mic: { firstVoicedPerf: number; lastVoicedPerf: number },
    partials: { t: number }[], finalT: number, reply: Reply, log: Parameters<typeof stallMs>[0]) => ({
    i, session, fixture, response_id: reply.responseId,
    last_voiced_to_first_played_ms: reply.firstPlayedAt == null ? null : reply.firstPlayedAt - mic.lastVoicedPerf,
    speech_start_to_first_partial_ms: partials.length ? partials[0]!.t - mic.firstVoicedPerf : null,
    partials: partials.length,
    last_voiced_to_final_ms: finalT - mic.lastVoicedPerf,
    final_to_response_started_ms: reply.startedAt - finalT,
    response_started_to_first_frame_ms: reply.firstAudioFrameAt == null ? null : reply.firstAudioFrameAt - reply.startedAt,
    first_frame_to_played_ms: reply.firstPlayedAt == null || reply.firstAudioFrameAt == null ? null : reply.firstPlayedAt - reply.firstAudioFrameAt,
    segments: reply.segments.length,
    words: reply.segments.join(' ').split(/\s+/).filter(Boolean).length,
    segment_gaps_ms: reply.playGaps,
    stall_ms: stallMs(log, reply.responseId),
  })

  let session = 0
  let inSession = SESSION_TURNS
  for (let i = 0; i < N; i++) {
    if (inSession >= SESSION_TURNS) {
      if (session > 0) {
        await endConversation(page)
        await waitRealtimeFree(page.request, 60_000)
      }
      session++
      inSession = 0
      const o = await startConversation(page)
      openings.push({ session, first_frame_to_played_ms: o.firstPlayedAt && o.firstAudioFrameAt ? o.firstPlayedAt - o.firstAudioFrameAt : null })
      if (!warmup) {
        const w = await speakTurn(page, 'e2e_cafe_01')
        warmup = { last_voiced_to_first_played_ms: w.reply.firstPlayedAt! - w.mic.lastVoicedPerf }
      }
    }
    inSession++
    const fixture = POOL[i % POOL.length]!
    const barge = BARGE_EVERY > 0 && (i + 1) % BARGE_EVERY === 0
    const from = await logLen(page)
    if (!barge) {
      const t = await speakTurn(page, fixture)
      turns.push(turnRecord(i, session, fixture, t.mic, t.partials, t.final.t, t.reply, await logSince(page, from)))
    } else {
      // The reply to this turn is interrupted 600 ms into its playback.
      const mic = await say(page, fixture)
      const speech = await waitFor(page, from, { kind: 'ws_in', type: 'speech.started' }, 20_000)
      const final = await waitFor(page, from, { kind: 'ws_in', type: 'asr.final', turn_id: speech.turn_id }, 30_000)
      const started = await waitFor(page, from, { kind: 'ws_in', type: 'response.started' }, 60_000)
      const rid = started.response_id as string
      const played = await waitFor(page, from, { kind: 'play', type: 'started', key: rid }, 60_000)
      await waitUntilPerf(page, played.t + 600)
      const at = await logLen(page)
      const bmic = await say(page, 'e2e_barge')
      const flushed = await waitFor(page, at, { kind: 'play', type: 'flushed' }, 10_000)
      const reply2 = await nextReply(page, at, 90_000)
      const log = await logSince(page, from)
      const partials = log.filter((e) => e.kind === 'ws_in' && e.type === 'asr.partial' && e.turn_id === speech.turn_id).map((e) => ({ t: e.t }))
      const frames = log.filter((e) => e.kind === 'ws_audio' && e.response_id === rid)
      const reply: Reply = { responseId: rid, startedAt: started.t, firstAudioFrameAt: frames[0]?.t ?? null, firstPlayedAt: played.t,
        completedAt: flushed.t, segments: log.filter((e) => e.kind === 'ws_in' && e.type === 'response.text' && e.response_id === rid).map((e) => String(e.text)), playGaps: [] }
      turns.push({ ...turnRecord(i, session, fixture, mic, partials, final.t, reply, []), interrupted: true, stall_ms: null })
      const cancelled = log.find((e) => e.kind === 'ws_in' && e.type === 'response.cancelled' && e.response_id === rid && e.t > at)
      const late = log.filter((e) => e.kind === 'play' && e.type === 'started' && e.key === rid && e.t > flushed.t).length
      bargeIns.push({ i, speech_start_to_local_stop_ms: flushed.t - bmic.firstVoicedPerf,
        speech_start_to_server_cancel_ms: cancelled ? cancelled.t - bmic.firstVoicedPerf : null, stale_playback_after_stop: late })
      postBarge.push({ i, last_voiced_to_first_played_ms: reply2.firstPlayedAt == null ? null : reply2.firstPlayedAt - bmic.lastVoicedPerf })
    }
  }
  const outputLatencyMs = await page.evaluate(() => window.__e2e.outputLatencyMs())
  const contextRate = await page.evaluate(() => (window.__e2e as unknown as { appCtx?: AudioContext }).appCtx?.sampleRate ?? null)
  await endConversation(page)
  await waitRealtimeFree(page.request, 60_000)

  // Recorded practice: free answer (ASR + LLM feedback + model audio), 30 s and 120 s inputs, one at a time.
  const a = await api(page)
  const sc = await (await a.get('/api/scenarios/cafe_order')).json()
  const exerciseId = sc.exercises.free_answer[0].exercise_id
  const passage = pcmOf('passage_30s.wav')
  const recorded: Record<string, unknown>[] = []
  for (const [seconds, reps, repeat] of [[30, REC30, 1], [120, REC120, 4]] as const) {
    const body = wav(padTo(passage, seconds, repeat), 16000)
    for (let r = 0; r < reps; r++) {
      const id = (await (await a.post('/api/attempts', { exercise_type: 'free_answer', scenario_id: 'cafe_order', exercise_id: exerciseId, history_opt_in: false })).json()).attempt_id
      const u0 = Date.now()
      await a.put(`/api/attempts/${id}/audio`, body)
      const u1 = Date.now()
      const job = await (await a.post(`/api/attempts/${id}/submit`, {}, { 'Idempotency-Key': `bench-${seconds}-${r}-${u0}` })).json()
      const t0 = Date.now()
      let transcribed: number | null = null
      let state = job.state
      while (!['completed', 'failed', 'cancelled', 'expired'].includes(state)) {
        await new Promise((res) => setTimeout(res, 100))
        state = (await (await a.get(`/api/jobs/${job.job_id}`)).json()).state
        if (transcribed == null && ['analyzing', 'synthesizing', 'completed'].includes(state)) transcribed = Date.now()
      }
      const done = Date.now()
      const res = await (await a.get(`/api/attempts/${id}/result`)).json()
      recorded.push({ input_s: seconds, rep: r, state, upload_ms: u1 - u0, submit_to_transcript_ms: transcribed == null ? null : transcribed - t0,
        submit_to_full_result_ms: done - t0, feedback_items: res.feedback?.length ?? 0, feedback_status: res.feedback_status ?? null,
        model_audio: res.model_audio?.length ?? 0, transcript_words: String(res.transcript ?? '').split(/\s+/).filter(Boolean).length,
        attempt_id: id, pronunciation_status: res.pronunciation?.status ?? null, pronunciation_reason: res.pronunciation?.reason ?? null,
        pronunciation_words: res.pronunciation?.words?.length ?? 0, pronunciation_score: res.pronunciation_score ?? null,
        prosody_learner_frames: res.pronunciation?.prosody?.learner?.f0_hz?.length ?? 0 })
    }
  }
  clearInterval(sampler)
  memory.push({ t: Date.now(), rss: rss(pids), footprint: footprint(pids) })
  const pidNames = Object.fromEntries(pids.slice(0, names.length).map((p, k) => [p, names[k]!]))
  writeFileSync(OUT, JSON.stringify({
    started_at: new Date(memory[0]!.t).toISOString(), finished_at: new Date().toISOString(),
    config: { turns: N, barge_every: BARGE_EVERY, session_turns: SESSION_TURNS, rec30: REC30, rec120: REC120 },
    browser: { name: 'chromium', version: browser.version(), headless: true, context_sample_rate: contextRate, output_latency_ms: outputLatencyMs },
    pids: pidNames, warmup, openings, turns, barge_ins: bargeIns, post_barge_turns: postBarge, recorded, memory,
  }, null, 1))
})
