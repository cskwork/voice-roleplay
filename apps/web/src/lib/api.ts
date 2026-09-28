import { ApiError, messageFor } from './errors'
import type {
  AttemptResult,
  Difficulty,
  ExerciseType,
  Feedback,
  GoalState,
  Health,
  HistoryEntry,
  Job,
  ReviewItem,
  Scenario,
  SessionSummary,
  Settings,
} from './types'

let csrfToken: string | null = null
let bootstrapping: Promise<string> | null = null

async function readError(res: Response): Promise<ApiError> {
  try {
    const body = (await res.json()) as { error?: { code?: string; message_ko?: string } }
    const code = body.error?.code ?? `HTTP_${res.status}`
    return new ApiError(code, messageFor(code, body.error?.message_ko), res.status)
  } catch {
    // No JSON envelope: a proxy/gateway that is down (502/503/504) or an unexpected server error.
    const code = res.status >= 502 && res.status <= 504 ? 'NETWORK' : `HTTP_${res.status}`
    return new ApiError(code, messageFor(code), res.status)
  }
}

async function send(input: string, init: RequestInit = {}): Promise<Response> {
  try {
    return await fetch(input, { credentials: 'same-origin', ...init })
  } catch {
    throw new ApiError('NETWORK', messageFor('NETWORK'))
  }
}

/** Fetches the CSRF token. `GET /` normally set the vr_sid cookie already; the dev server needs a nudge. */
export function bootstrap(force = false): Promise<string> {
  if (csrfToken && !force) return Promise.resolve(csrfToken)
  bootstrapping ??= (async () => {
    let res = await send('/api/bootstrap')
    if (res.status === 401 && import.meta.env.DEV) {
      await send('/__vr_init')
      res = await send('/api/bootstrap')
    }
    if (!res.ok) throw await readError(res)
    const body = (await res.json()) as { csrf_token: string }
    csrfToken = body.csrf_token
    return csrfToken
  })().finally(() => {
    bootstrapping = null
  })
  return bootstrapping
}

