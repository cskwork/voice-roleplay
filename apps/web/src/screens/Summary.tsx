import { useEffect, useState } from 'react'
import { Drill } from '../components/Drill'
import { Icon } from '../components/Icon'
import { FeedbackCard, Notice, PronunciationNote } from '../components/ui'
import { useApp, useVoiceId } from '../state'

const UNSAVED_TTL_MS = 15 * 60 * 1000

export function Summary() {
  const app = useApp()
  const last = app.lastSession
  const sc = app.scenario(last?.scenarioId)
  const voiceId = useVoiceId(sc)
  const [drillId, setDrillId] = useState<string | null>(null)
  const [now, setNow] = useState(Date.now())

  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 30000)
    return () => clearInterval(t)
  }, [])

  // Unsaved summaries disappear after 15 minutes (PRD §16).
  const expired = last && !last.historyOptIn && now - last.endedAt > UNSAVED_TTL_MS
  useEffect(() => {
    if (expired) app.setLastSession(null)
  }, [expired, app])

  if (!last || !sc) {
    return (
      <main className="page narrow">
        <Notice>보여 줄 세션 요약이 없어요. 저장하지 않은 요약은 15분 뒤 사라져요.</Notice>
        <a className="btn btn-primary" href="#/home">
          학습 홈으로
        </a>
      </main>
    )
  }

  const goals = last.summary?.goals ?? []
  const items = (last.summary?.improvements ?? []).slice(0, 3)

  return (
    <main className="page narrow" aria-labelledby="sum-title">
      <header className="page-head">
        <p className="eyebrow">세션 요약 · {sc.title_ko}</p>
        <h1 id="sum-title">수고했어요! 이렇게 말해 봤어요</h1>
        {!last.historyOptIn && <p className="muted small">기록 저장을 선택하지 않아 이 요약은 15분 뒤 사라져요.</p>}
      </header>

      {last.error && <Notice tone="danger" icon="alert">{last.error}</Notice>}

      <section className="card" aria-labelledby="g-h">
        <h2 id="g-h">대화 목표</h2>
        <ol className="goals">
          {sc.goals.map((g) => {
            const done = goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')
            return (
              <li key={g.goal_id} className={done ? 'done' : ''}>
                <span className="goal-mark">
                  <Icon name={done ? 'check' : 'circle'} size={18} />
                </span>
                <span>
                  {g.ko} <strong className="small">{done ? '달성' : '확인되지 않음'}</strong>
                </span>
              </li>
            )
          })}
        </ol>
      </section>

      <section aria-labelledby="imp-h">
        <h2 id="imp-h" className="section-title">
          다음엔 이렇게 말해 보세요 <span className="muted small">(최대 3개)</span>
        </h2>
        {items.length === 0 && !last.error && (
          <Notice tone={last.summary?.status === 'ok' ? 'info' : 'warn'}>
            {last.summary?.status === 'held'
              ? '이번 대화에서 인식된 내 말이 없어 피드백을 만들지 않았어요.'
              : last.summary?.status === 'unavailable'
                ? '대화 모델을 쓸 수 없어 이번에는 개선 제안을 만들지 못했어요. 대화 기록은 그대로예요.'
                : '이번 대화에서 따로 고칠 점을 찾지 못했어요.'}
          </Notice>
        )}
        {items.map((f) => (
          <FeedbackCard
            key={f.feedback_id}
            f={f}
            action={
              f.suggestion ? (
                drillId === f.feedback_id ? (
                  <Drill
                    target={f.suggestion}
                    before={f.evidence_quote}
                    scenarioId={sc.scenario_id}
                    voiceId={voiceId}
                    historyOptIn={last.historyOptIn}
                  />
                ) : (
                  <button type="button" className="btn btn-primary" onClick={() => setDrillId(f.feedback_id)}>
                    <Icon name="mic" /> 다시 말하기
                  </button>
                )
              ) : null
            }
          />
        ))}
        <PronunciationNote />
      </section>

      <div className="page-actions">
        <a className="btn btn-primary" href={`#/talk/${encodeURIComponent(sc.scenario_id)}`}>
          같은 상황 다시 하기
        </a>
        <a className="btn" href={`#/practice/${encodeURIComponent(sc.scenario_id)}`}>
          녹음형으로 연습하기
        </a>
        <a className="btn btn-ghost" href="#/home">
          학습 홈
        </a>
      </div>
    </main>
  )
}
