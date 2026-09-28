import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Icon, type IconName } from './Icon'
import { toDb } from '../lib/pcm'
import type { Feedback, FeedbackStatus, JobState } from '../lib/types'

export function Badge({ tone = 'neutral', children }: { tone?: 'neutral' | 'ok' | 'warn' | 'danger' | 'brand'; children: ReactNode }) {
  return <span className={`badge badge-${tone}`}>{children}</span>
}

export function Notice({ tone = 'info', icon = 'info', children }: { tone?: 'info' | 'warn' | 'danger'; icon?: IconName; children: ReactNode }) {
  return (
    <div className={`notice notice-${tone}`} role={tone === 'danger' ? 'alert' : undefined}>
      <Icon name={icon} />
      <div>{children}</div>
    </div>
  )
}

export function PronunciationNote() {
  return (
    <p className="pron-note">
      <Icon name="info" size={16} /> 발음 평가는 제공되지 않습니다. 결과는 음성 인식이 받아쓴 문장을 기준으로 해요.
    </p>
  )
}

/**
 * A disabled button that says why it is disabled. `quietReason` keeps the reason out of the layout
 * (screen readers still get it via aria-describedby, pointer users via the tooltip) for toolbars whose
 * buttons change state every turn and must not reflow.
 */
export function ActionButton({
  onClick,
  disabledReason,
  children,
  className = 'btn',
  icon,
  quietReason = false,
  ...rest
}: {
  onClick?: () => void
  disabledReason?: string | null
  children: ReactNode
  className?: string
  icon?: IconName
  quietReason?: boolean
  'aria-pressed'?: boolean
  'aria-keyshortcuts'?: string
  type?: 'button' | 'submit'
}) {
  const id = useId()
  return (
    <span className="action" title={quietReason && disabledReason ? disabledReason : undefined}>
      <button type="button" className={className} onClick={onClick} disabled={!!disabledReason} aria-describedby={disabledReason ? id : undefined} {...rest}>
        {icon && <Icon name={icon} />}
        <span>{children}</span>
      </button>
      {disabledReason && (
        <span id={id} className={quietReason ? 'sr-only' : 'disabled-reason'}>
          {disabledReason}
        </span>
      )}
    </span>
  )
}

export interface ChoiceOption<T extends string> {
  value: T
  label: string
  note?: string
  disabledReason?: string
}

/**
 * Compact segmented choice (radio group). The selected option's note, and the reason for any
 * unavailable option, are shown as one line under the control instead of inside every option.
 */
export function Choice<T extends string>({
  label,
  name,
  value,
  options,
  onChange,
}: {
  label: string
  name: string
  value: T
  options: ChoiceOption<T>[]
  onChange(v: T): void
}) {
  const helpId = useId()
  const current = options.find((o) => o.value === value)
  const off = options.filter((o) => o.disabledReason)
  return (
    <fieldset className="choice" aria-describedby={helpId}>
      <legend className="choice-label">{label}</legend>
      <div className="choice-options">
        {options.map((o) => (
          <label key={o.value} className={`choice-opt ${value === o.value ? 'is-on' : ''} ${o.disabledReason ? 'is-off' : ''}`} title={o.disabledReason}>
            <input type="radio" name={name} value={o.value} checked={value === o.value} disabled={!!o.disabledReason} onChange={() => onChange(o.value)} />
            {value === o.value && <Icon name="check" size={16} />}
            <span>{o.label}</span>
          </label>
        ))}
      </div>
      <p id={helpId} className="choice-help">
        {current?.note}
        {off.map((o) => (
          <span key={o.value} className="choice-off">
            {o.label}: {o.disabledReason}
          </span>
        ))}
      </p>
    </fieldset>
  )
}

/** Input level meter; reads the level itself on animation frames so the page doesn't re-render. */
export function LevelMeter({ getLevel, label = '입력 레벨' }: { getLevel: () => number; label?: string }) {
  const bar = useRef<HTMLDivElement>(null)
  const host = useRef<HTMLDivElement>(null)
  useEffect(() => {
    let raf = 0
    let lastAria = 0
    const tick = (t: number) => {
      const db = toDb(getLevel())
      const pct = Math.max(0, Math.min(100, ((db + 60) / 60) * 100))
      if (bar.current) bar.current.style.transform = `scaleX(${pct / 100})`
      if (host.current && t - lastAria > 500) {
        host.current.setAttribute('aria-valuenow', String(Math.round(pct)))
        host.current.setAttribute('aria-valuetext', pct < 5 ? '소리 없음' : pct < 35 ? '작음' : pct < 80 ? '적당함' : '큼')
        lastAria = t
      }
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [getLevel])
  return (
    <div ref={host} className="meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={0}>
      <div ref={bar} className="meter-bar" />
    </div>
  )
}

const STATUS_LABEL: Record<FeedbackStatus, [string, 'ok' | 'brand' | 'warn' | 'neutral']> = {
  observed: ['확인됨', 'ok'],
  suggested: ['제안', 'brand'],
  needs_confirmation: ['확인 필요', 'warn'],
  unavailable: ['제공 불가', 'neutral'],
}

const CATEGORY_LABEL: Record<Feedback['category'], string> = {
  grammar: '문법',
  expression: '표현',
  vocabulary: '어휘',
  goal: '상황 목표',
  fluency_metric: '말하기 지표',
}

export function FeedbackCard({ f, action }: { f: Feedback; action?: ReactNode }) {
  const [label, tone] = STATUS_LABEL[f.status] ?? ['', 'neutral']
  return (
    <article className="card feedback">
      <div className="feedback-tags">
        <Badge tone={tone}>{label}</Badge>
        <Badge tone={f.severity === 'required' ? 'danger' : 'neutral'}>{f.severity === 'required' ? '꼭 고치기' : '더 자연스럽게'}</Badge>
        <Badge>{CATEGORY_LABEL[f.category] ?? f.category}</Badge>
      </div>
      {f.evidence_quote && (
        <div className="quote">
          <span className="quote-label">내가 말한 부분</span>
          <q lang="en">{f.evidence_quote}</q>
        </div>
      )}
      {f.suggestion && (
        <div className="suggestion">
          <span className="quote-label">이렇게 말해 보세요</span>
          <p lang="en">{f.suggestion}</p>
        </div>
      )}
      {f.explanation_ko && <p className="explain">{f.explanation_ko}</p>}
      {f.status === 'needs_confirmation' && <p className="muted small">이렇게 들렸어요. 맞는지 전사를 확인해 주세요.</p>}
      {action}
    </article>
  )
}

export const JOB_LABEL: Record<JobState, string> = {
  queued: '대기 중',
  transcribing: '받아쓰는 중',
  analyzing: '문장 분석 중',
  synthesizing: '모범 음성 만드는 중',
  completed: '완료',
  failed: '실패',
  cancelled: '취소됨',
  expired: '만료됨 — 다시 녹음이 필요해요',
}

const JOB_STEPS: JobState[] = ['queued', 'transcribing', 'analyzing', 'synthesizing', 'completed']

export function JobProgress({ state }: { state: JobState }) {
  const idx = JOB_STEPS.indexOf(state)
  return (
    <ol className="job-steps" aria-label="분석 진행 상태">
      {JOB_STEPS.map((s, i) => (
        <li key={s} className={i < idx ? 'done' : i === idx ? 'current' : ''} aria-current={i === idx ? 'step' : undefined}>
          <span className="dot" aria-hidden="true">
            {i < idx ? <Icon name="check" size={14} /> : null}
          </span>
          {JOB_LABEL[s]}
        </li>
      ))}
    </ol>
  )
}
