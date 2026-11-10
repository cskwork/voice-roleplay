import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from '../src/lib/errors'
import { AttemptRun } from '../src/lib/attemptRun'
import type { Take } from '../src/hooks/audio'
import type { AttemptResult, Job } from '../src/lib/types'

const deferred = <T>() => { let resolve!: (v: T) => void; let reject!: (e: Error) => void; const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no }); return { promise, resolve, reject } }
const take: Take = { id: 'take1', wav: new ArrayBuffer(2), durationMs: 1500, recordedAt: 0, url: 'blob:x' }
const body = { exercise_type: 'shadowing', scenario_id: 'cafe_order', text_id: 'text1', history_opt_in: false } as const
const result = { attempt_id: 'a1', transcript: 'Hi.' } as AttemptResult
const job = { job_id: 'j1', attempt_id: 'a1', state: 'completed' } as Job
const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve() }
function setup() {
  const api = { createAttempt: vi.fn(async () => ({ attempt_id: 'a1' })), uploadAudio: vi.fn(async () => ({})), submitAttempt: vi.fn(async () => job), job: vi.fn(async () => job), result: vi.fn(async () => result), cancelJob: vi.fn(async () => ({ ...job, state: 'cancelled' as const })) }
  const changed = vi.fn(), completed = vi.fn(), remember = vi.fn()
  const run = new AttemptRun(api, changed, completed, remember)
  return { api, run, changed, completed, remember }
}
afterEach(() => vi.useRealTimers())
describe('recording transaction lifetime', () => {
  it('completes only after upload, submission, terminal job and result', async () => {
    const { run, api, completed } = setup()
    await run.submit(take, body); await settle()
    expect(api.uploadAudio).toHaveBeenCalledWith('a1', take.wav)
    expect(api.submitAttempt).toHaveBeenCalledWith('a1', `${take.id}:a1`)
    expect(completed).toHaveBeenCalledExactlyOnceWith(result)
    expect(run.state.phase).toBe('done')
    run.dispose()
  })
  it('uploads again after a failed upload instead of submitting an empty cached attempt', async () => {
    const { run, api, completed, remember } = setup()
    api.uploadAudio.mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body)
    expect(api.submitAttempt).not.toHaveBeenCalled()
    expect(remember).not.toHaveBeenCalled()
    await run.submit(take, body); await settle()
    expect(api.uploadAudio).toHaveBeenCalledTimes(2)
    expect(api.createAttempt).toHaveBeenCalledTimes(2)
    expect(completed).toHaveBeenCalledOnce()
    run.dispose()
  })
  it('blocks duplicate clicks before a create request resolves', async () => {
    const { run, api } = setup(); const create = deferred<{ attempt_id: string }>()
    api.createAttempt.mockReturnValueOnce(create.promise)
    const p = run.submit(take, body); await run.submit(take, body)
    expect(api.createAttempt).toHaveBeenCalledTimes(1)
    create.resolve({ attempt_id: 'a1' }); await p; await settle(); run.dispose()
  })
  for (const boundary of ['create', 'upload', 'submit', 'poll', 'result'] as const) it(`ignores a late ${boundary} response after leaving the screen`, async () => {
    const { run, api, completed, changed } = setup()
    const wait = deferred<never>()
    const target = { create: api.createAttempt, upload: api.uploadAudio, submit: api.submitAttempt, poll: api.job, result: api.result }[boundary]
    target.mockReturnValueOnce(wait.promise)
    const p = run.submit(take, body); await settle()
    run.dispose(); const before = changed.mock.calls.length
    const values = { create: { attempt_id: 'a1' }, upload: {}, submit: job, poll: job, result }
    wait.resolve(values[boundary] as never); await p; await settle()
    expect(completed).not.toHaveBeenCalled()
    expect(changed).toHaveBeenCalledTimes(before)
    if (boundary === 'create') expect(api.uploadAudio).not.toHaveBeenCalled()
    if (boundary === 'upload') expect(api.submitAttempt).not.toHaveBeenCalled()
    if (boundary === 'submit') expect(api.cancelJob).toHaveBeenCalledWith('j1')
  })
  it('a late previous result cannot fill a reset/retried expression', async () => {
    const { run, api, completed } = setup(); const old = deferred<AttemptResult>()
    api.result.mockReturnValueOnce(old.promise)
    await run.submit(take, body); await settle(); run.reset()
    const next = { ...result, attempt_id: 'a2', transcript: 'Next.' }
    api.result.mockResolvedValueOnce(next)
    await run.submit({ ...take, id: 'take2' }, body); await settle()
    old.resolve(result); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith(next)
    run.dispose()
  })
  for (const stage of ['createAttempt', 'submitAttempt', 'job', 'result'] as const) it(`${stage} failure is explicit and releases busy state`, async () => {
    const { run, api, completed } = setup()
    api[stage].mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body); await settle()
    expect(run.state.phase).toBe('idle'); expect(run.state.error).toBeTruthy()
    expect(completed).not.toHaveBeenCalled(); run.dispose()
  })
  it('reports a failed explicit cancellation without claiming server cancellation', async () => {
    const { run, api } = setup()
    api.job.mockResolvedValue({ ...job, state: 'transcribing' })
    api.cancelJob.mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body); await settle(); await run.cancel()
    expect(run.state.error).toBeTruthy(); expect(run.state.endState).toBeNull(); expect(run.state.phase).toBe('idle')
    run.dispose()
  })
  it('polls pending jobs, and cancel prevents another tick or completion', async () => {
    vi.useFakeTimers(); const { run, api, completed } = setup()
    api.job.mockResolvedValue({ ...job, state: 'transcribing' })
    await run.submit(take, body); await settle(); run.cancel()
    await vi.advanceTimersByTimeAsync(1200)
    expect(api.job).toHaveBeenCalledTimes(1)
    expect(api.cancelJob).toHaveBeenCalledWith('j1')
    expect(run.state.endState).toBe('cancelled'); expect(completed).not.toHaveBeenCalled(); run.dispose()
  })
})

