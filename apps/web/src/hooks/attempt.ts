import { useCallback, useRef, useState } from 'react'
import { api } from '../lib/api'
import { messageFor, toApiError } from '../lib/errors'
import { rememberTake } from '../lib/takes'
import type { AttemptResult, Job } from '../lib/types'
import type { Take } from './audio'
import { useJobPoll } from './audio'

export type AttemptBody = Parameters<typeof api.createAttempt>[0]

/** create attempt → upload WAV → submit (idempotent per take) → poll job → fetch result. */
export function useAttemptRunner(onResult: (r: AttemptResult) => void) {
  const [phase, setPhase] = useState<'idle' | 'uploading' | 'waiting' | 'done'>('idle')
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [endState, setEndState] = useState<Job['state'] | null>(null)
  const byTake = useRef(new Map<string, string>()) // take id -> attempt id
  const current = useRef<string | null>(null)

  const { job } = useJobPoll(jobId, async (j) => {
    setEndState(j.state)
    if (j.state === 'completed') {
      try {
        const r = await api.result(j.attempt_id ?? current.current!)
        setPhase('done')
        onResult(r)
      } catch (e) {
        setError(toApiError(e).messageKo)
        setPhase('idle')
      }
    } else {
      if (j.error_code) setError(messageFor(j.error_code))
      setPhase('idle')
    }
  })

  const submit = useCallback(async (take: Take, body: AttemptBody) => {
    if (phase === 'uploading' || phase === 'waiting') return
    setError(null)
    setEndState(null)
    setPhase('uploading')
    try {
      let attemptId = byTake.current.get(take.id)
      if (!attemptId) {
        attemptId = (await api.createAttempt(body)).attempt_id
        byTake.current.set(take.id, attemptId)
        await api.uploadAudio(attemptId, take.wav)
        rememberTake(attemptId, take.wav, take.recordedAt) // "내 발음" word playback on the result screen
      }
      current.current = attemptId
      const { job_id } = await api.submitAttempt(attemptId, take.id)
      setJobId(job_id)
      setPhase('waiting')
    } catch (e) {
      setError(toApiError(e).messageKo)
      setPhase('idle')
    }
  }, [phase])

  const cancel = useCallback(async () => {
    if (!jobId) return
    try {
      await api.cancelJob(jobId)
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }, [jobId])

  const reset = useCallback(() => {
    setJobId(null)
    setPhase('idle')
    setEndState(null)
    setError(null)
  }, [])

  return { phase, job, endState, error, submit, cancel, reset, busy: phase === 'uploading' || phase === 'waiting' }
}
