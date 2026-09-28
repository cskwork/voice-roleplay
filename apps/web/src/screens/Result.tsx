import { useState } from 'react'
import { Icon } from '../components/Icon'
import { ModelAudio } from '../components/ModelAudio'
import { PronunciationCard, PronunciationUnavailable } from '../components/Pronunciation'
import { Badge, FeedbackCard, JobProgress, Notice, PronunciationNote } from '../components/ui'
import { diffCount, WordDiff } from '../components/WordDiff'
import { useJobPoll } from '../hooks/audio'
import { api } from '../lib/api'
import { messageFor, toApiError } from '../lib/errors'
import { unavailableReason } from '../lib/pronunciation'
import type { AttemptResult, Metrics } from '../lib/types'
import { navigate, useApp, useVoiceId, type StoredResult } from '../state'
import { roleplayThread } from './Practice'

const TYPE_LABEL = { reading: '문장 읽기', shadowing: '따라 말하기', free_answer: '자유 답변', roleplay_turn: '턴제 역할극', drill: '다시 말하기' } as const

function MetricsView({ m, edited }: { m: Metrics | null; edited: boolean }) {
  if (!m) return <p className="muted small">이번 시도에는 측정 지표가 없어요.</p>
  return (
    <>
      <dl className="metrics">
        <div>
          <dt>말하기 속도</dt>
          <dd>{m.wpm == null ? '측정 불가' : <>{Math.round(m.wpm)}<span className="unit">단어/분</span></>}</dd>
        </div>
        <div>
          <dt>말한 구간</dt>
          <dd>
            {m.speech_span_s.toFixed(1)}
            <span className="unit">초</span>
          </dd>
        </div>
        <div>
          <dt>0.5초 이상 쉰 횟수</dt>
          <dd>
            {m.pause_count}
            <span className="unit">회</span>
          </dd>
        </div>
        <div>
          <dt>쉰 시간 합계</dt>
          <dd>
            {m.total_pause_s.toFixed(1)}
            <span className="unit">초</span>
          </dd>
        </div>
      </dl>
      <p className="muted small">실력 점수가 아니며, 천천히 말해도 감점하지 않아요.{edited ? ' 원래 녹음 기준이라 전사를 고쳐도 다시 계산하지 않아요.' : ''}</p>
      <details className="fine-print">
        <summary>지표 계산 방법</summary>
        <p className="muted small">
          {m.definition_ko} (지표 {m.metrics_version})
        </p>
      </details>
    </>
  )
}

function Transcript({ r, onEdited }: { r: AttemptResult; onEdited(): void }) {
  const [editing, setEditing] = useState(false)
  const [text, setText] = useState(r.transcript ?? '')
  const [jobId, setJobId] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const { job } = useJobPoll(jobId, (j) => {
    setJobId(null)
    if (j.state === 'completed') onEdited()
    else setError(j.error_code ? messageFor(j.error_code) : '다시 분석하지 못했어요.')
  })
  const current = r.revisions.find((v) => v.revision === r.transcript_revision)

  const save = async () => {
    setSaving(true)
    setError(null)
    try {
      const res = await api.editTranscript(r.attempt_id, text.trim())
      setEditing(false)
      if (res.job_id) setJobId(res.job_id)
      else onEdited()
    } catch (e) {
      setError(toApiError(e).messageKo)
    } finally {
      setSaving(false)
    }
  }

  return (
    <section className="card" aria-labelledby="tr-h">
      <div className="card-head">
        <h2 id="tr-h">인식된 문장</h2>
        <span className="muted small">
          {current && current.source !== 'asr' ? `내가 고친 문장 (수정본 ${r.transcript_revision})` : '음성 인식 원문'}
        </span>
      </div>
      {editing ? (
        <>
          <label className="field">
            <span>들린 대로 고쳐 주세요. 원문은 그대로 보관되고, 새 수정본으로 문장 피드백만 다시 만들어요.</span>
            <textarea lang="en" rows={3} value={text} onChange={(e) => setText(e.target.value)} />
          </label>
          <div className="row">
            <button type="button" className="btn btn-primary" onClick={() => void save()} disabled={saving || !text.trim() || text.trim() === r.transcript}>
              수정본으로 다시 분석
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => { setEditing(false); setText(r.transcript ?? '') }}>
              취소
            </button>
          </div>
        </>
      ) : (
        <>
          <p className="transcript" lang="en">
            {r.transcript || '(인식된 말이 없어요)'}
          </p>
          {r.no_speech && <Notice tone="warn">말소리가 거의 들리지 않았어요. 마이크 가까이에서 다시 녹음해 보세요. 이 시도는 실력 기록에 반영하지 않아요.</Notice>}
          <button type="button" className="btn btn-ghost" onClick={() => setEditing(true)} disabled={!!jobId || !r.transcript}>
            <Icon name="edit" /> 다르게 들렸나요? 고치기
          </button>
        </>
      )}
      {r.revisions.length > 1 && (
        <details>
          <summary>이전 전사 보기</summary>
          <ol className="revisions">
            {r.revisions.map((v) => (
              <li key={v.revision} lang="en">
                <span className="muted small">{v.source === 'asr' ? '인식 원문' : `수정본 ${v.revision}`}</span> {v.text}
              </li>
            ))}
          </ol>
        </details>
      )}
      {job && <JobProgress state={job.state} />}
      {error && <Notice tone="danger" icon="alert">{error}</Notice>}
    </section>
  )
}

