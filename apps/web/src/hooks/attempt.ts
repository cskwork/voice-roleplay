import { useEffect, useRef, useState } from 'react'
import { api } from '../lib/api'
import { AttemptRun } from '../lib/attemptRun'
import { rememberTake } from '../lib/takes'
import type { AttemptResult } from '../lib/types'

export type AttemptBody = Parameters<typeof api.createAttempt>[0]

/** create → upload → submit → poll → result; reset/unmount invalidate every late reply. */
export function useAttemptRunner(onResult: (r: AttemptResult) => void) {
  const callback = useRef(onResult)
  callback.current = onResult
  const [state, setState] = useState<AttemptRun['state']>({ phase: 'idle', job: null, endState: null, error: null })
  const run = useRef<AttemptRun | null>(null)
  // Construct in the effect so StrictMode's setup/cleanup/setup gets a fresh transaction.
  useEffect(() => {
    const r = new AttemptRun(api, setState, (result) => callback.current(result), (id, take) => rememberTake(id, take.wav, take.recordedAt))
    run.current = r
    return () => { run.current = null; r.dispose() }
  }, [])
  return {
    ...state,
    submit: (take: Parameters<AttemptRun['submit']>[0], body: AttemptBody) => run.current?.submit(take, body),
    cancel: () => run.current?.cancel(),
    reset: () => run.current?.reset(),
    busy: state.phase === 'uploading' || state.phase === 'waiting',
  }
}