// Mirror gateway _idempotent(): a key keeps returning the original job, including cancelled jobs.
function idempotentSetup() {
  const jobs = new Map<string, Job>(), keys = new Map<string, string>()
  let count = 0
  const api = {
    createAttempt: vi.fn(async () => ({ attempt_id: `a${++count}` })),
    uploadAudio: vi.fn(async () => ({})),
    submitAttempt: vi.fn(async (id: string, key: string) => {
      const existing = keys.get(key)
      if (existing) {
        const job = jobs.get(existing)!
        if (job.attempt_id !== id) throw new Error('IDEMPOTENCY_CONFLICT')
        return { ...job }
      }
      const job: Job = { job_id: `j${id}`, attempt_id: id, state: 'queued' }
      jobs.set(job.job_id, job); keys.set(key, job.job_id)
      return { ...job }
    }),
    job: vi.fn(async (id: string) => {
      const job = jobs.get(id)!
      if (job.state === 'queued') job.state = 'completed'
      return { ...job }
    }),
    result: vi.fn(async (id: string) => ({ ...result, attempt_id: id })),
    cancelJob: vi.fn(async (id: string) => { const job = jobs.get(id)!; job.state = 'cancelled'; return { ...job } }),
  }
  const completed = vi.fn()
  const run = new AttemptRun(api, vi.fn(), completed, vi.fn())
  return { api, run, completed, jobs }
}