export function Result({ attemptId }: { attemptId: string }) {
  const app = useApp()
  const stored = app.results.get(attemptId)
  const sc = app.scenario(stored?.meta.scenarioId)
  const voiceId = useVoiceId(sc)
  const [reloadError, setReloadError] = useState<string | null>(null)

  if (!stored || !sc) {
    return (
      <main className="page narrow">
        <Notice>결과가 이 화면에 남아 있지 않아요. 저장하지 않은 결과는 새로고침하면 사라져요.</Notice>
        <a className="btn btn-primary" href="#/home">
          학습 홈으로
        </a>
      </main>
    )
  }

  const { result: r, meta } = stored
  const prev: StoredResult | undefined = meta.previousAttemptId ? app.results.get(meta.previousAttemptId) : undefined
  const ttsReady = app.health?.components.tts?.ready ?? false
  const target = r.target_en ?? (meta.exerciseType === 'reading' || meta.exerciseType === 'shadowing' || meta.exerciseType === 'drill' ? meta.target?.en : undefined)
  const question = r.question_en ?? (meta.exerciseType === 'free_answer' ? meta.target?.en : undefined)
  const edited = r.revisions.some((v) => v.source !== 'asr')
  const audioOf = (kind: string) => r.model_audio?.find((a) => a.kind === kind)
  const modelSource = (() => {
    const a = audioOf('target') ?? audioOf('sample_answer') ?? audioOf('suggestion')
    if (a) return { text: a.text, url: a.url, textId: a.kind === 'target' ? (meta.target?.textId ?? undefined) : undefined }
    if (target) return { text: target, textId: meta.target?.textId }
    if (meta.sample) return { text: meta.sample }
    return null
  })()

  const reload = async () => {
    try {
      app.rememberResult({ result: await api.result(r.attempt_id), meta })
    } catch (e) {
      setReloadError(toApiError(e).messageKo)
    }
  }

  const showDiff = !!target && r.transcript != null
  const diffs = showDiff && target ? diffCount(target, r.transcript ?? '', r.target_diff) : 0
  const pron = r.pronunciation
  const retry = () => navigate('practice', meta.scenarioId, meta.opts, meta.exerciseType, meta.itemId ?? '', r.attempt_id)

  return (
    <main className="page narrow" aria-labelledby="res-title">
      <header className="page-head">
        <h1 id="res-title">이렇게 들렸어요</h1>
        <p className="page-meta">
          <Badge tone="brand">{TYPE_LABEL[meta.exerciseType]}</Badge> <Badge>{sc.title_ko}</Badge>
        </p>
        {question ? (
          <p className="lead" lang="en">
            Q. {question}
          </p>
        ) : (
          target &&
          !showDiff && (
            <p className="lead">
              목표 문장 · <span lang="en">{target}</span>
            </p>
          )
        )}
      </header>

      <Transcript key={r.transcript_revision} r={r} onEdited={() => void reload()} />
      {reloadError && <Notice tone="danger" icon="alert">{reloadError}</Notice>}

      {showDiff && target && (
        <section className="card">
          <WordDiff target={target} heard={r.transcript ?? ''} serverOps={r.target_diff} level="h2" />
        </section>
      )}

      {pron && pron.status !== 'unavailable' && (
        <PronunciationCard attemptId={r.attempt_id} p={pron} modelUrl={pron.prosody.model ? audioOf('target')?.url : undefined} canSaveReview={app.settings.history_opt_in} />
      )}

      {meta.exerciseType === 'roleplay_turn' && (
        <section className="card ai-reply" aria-labelledby="ai-h">
          <h2 id="ai-h">AI의 다음 말</h2>
          {r.next_ai?.text ? (
            <>
              <p lang="en">{r.next_ai.text}</p>
              <ModelAudio voiceId={voiceId} source={{ text: r.next_ai.text, url: audioOf('next_ai')?.url }} ttsReady={ttsReady} label="AI 말 듣기" />
            </>
          ) : (
            <Notice tone="warn">{r.no_speech ? '대답이 들리지 않아 AI가 이어 말하지 않았어요.' : '대화 모델이 준비되지 않아 다음 말을 만들지 못했어요.'}</Notice>
          )}
          <button type="button" className="btn btn-primary" onClick={() => navigate('practice', meta.scenarioId, meta.opts, 'roleplay_turn')}>
            {r.next_ai?.text ? '이어서 대답하기' : '다시 대답하기'}
          </button>
          <p className="muted small">지금까지 {roleplayThread(sc).turns.filter((t) => t.who === 'me').length}번 대답했어요.</p>
        </section>
      )}

      <section aria-labelledby="fb-h">
        <h2 id="fb-h" className="section-title">
          개선 제안{' '}
          {r.feedback_revision != null && (
            <span className="muted small">— 전사 {r.feedback_revision === 1 ? '원문' : `수정본 ${r.feedback_revision}`} 기준</span>
          )}
        </h2>
        {r.feedback.length === 0 &&
          (r.feedback_status && r.feedback_status !== 'ok' ? (
            <Notice tone="warn">문장 피드백을 만들지 못했어요. {app.health?.modes.recorded.feedback_available === false ? '대화 모델이 준비되지 않았어요.' : '전사가 불확실하면 피드백을 보류해요. 전사를 확인해 주세요.'}</Notice>
          ) : (
            <Notice>{diffs > 0 ? '문장 제안은 없어요. 위의 다르게 인식된 부분을 목표 문장과 비교해 보세요.' : '고칠 점을 찾지 못했어요.'}</Notice>
          ))}
        {r.feedback.map((f) => (
          <FeedbackCard key={f.feedback_id} f={f} />
        ))}
      </section>

      <section className="card" aria-labelledby="m-h">
        <h2 id="m-h">참고 지표</h2>
        <MetricsView m={r.metrics} edited={edited} />
      </section>

      {modelSource && meta.exerciseType !== 'roleplay_turn' && (
        <section className="card" aria-labelledby="model-h">
          <h2 id="model-h">모범 음성</h2>
          <p lang="en" className="model-text">
            {modelSource.text}
          </p>
          {!target && <p className="muted small">예시 문장이에요. 내 표현이 달라도 틀린 것이 아니에요.</p>}
          <ModelAudio voiceId={voiceId} source={modelSource} ttsReady={ttsReady} hideLabel />
        </section>
      )}

      {prev && (
        <section className="card" aria-labelledby="cmp-h">
          <h2 id="cmp-h">이전 시도와 비교</h2>
          <div className="compare-grid">
            <div>
              <span className="quote-label">이전</span>
              <p lang="en">{prev.result.transcript || '—'}</p>
              <p className="muted small">
                분당 단어 {prev.result.metrics?.wpm != null ? Math.round(prev.result.metrics.wpm) : '—'} · 쉰 횟수 {prev.result.metrics?.pause_count ?? '—'} ·
                단어 {prev.result.metrics?.word_count ?? '—'}개
              </p>
            </div>
            <div>
              <span className="quote-label">이번</span>
              <p lang="en">{r.transcript || '—'}</p>
              <p className="muted small">
                분당 단어 {r.metrics?.wpm != null ? Math.round(r.metrics.wpm) : '—'} · 쉰 횟수 {r.metrics?.pause_count ?? '—'} · 단어{' '}
                {r.metrics?.word_count ?? '—'}개
              </p>
            </div>
          </div>
          <p className="muted small">표현이 달라도 실패가 아니에요. 측정할 수 있는 항목만 나란히 보여 줘요.</p>
        </section>
      )}

      {pron?.status === 'experimental_banded' ? (
        <p className="pron-note">정식 발음 평가는 제공되지 않습니다. 위의 단어 표시는 검증 전 실험 기능이에요.</p>
      ) : (
        <PronunciationNote />
      )}
      {pron?.status === 'unavailable' && !r.no_speech && <PronunciationUnavailable p={pron} reason={unavailableReason(pron.reason)} />}

      <div className="page-actions">
        {meta.exerciseType !== 'roleplay_turn' && (
          <button type="button" className="btn btn-primary" onClick={retry}>
            <Icon name="replay" /> 다시 시도
          </button>
        )}
        <a className="btn" href={`#/practice/${encodeURIComponent(meta.scenarioId)}/${encodeURIComponent(meta.opts)}`}>
          다른 연습
        </a>
        <a className="btn btn-ghost" href="#/home">
          학습 홈
        </a>
      </div>
    </main>
  )
}
