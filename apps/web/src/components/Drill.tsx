import { useState } from 'react'
import { useAttemptRunner } from '../hooks/attempt'
import type { AttemptResult } from '../lib/types'
import { useApp } from '../state'
import { ModelAudio } from './ModelAudio'
import { Recorder } from './Recorder'
import { JOB_LABEL, JobProgress, Notice, PronunciationNote } from './ui'
import { WordDiff } from './WordDiff'

/**
 * "다시 말하기" loop: see the suggestion, hear it, record it, compare with the target
 * (and with what was said before). Works without saved history.
 */
export function Drill({
  target,
  before,
  scenarioId,
  voiceId,
  historyOptIn,
  onDone,
}: {
  target: string
  before?: string
  scenarioId: string
  voiceId: string
  historyOptIn: boolean
  onDone?(r: AttemptResult): void
}) {
  const { health, micDeviceId } = useApp()
  const [result, setResult] = useState<AttemptResult | null>(null)
  const runner = useAttemptRunner((r) => {
    setResult(r)
    onDone?.(r)
  })
  const recordedReady = health?.modes.recorded.available ?? false
  const ttsReady = health?.components.tts?.ready ?? false

  return (
    <section className="drill" aria-label="다시 말하기 연습">
      <p className="drill-target" lang="en">
        {target}
      </p>
      <ModelAudio voiceId={voiceId} source={{ text: target }} ttsReady={ttsReady} />
      <Recorder
        deviceId={micDeviceId}
        minMs={1000}
        maxMs={30000}
        submitting={runner.busy}
        submitDisabledReason={recordedReady ? null : (health?.modes.recorded.reason_ko ?? '녹음형 분석을 지금 사용할 수 없어요')}
        onTakeChange={() => setResult(null)}
        onSubmit={(take) =>
          void runner.submit(take, {
            exercise_type: 'drill',
            scenario_id: scenarioId,
            history_opt_in: historyOptIn,
            target_text: target,
          })
        }
      />
      {runner.busy && runner.job && (
        <div className="row">
          <JobProgress state={runner.job.state} />
          <button type="button" className="btn btn-ghost" onClick={() => void runner.cancel()}>
            분석 취소
          </button>
        </div>
      )}
      {runner.endState && runner.endState !== 'completed' && <Notice tone="warn">{JOB_LABEL[runner.endState]}</Notice>}
      {runner.error && <Notice tone="danger" icon="alert">{runner.error}</Notice>}
      {result && (
        <div className="compare" aria-live="polite">
          {before && (
            <div>
              <span className="quote-label">처음 말한 문장</span>
              <p lang="en">{before}</p>
            </div>
          )}
          <div>
            <span className="quote-label">이번에 인식된 문장</span>
            <p lang="en">{result.transcript || '(인식된 말이 없어요)'}</p>
          </div>
          <WordDiff target={target} heard={result.transcript ?? ''} serverOps={result.target_diff} />
          <PronunciationNote />
        </div>
      )}
    </section>
  )
}
