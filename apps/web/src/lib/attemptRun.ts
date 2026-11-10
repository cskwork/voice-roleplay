import { messageFor, toApiError } from './errors'
import { TERMINAL_JOB_STATES, type AttemptResult, type Job } from './types'
import type { api } from './api'
import type { Take } from '../hooks/audio'

type AttemptBody = Parameters<typeof api.createAttempt>[0]
type AttemptApi = Pick<typeof api, 'createAttempt' | 'uploadAudio' | 'submitAttempt' | 'job' | 'result' | 'cancelJob'>
export interface AttemptState {
  phase: 'idle' | 'uploading' | 'waiting' | 'done'
  job: Job | null
  endState: Job['state'] | null
  error: string | null
}
const RENEW_ATTEMPT = new Set(['AUDIO_EXPIRED', 'NOT_FOUND', 'INVALID_STATE', 'IDEMPOTENCY_CONFLICT'])
const initial = (): AttemptState => ({ phase: 'idle', job: null, endState: null, error: null })

/** One screen's recording transaction. Every asynchronous boundary belongs to a generation. */
export class AttemptRun {
  state = initial()
  private generation = 0
  private timer: ReturnType<typeof setTimeout> | undefined
  private uploaded = new Map<string, { attemptId: string; jobId?: string }>()
  private jobId: string | null = null
  private disposed = false
  constructor(private api: AttemptApi, private changed: (s: AttemptState) => void, private completed: (r: AttemptResult) => void, private remember: (id: string, take: Take) => void) {}
  private emit(patch: Partial<AttemptState>) {
    this.state = { ...this.state, ...patch }
    this.changed(this.state)
  }
  private valid(n: number) { return !this.disposed && n === this.generation }
  private stop() {
    this.generation++
    clearTimeout(this.timer)
    const id = this.jobId
    this.jobId = null
    return id
  }
  private forgetAttempt(id: string) {
    for (const [key, entry] of this.uploaded) if (entry.attemptId === id) this.uploaded.delete(key)
  }
  private cancelRemote(id: string | null) { if (id) void this.api.cancelJob(id).catch(() => {}) }
  reset() {
    this.uploaded.clear()
    this.cancelRemote(this.stop())
    if (!this.disposed) this.emit(initial())
  }
  dispose() { this.disposed = true; this.cancelRemote(this.stop()); this.uploaded.clear() }
  async cancel() {
    this.uploaded.clear()
    const id = this.stop(), n = this.generation
    this.emit({ phase: 'idle', job: null, endState: null, error: null })
    if (!id) { this.emit({ endState: 'cancelled' }); return }
    try {
      const job = await this.api.cancelJob(id)
      if (!this.valid(n)) return
      if (job.state === 'cancelled') this.emit({ endState: 'cancelled' })
      else this.emit({ error: '서버에서 취소를 확인하지 못했어요. 이 결과는 연습에 반영하지 않아요.' })
    } catch (e) {
      if (this.valid(n)) this.emit({ error: toApiError(e).messageKo })
    }
  }
  async submit(take: Take, body: AttemptBody) {
    if (this.disposed || this.state.phase === 'uploading' || this.state.phase === 'waiting') return
    let uploaded = this.uploaded.get(take.id)
    const previousJob = this.stop()
    // A transport failure does not fail the server job. Resume it for this take.
    if (previousJob && previousJob !== uploaded?.jobId) {
      this.cancelRemote(previousJob)
      for (const [key, entry] of this.uploaded) if (entry.jobId === previousJob) this.uploaded.delete(key)
    }
    const n = this.generation
    this.emit({ ...initial(), phase: uploaded?.jobId ? 'waiting' : 'uploading' })
    try {
      if (uploaded?.jobId) {
        this.jobId = uploaded.jobId
        void this.poll(n, uploaded.jobId, uploaded.attemptId)
        return
      }
      let id = uploaded?.attemptId
      if (!id) {
        id = (await this.api.createAttempt(body)).attempt_id
        if (!this.valid(n)) return
        await this.api.uploadAudio(id, take.wav)
        if (!this.valid(n)) return
        // A failed upload must never be reused as an uploaded attempt.
        uploaded = { attemptId: id }
        this.uploaded.set(take.id, uploaded)
        this.remember(id, take)
      }
      // Gateway keys are global: a replacement attempt needs a different key, even for the same WAV.
      const job = await this.api.submitAttempt(id, `${take.id}:${id}`)
      if (!this.valid(n)) { this.cancelRemote(job.job_id); return }
      uploaded!.jobId = job.job_id
      this.jobId = job.job_id
      this.emit({ phase: 'waiting', job })
      void this.poll(n, job.job_id, id)
    } catch (e) {
      if (!this.valid(n)) return
      const error = toApiError(e)
      if (RENEW_ATTEMPT.has(error.code)) this.uploaded.delete(take.id)
      this.emit({ phase: 'idle', error: error.messageKo })
    }
  }
  private async poll(n: number, jobId: string, attemptId: string) {
    try {
      const job = await this.api.job(jobId)
      if (!this.valid(n)) return
      this.emit({ job })
      if (!TERMINAL_JOB_STATES.includes(job.state)) {
        this.timer = setTimeout(() => void this.poll(n, jobId, attemptId), 600)
        return
      }
      this.jobId = null
      if (job.state !== 'completed') {
        // Failed/cancelled/expired analysis released its WAV; retry must upload a new attempt.
        this.forgetAttempt(attemptId)
        this.emit({ phase: 'idle', endState: job.state, error: job.error_code ? messageFor(job.error_code) : null })
        return
      }
      const result = await this.api.result(job.attempt_id ?? attemptId)
      if (!this.valid(n)) return
      this.emit({ phase: 'done', endState: 'completed' })
      this.completed(result)
    } catch (e) {
      if (!this.valid(n)) return
      const error = toApiError(e)
      if (RENEW_ATTEMPT.has(error.code)) { this.forgetAttempt(attemptId); this.jobId = null }
      this.emit({ phase: 'idle', error: error.messageKo })
    }
  }
}