describe('same recording recovery with real idempotency semantics', () => {
  for (const stage of ['createAttempt', 'uploadAudio'] as const) it(`recovers the same recording after ${stage} network failure`, async () => {
    const { api, run, completed } = idempotentSetup()
    api[stage].mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body); await settle()
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledOnce(); expect(run.state.phase).toBe('done')
    expect(api.cancelJob).not.toHaveBeenCalled(); run.dispose()
  })
  for (const stage of ['submitAttempt', 'job', 'result'] as const) it(`starts a new attempt after ${stage} reports expired server state`, async () => {
    const { api, run, completed } = idempotentSetup()
    api[stage].mockRejectedValueOnce(new ApiError('NOT_FOUND', '만료됐어요.', 404))
    await run.submit(take, body); await settle()
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a2' })
    expect(api.createAttempt).toHaveBeenCalledTimes(2); expect(api.uploadAudio).toHaveBeenCalledTimes(2)
    run.dispose()
  })
  it('resumes polling after one network failure without cancelling or submitting the same job again', async () => {
    const { api, run, completed } = idempotentSetup()
    api.job.mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body); await settle()
    expect(run.state.phase).toBe('idle'); expect(run.state.error).toBeTruthy()
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a1' })
    expect(api.cancelJob).not.toHaveBeenCalled()
    expect(api.createAttempt).toHaveBeenCalledTimes(1); expect(api.uploadAudio).toHaveBeenCalledTimes(1)
    expect(api.submitAttempt).toHaveBeenCalledTimes(1)
    expect(api.job).toHaveBeenCalledTimes(2)
    run.dispose()
  })
  it('retries fetching a completed result without cancelling or resubmitting', async () => {
    const { api, run, completed } = idempotentSetup()
    api.result.mockRejectedValueOnce(new TypeError('offline'))
    await run.submit(take, body); await settle()
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a1' })
    expect(api.submitAttempt).toHaveBeenCalledTimes(1)
    expect(api.cancelJob).not.toHaveBeenCalled(); run.dispose()
  })
  it('reuses the attempt/key when the submit reply is lost after the gateway enqueues it', async () => {
    const { api, run, completed } = idempotentSetup()
    const submit = api.submitAttempt.getMockImplementation()!
    api.submitAttempt.mockImplementationOnce(async (id, key) => { await submit(id, key); throw new TypeError('reply lost') })
    await run.submit(take, body); await settle()
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledOnce()
    expect(api.createAttempt).toHaveBeenCalledTimes(1); expect(api.uploadAudio).toHaveBeenCalledTimes(1)
    expect(api.submitAttempt.mock.calls[0]).toEqual(api.submitAttempt.mock.calls[1])
    expect(api.cancelJob).not.toHaveBeenCalled(); run.dispose()
  })
  for (const state of ['failed', 'cancelled', 'expired'] as const) it(`creates a new attempt and key after a terminal ${state} job`, async () => {
    const { api, run, completed } = idempotentSetup()
    api.job.mockImplementationOnce(async (id) => ({ job_id: id, attempt_id: 'a1', state }))
    await run.submit(take, body); await settle()
    expect(run.state.endState).toBe(state)
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a2' })
    expect(api.createAttempt).toHaveBeenCalledTimes(2); expect(api.uploadAudio).toHaveBeenCalledTimes(2)
    expect(api.submitAttempt.mock.calls[0]![1]).not.toBe(api.submitAttempt.mock.calls[1]![1])
    run.dispose()
  })
  it('creates a new attempt/key after explicit cancellation of the same recording', async () => {
    vi.useFakeTimers()
    const { api, run, completed } = idempotentSetup()
    api.job.mockImplementationOnce(async (id) => ({ job_id: id, attempt_id: 'a1', state: 'transcribing' }))
    await run.submit(take, body); await settle(); await run.cancel()
    expect(run.state.endState).toBe('cancelled')
    await run.submit(take, body); await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a2' })
    expect(api.createAttempt).toHaveBeenCalledTimes(2)
    expect(api.submitAttempt.mock.calls[0]![1]).not.toBe(api.submitAttempt.mock.calls[1]![1])
    run.dispose()
  })
  it('a late old submit response cannot cancel the new job after reset and same-take retry', async () => {
    const { api, run, completed, jobs } = idempotentSetup()
    const submit = api.submitAttempt.getMockImplementation()!, late = deferred<Job>()
    api.submitAttempt.mockImplementationOnce(async (id, key) => { await submit(id, key); return late.promise })
    const first = run.submit(take, body); await settle(); run.reset()
    await run.submit(take, body); await settle()
    late.resolve({ ...jobs.get('ja1')! }); await first; await settle()
    expect(completed).toHaveBeenCalledExactlyOnceWith({ ...result, attempt_id: 'a2' })
    expect(api.cancelJob).toHaveBeenCalledExactlyOnceWith('ja1')
    expect(jobs.get('ja2')!.state).toBe('completed')
    run.dispose()
  })
})
