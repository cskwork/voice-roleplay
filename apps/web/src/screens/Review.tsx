import { useCallback, useEffect, useState } from 'react'
import { Drill } from '../components/Drill'
import { Icon } from '../components/Icon'
import { ModelAudio } from '../components/ModelAudio'
import { Badge, Notice } from '../components/ui'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import type { HistoryEntry, ReviewItem } from '../lib/types'
import { useApp, useVoiceId } from '../state'

const TYPE_LABEL: Record<string, string> = { reading: '문장 읽기', shadowing: '따라 말하기', free_answer: '자유 답변', roleplay_turn: '턴제 역할극', drill: '다시 말하기' }

function DueCard({ item, onGraded }: { item: ReviewItem; onGraded(): void }) {
  const app = useApp()
  const voiceId = useVoiceId()
  const [practicing, setPracticing] = useState(false)
  const [tried, setTried] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const grade = async (result: 'again' | 'good') => {
    try {
      await api.gradeReview(item.item_id, result)
      onGraded()
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }

  return (
    <li className="card review-card">
      <div className="card-head">
        <Badge tone="brand">상자 {item.box}/5</Badge>
        <span className="muted small">{item.source_type === 'session' ? '회화에서 저장한 표현' : item.source_type === 'attempt' ? '연습에서 저장한 표현' : '모범 표현'}</span>
      </div>
      <p className="prompt-en" lang="en">
        {item.text_en}
      </p>
      {item.text_ko && <p className="prompt-ko">{item.text_ko}</p>}
      <ModelAudio voiceId={voiceId} source={{ text: item.text_en }} ttsReady={app.health?.components.tts?.ready ?? false} />
      {practicing ? (
        <Drill
          target={item.text_en}
          voiceId={voiceId}
          historyOptIn
          onDone={() => setTried(true)}
        />
      ) : (
        <button type="button" className="btn btn-primary" onClick={() => setPracticing(true)}>
          <Icon name="mic" /> 말해 보기
        </button>
      )}
      <div className="row" role="group" aria-label="복습 결과">
        <button type="button" className="btn" onClick={() => void grade('again')}>
          다시 볼래요
        </button>
        <button type="button" className="btn btn-soft" onClick={() => void grade('good')}>
          <Icon name="check" /> {tried ? '잘 말했어요' : '이미 익숙해요'}
        </button>
      </div>
      {error && <Notice tone="danger" icon="alert">{error}</Notice>}
    </li>
  )
}

export function Review() {
  const { settings, scenario } = useApp()
  const scenarioTitle = (id: string) => scenario(id)?.title_ko ?? id
  const [due, setDue] = useState<ReviewItem[] | null>(null)
  const [history, setHistory] = useState<HistoryEntry[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setError(null)
    try {
      const [d, h] = await Promise.all([api.reviewDue(), api.history()])
      setDue(d)
      setHistory(h)
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  const empty = due?.length === 0 && history?.length === 0

  return (
    <main className="page narrow" aria-labelledby="rv-title">
      <header className="page-head">
        <p className="eyebrow">복습</p>
        <h1 id="rv-title">오늘 다시 말해 볼 표현</h1>
        <p className="lead">저장에 동의한 연습만 여기에 모여요. 간격을 두고 다시 말하면 오래 기억돼요.</p>
      </header>

      {error && <Notice tone="danger" icon="alert">{error}</Notice>}
      {empty && (
        <Notice>
          아직 복습할 표현이 없어요.{' '}
          {!settings.history_opt_in && (
            <>
              연습을 시작할 때 <strong>기록 저장</strong>을 켜면 표현이 여기에 모여요. <a href="#/settings">설정 열기</a>
            </>
          )}
        </Notice>
      )}

      {due && due.length > 0 && (
        <ul className="stack" aria-label="복습할 표현">
          {due.map((item) => (
            <DueCard key={item.item_id} item={item} onGraded={() => void load()} />
          ))}
        </ul>
      )}

      {history && history.length > 0 && (
        <section aria-labelledby="h-h">
          <h2 id="h-h" className="section-title">
            연습 기록
          </h2>
          <p className="muted small">
            <Icon name="info" size={16} /> 녹음 음성은 저장하지 않아 다시 들을 수 없어요. 전사와 피드백만 보관돼요.
          </p>
          <ul className="history">
            {history.map((h) => (
              <li key={`${h.kind}:${h.id}`} className="card history-row">
                <span className="muted small">
                  {new Date(h.created_at * 1000).toLocaleString('ko-KR')} · {h.kind === 'session' ? '실시간 회화' : (TYPE_LABEL[h.exercise_type ?? ''] ?? '녹음형 연습')}
                  {h.scenario_id && ` · ${scenarioTitle(h.scenario_id)}`}
                </span>
                {h.text && <p lang="en">{h.text}</p>}
                <span className="small">개선 제안 {h.feedback_count}개</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </main>
  )
}