async function request(method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<Response> {
  const build = async (): Promise<RequestInit> => {
    const h: Record<string, string> = { ...headers }
    if (method !== 'GET') h['X-VR-CSRF'] = await bootstrap()
    let payload: BodyInit | undefined
    if (body instanceof ArrayBuffer || body instanceof Blob) {
      payload = body
    } else if (body !== undefined) {
      h['Content-Type'] = 'application/json'
      payload = JSON.stringify(body)
    }
    return { method, headers: h, body: payload }
  }
  let res = await send(path, await build())
  if (res.status === 403 && method !== 'GET') {
    const err = await readError(res.clone())
    if (err.code === 'CSRF_INVALID') {
      // Gateway restarted and rotated the token: refresh once.
      await bootstrap(true)
      res = await send(path, await build())
    }
  }
  if (!res.ok) throw await readError(res)
  return res
}

async function json<T>(method: string, path: string, body?: unknown, headers?: Record<string, string>): Promise<T> {
  const res = await request(method, path, body, headers)
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

function listOf<T>(body: unknown, key: string): T[] {
  if (Array.isArray(body)) return body as T[]
  const v = (body as Record<string, unknown> | null)?.[key]
  return Array.isArray(v) ? (v as T[]) : []
}

type Obj = Record<string, unknown>
const obj = (v: unknown): Obj => (v && typeof v === 'object' ? (v as Obj) : {})

export const api = {
  health: async () => normalizeHealth(await json<unknown>('GET', '/api/health')),
  scenarios: async () => listOf<Scenario>(await json<unknown>('GET', '/api/scenarios'), 'scenarios'),
  settings: () => json<Settings>('GET', '/api/settings'),
  saveSettings: (s: Partial<Settings>) => json<Settings>('PUT', '/api/settings', s),

  createSession: (body: { mode: 'realtime' | 'turn_based'; scenario_id: string; difficulty: Difficulty; history_opt_in: boolean; feedback_policy: 'session_end' }) =>
    json<{ session_id: string }>('POST', '/api/sessions', body),
  endSession: async (id: string): Promise<SessionSummary> =>
    normalizeSummary(await json<unknown>('POST', `/api/sessions/${encodeURIComponent(id)}/end`, {})),

  createAttempt: (body: {
    exercise_type: ExerciseType
    /** Required except for drill (a saved review expression may have no scenario). */
    scenario_id?: string
    history_opt_in: boolean
    text_id?: string
    exercise_id?: string
    session_id?: string
    /** drill only: the English sentence to say again (≤ 400 chars); handled like reading, never sent to ASR. */
    target_text?: string
  }) => json<{ attempt_id: string; attempt_index?: number }>('POST', '/api/attempts', body),
  uploadAudio: (id: string, wav: ArrayBuffer) =>
    json<unknown>('PUT', `/api/attempts/${encodeURIComponent(id)}/audio`, new Blob([wav], { type: 'audio/wav' })),
  submitAttempt: (id: string, idempotencyKey: string) =>
    json<Job>('POST', `/api/attempts/${encodeURIComponent(id)}/submit`, {}, { 'Idempotency-Key': idempotencyKey }),
  job: (id: string) => json<Job>('GET', `/api/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id: string) => json<Job>('DELETE', `/api/jobs/${encodeURIComponent(id)}`),
  result: (id: string) => json<AttemptResult>('GET', `/api/attempts/${encodeURIComponent(id)}/result`),
  editTranscript: (id: string, text: string) => json<Job>('PATCH', `/api/attempts/${encodeURIComponent(id)}/transcript`, { text }),

  history: async () => flattenHistory(await json<unknown>('GET', '/api/history')),
  deleteHistory: () => json<unknown>('DELETE', '/api/history'),
  reviewDue: async () => listOf<ReviewItem>(await json<unknown>('GET', '/api/review/due'), 'items'),
  gradeReview: (id: string, result: 'again' | 'good') => json<unknown>('POST', `/api/review/${encodeURIComponent(id)}/grade`, { result }),

  cachedTts: async (voiceId: string, textId: string, slow = false) =>
    (await request('GET', `/api/tts/cached?voice_id=${encodeURIComponent(voiceId)}&text_id=${encodeURIComponent(textId)}${slow ? '&slow=true' : ''}`)).arrayBuffer(),
  tts: async (voiceId: string, text: string, speed = 1.0) => (await request('POST', '/api/tts', { voice_id: voiceId, text, speed })).arrayBuffer(),
  /** Same-origin model audio URL returned inside an attempt result. */
  audioUrl: async (url: string) => {
    if (!url.startsWith('/api/')) throw new ApiError('NOT_FOUND', messageFor('NOT_FOUND'))
    return (await request('GET', url)).arrayBuffer()
  },
}

export function normalizeHealth(raw: unknown): Health {
  const b = obj(raw)
  const workers = obj(b.workers)
  const components: Health['components'] = {}
  for (const k of ['asr', 'tts', 'llm', 'vad'] as const) {
    const c = obj(workers[k] ?? b[k] ?? obj(b.components)[k])
    if (Object.keys(c).length) components[k] = { ...c, ready: c.ready === true } as Health['components'][typeof k]
  }
  const gw = obj(b.gateway)
  components.gateway = { ready: Object.keys(gw).length ? gw.ready !== false : true }
  const modes = obj(b.modes)
  const mode = (k: string) => {
    const m = obj(modes[k])
    return { ...m, available: m.available === true, reason_ko: (m.reason_ko as string | null | undefined) ?? undefined }
  }
  return {
    ready: (['asr', 'tts', 'llm', 'vad'] as const).every((k) => components[k]?.ready),
    components,
    modes: { realtime: mode('realtime'), recorded: mode('recorded') },
    benchmark: (b.benchmark as Health['benchmark']) ?? null,
  }
}

export function normalizeSummary(raw: unknown): SessionSummary {
  const body = obj(raw)
  const s = body.summary ? obj(body.summary) : body
  const items = Array.isArray(s.items) ? s.items : Array.isArray(s.improvements) ? s.improvements : []
  return {
    improvements: (items as Feedback[]).slice(0, 3),
    goals: Array.isArray(s.goals) ? (s.goals as GoalState[]) : [],
    status: typeof s.status === 'string' ? s.status : 'ok',
    reason: typeof s.reason === 'string' ? s.reason : null,
  }
}

function flattenHistory(raw: unknown): HistoryEntry[] {
  const b = obj(raw)
  const out: HistoryEntry[] = []
  for (const s of listOf<Obj>(b, 'sessions')) {
    const turns = listOf<Obj>(s, 'turns').filter((t) => t.role === 'user')
    out.push({
      kind: 'session',
      id: String(s.session_id),
      scenario_id: s.scenario_id as string | undefined,
      created_at: Number(s.created_at),
      text: turns.map((t) => String(t.text ?? '')).join(' / '),
      feedback_count: listOf<unknown>(obj(s.summary), 'items').length,
    })
  }
  for (const a of listOf<Obj>(b, 'attempts')) {
    const revs = listOf<Obj>(a, 'revisions')
    out.push({
      kind: 'attempt',
      id: String(a.attempt_id),
      scenario_id: a.scenario_id as string | undefined,
      exercise_type: a.exercise_type as string | undefined,
      created_at: Number(a.created_at),
      text: String(revs.at(-1)?.text ?? ''),
      feedback_count: listOf<unknown>(a, 'feedback').length,
    })
  }
  return out.sort((x, y) => y.created_at - x.created_at)
}

export function newId(): string {
  return crypto.randomUUID()
}
