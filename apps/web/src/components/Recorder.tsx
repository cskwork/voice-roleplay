import { useEffect } from 'react'
import { useRecorder, type Take } from '../hooks/audio'
import { Icon } from './Icon'
import { ActionButton, LevelMeter, Notice } from './ui'

const fmt = (ms: number) => `${Math.floor(ms / 60000)}:${String(Math.floor((ms % 60000) / 1000)).padStart(2, '0')}`

/**
 * Record → stop → preview → re-record → submit. Only the take shown at submit time is sent.
 * Keyboard: the record button toggles with Enter/Space like any button.
 */
export function Recorder({
  deviceId,
  minMs,
  maxMs,
  submitDisabledReason,
  submitting,
  onSubmit,
  onTakeChange,
}: {
  deviceId: string
  minMs: number
  maxMs: number
  submitDisabledReason?: string | null
  submitting?: boolean
  onSubmit(take: Take): void
  onTakeChange?(): void
}) {
  const r = useRecorder(deviceId, maxMs)
  useEffect(() => onTakeChange?.(), [r.take, onTakeChange])
  const tooShort = r.take && r.take.durationMs < minMs
  return (
    <div className="recorder">
      <div className="recorder-main">
        {r.recording ? (
          <button type="button" className="rec-button is-recording" onClick={r.stop} aria-label="녹음 정지">
            <Icon name="stop" size={36} />
            <span className="rec-label">녹음 중 · 정지</span>
          </button>
        ) : (
          <button type="button" className="rec-button" onClick={() => void r.start()} disabled={submitting} aria-label={r.take ? '다시 녹음' : '녹음 시작'}>
            <Icon name={r.take ? 'replay' : 'mic'} size={36} />
            <span className="rec-label">{r.take ? '다시 녹음' : '녹음 시작'}</span>
          </button>
        )}
        <div className="recorder-side">
          <p className="rec-time" aria-live="off">
            <span className={r.recording ? 'rec-dot' : 'rec-dot idle'} aria-hidden="true" />
            {r.recording ? '녹음 중' : r.take ? '녹음 완료' : '대기'} · {fmt(r.recording ? r.elapsedMs : (r.take?.durationMs ?? 0))} / {fmt(maxMs)}
          </p>
          {r.recording && <LevelMeter getLevel={r.level} />}
          <p className="muted small">
            최소 {Math.round(minMs / 1000)}초, 최대 {Math.round(maxMs / 1000)}초. 최대 길이에 닿으면 자동으로 멈춰요.
          </p>
        </div>
      </div>
      {r.error && <Notice tone="danger" icon="alert">{r.error}</Notice>}
      {r.expired && <Notice tone="warn">미리 듣기용 녹음은 5분 뒤 지워져요. 다시 녹음해 주세요.</Notice>}
      {r.take && !r.recording && (
        <div className="take">
          <label className="take-label">
            미리 듣기
            <audio controls src={r.take.url} preload="auto" />
          </label>
          {tooShort && <Notice tone="warn">녹음이 너무 짧아요. {Math.round(minMs / 1000)}초 이상 말해 주세요.</Notice>}
          <div className="row">
            <ActionButton
              className="btn btn-primary"
              icon="send"
              onClick={() => r.take && onSubmit(r.take)}
              disabledReason={submitting ? '제출 중이에요' : tooShort ? '녹음이 너무 짧아요' : (submitDisabledReason ?? null)}
            >
              제출하고 분석받기
            </ActionButton>
            <button type="button" className="btn btn-ghost" onClick={r.discard} disabled={submitting}>
              녹음 지우기
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
