import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Icon, type IconName } from '../components/Icon'
import { ActionButton, Badge, LevelMeter, Notice } from '../components/ui'
import { api } from '../lib/api'
import type { Caption } from '../lib/captions'
import { toApiError } from '../lib/errors'
import type { Difficulty, Scenario } from '../lib/types'
import { RealtimeSession, type HintView, type ServerState, type Snapshot } from '../session/realtimeSession'
import { navigate, useApp } from '../state'
import { DIFFICULTY_LABEL } from './Home'

const STATE_TEXT: Record<ServerState, [string, IconName, string]> = {
  CONNECTING: ['연결 중', 'circle', 'neutral'],
  READY: ['준비됨', 'check', 'neutral'],
  LISTENING: ['듣는 중', 'mic', 'listen'],
  FINALIZING: ['인식 마무리 중', 'circle', 'think'],
  RESPONDING: ['AI 응답 중', 'speaker', 'speak'],
  INTERRUPTING: ['끊는 중', 'stop', 'think'],
  PAUSED: ['일시정지', 'pause', 'neutral'],
  RECOVERABLE_ERROR: ['오류 — 다시 시도 가능', 'alert', 'warn'],
  CLOSED: ['종료됨', 'close', 'neutral'],
}

const HINT_STEP = ['한국어 뜻', '핵심 단어', '예시 문장'] as const

function StateChip({ s }: { s: Snapshot }) {
  const [text, icon, tone] = STATE_TEXT[s.state] ?? STATE_TEXT.READY
  return (
    <span className={`state-chip tone-${tone}`} role="status" aria-live="polite">
      <Icon name={icon} size={16} /> {text}
    </span>
  )
}

function MicStatus({ s, onToggleMute, onPtt }: { s: Snapshot; onToggleMute(): void; onPtt(pressed: boolean): void }) {
  if (s.pushToTalk) {
    return (
      <button
        type="button"
        className={`mic-orb ${s.pttPressed ? 'is-live' : ''}`}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId)
          onPtt(true)
        }}
        onPointerUp={() => onPtt(false)}
        onPointerCancel={() => onPtt(false)}
        onKeyDown={(e) => {
          if ((e.key === ' ' || e.key === 'Enter') && !e.repeat) {
            e.preventDefault()
            onPtt(true)
          }
        }}
        onKeyUp={(e) => {
          if (e.key === ' ' || e.key === 'Enter') onPtt(false)
        }}
        aria-pressed={s.pttPressed}
        aria-label="누르고 있는 동안 말하기"
      >
        <Icon name={s.pttPressed ? 'mic' : 'hand'} size={32} />
        <span className="orb-text">
          <span className="orb-label">{s.pttPressed ? '말하는 중…' : '누르고 말하기'}</span>
          <span className="orb-hint">{s.pttPressed ? '떼면 제출돼요' : '누르고 있는 동안만 들어요'}</span>
        </span>
      </button>
    )
  }
  const [label, hint, icon] = s.paused
    ? ['일시정지됨', '다시 시작을 누르면 이어져요', 'pause' as const]
    : s.muted
      ? ['마이크 꺼짐', '눌러서 켜기', 'micOff' as const]
      : s.aiSpeaking
        ? ['AI가 말하는 중', s.autoBargeIn ? '말하면 바로 끼어들 수 있어요' : 'Esc로 멈출 수 있어요', 'speaker' as const]
        : ['듣고 있어요', '눌러서 음소거', 'mic' as const]
  return (
    <button
      type="button"
      className={`mic-orb ${!s.muted && !s.paused ? 'is-live' : 'is-muted'} ${s.aiSpeaking ? 'is-ai' : ''}`}
      onClick={onToggleMute}
      disabled={s.paused || s.state === 'CLOSED'}
      aria-pressed={s.muted}
      aria-label={s.muted ? '마이크 켜기' : '마이크 음소거'}
      aria-keyshortcuts="M"
    >
      <Icon name={icon} size={32} />
      <span className="orb-text">
        <span className="orb-label">{label}</span>
        <span className="orb-hint">{hint}</span>
      </span>
    </button>
  )
}

function CaptionList({ captions }: { captions: Caption[] }) {
  const box = useRef<HTMLDivElement>(null)
  const pinned = useRef(true)
  const [unseen, setUnseen] = useState(false)
  const toBottom = useCallback((smooth: boolean) => {
    const el = box.current
    if (!el) return
    el.scrollTo({ top: el.scrollHeight, behavior: smooth && !matchMedia('(prefers-reduced-motion: reduce)').matches ? 'smooth' : 'auto' })
    pinned.current = true
    setUnseen(false)
  }, [])
  // Follow new captions only while the learner is already at the bottom; never scroll the page itself.
  useEffect(() => {
    if (pinned.current) toBottom(false)
    else setUnseen(true)
  }, [captions, toBottom])
  // The box shrinks when a hint or notice opens (or the window resizes): stay pinned to the latest line.
  useEffect(() => {
    const el = box.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => {
      if (pinned.current) el.scrollTop = el.scrollHeight
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const onScroll = () => {
    const el = box.current
    if (!el) return
    pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80
    if (pinned.current) setUnseen(false)
  }
  return (
    <div className="captions-wrap">
      <div ref={box} className="captions" role="region" aria-label="대화 자막" onScroll={onScroll} tabIndex={0}>
        {captions.length === 0 && <p className="muted center captions-empty">AI가 먼저 말을 걸어요. 편하게 대답해 보세요.</p>}
        {captions.map((c) => {
          if (c.kind === 'user') {
            return (
              <div key={c.turn_id} className={`bubble user ${c.final ? 'final' : 'partial'}`}>
                <span className="who">{c.final ? '나' : '나 · 인식 중'}</span>
                <p lang="en">{c.text || '…'}</p>
              </div>
            )
          }
          const shown = c.segments.filter((x) => x.status !== 'dropped')
          const stopAt = shown.findIndex((x) => x.status === 'interrupted')
          return (
            <div key={c.response_id} className={`bubble ai ${c.state === 'cancelled' ? 'cancelled' : ''}`}>
              <span className="who">AI{c.state === 'cancelled' ? ' · 말하다 멈춤' : ''}</span>
              <p lang="en">
                {shown.map((x, i) => (
                  <span key={x.segment_id} className={`seg-${x.status}`}>
                    {i === stopAt && (
                      <span className="stop-mark" lang="ko" aria-hidden="true">
                        여기서 멈춤
                      </span>
                    )}
                    {x.text}
                    {x.status === 'interrupted' && <span className="sr-only"> (여기서 끊김)</span>}{' '}
                  </span>
                ))}
              </p>
            </div>
          )
        })}
      </div>
      {unseen && (
        <button type="button" className="new-caption" onClick={() => toBottom(true)}>
          <Icon name="arrowDown" size={16} /> 새 대화
        </button>
      )}
    </div>
  )
}

/** The gateway prefixes a level-1 hint with its Korean rendering of the AI line: `상대방: “…”\n<hint>`. */
function splitHint(text?: string): { gloss: string | null; body: string } {
  const [first = '', ...rest] = (text ?? '').split('\n')
  if (!first.startsWith('상대방:')) return { gloss: null, body: text ?? '' }
  return { gloss: first.replace(/^상대방:\s*/, '').replace(/^[“"]|[”"]$/g, '').trim() || null, body: rest.join('\n').trim() }
}

function HintPanel({
  hint,
  level,
  aiLine,
  gloss,
  glossPending,
  collapsible,
  open,
  onToggle,
}: {
  hint: HintView
  level: number
  aiLine: string
  gloss: string | null
  glossPending: boolean
  collapsible: boolean
  open: boolean
  onToggle(): void
}) {
  const body = splitHint(hint.text_ko).body
  const title = (
    <>
      <Icon name="hint" size={18} /> 힌트 {level}/3 · {HINT_STEP[level - 1]}
    </>
  )
  const shown = !collapsible || open
  return (
    <div className={`hint-panel ${shown ? '' : 'is-collapsed'}`} aria-live="polite">
      {collapsible ? (
        <h2 className="hint-title">
          <button type="button" className="hint-toggle" aria-expanded={open} aria-controls="hint-body" onClick={onToggle}>
            <span className="hint-toggle-label">{title}</span>
            {!open && body && <span className="hint-preview">{body}</span>}
            <Icon name="chevronDown" size={18} className="hint-chevron" />
            <span className="sr-only">{open ? ' 접기' : ' 펼치기'}</span>
          </button>
        </h2>
      ) : (
        <h2 className="hint-title">{title}</h2>
      )}
      {shown && (
        <div id="hint-body" className="hint-body">
          {gloss && (
            <div className="hint-gloss">
              <span className="hint-label">상대방이 한 말</span>
              <p className="hint-gloss-en" lang="en">
                {aiLine}
              </p>
              <p>{gloss}</p>
            </div>
          )}
          {!gloss && glossPending && <p className="muted small">상대방 말의 한국어 뜻을 준비하고 있어요…</p>}
          {body && (
            <div>
              {gloss && <span className="hint-label">이렇게 이어 가 보세요</span>}
              <p>{body}</p>
            </div>
          )}
          {level >= 2 && hint.keywords && hint.keywords.length > 0 && (
            <p className="keywords" lang="en">
              {hint.keywords.map((k) => (
                <span key={k} className="kw">
                  {k}
                </span>
              ))}
            </p>
          )}
          {level >= 3 && hint.example_en && (
            <p className="example" lang="en">
              “{hint.example_en}”
            </p>
          )}
        </div>
      )}
    </div>
  )
}

function localHint(sc: Scenario, s: Snapshot, level: 1 | 2 | 3, responseId?: string): HintView | null {
  const pending = sc.goals.find((g) => !s.goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')) ?? sc.goals[0]
  const h = sc.hints.find((x) => x.goal_id === pending?.goal_id) ?? sc.hints[0]
  return h ? { level, text_ko: h.ko, keywords: h.keywords, example_en: h.example_en, source: 'local', response_id: responseId } : null
}

export function Talk({ scenarioId, opts }: { scenarioId: string; opts: string }) {
  const app = useApp()
  const sc = app.scenario(scenarioId)
  const [difficultyRaw, optRaw] = (opts ?? '').split(':')
  const difficulty = (['easy', 'normal', 'hard'].includes(difficultyRaw ?? '') ? difficultyRaw : app.settings.difficulty) as Difficulty
  const historyOptIn = optRaw ? optRaw === '1' : app.settings.history_opt_in
  const [session, setSession] = useState<RealtimeSession | null>(null)
  const [starting, setStarting] = useState(false)
  const [startError, setStartError] = useState<string | null>(null)
  const [ending, setEnding] = useState(false)
  const [hintLevel, setHintLevel] = useState(0)

  useEffect(() => () => session?.shutdown(), [session])

  const begin = async () => {
    if (!sc) return
    setStarting(true)
    setStartError(null)
    let s: RealtimeSession | null = null
    try {
      const { session_id } = await api.createSession({ mode: 'realtime', scenario_id: sc.scenario_id, difficulty, history_opt_in: historyOptIn, feedback_policy: 'session_end' })
      s = new RealtimeSession(session_id, { autoBargeIn: app.settings.auto_barge_in })
      await s.start(app.micDeviceId || undefined, app.settings.silence_ms)
      setSession(s)
    } catch (e) {
      s?.shutdown()
      setStartError(toApiError(e).messageKo)
    } finally {
      setStarting(false)
    }
  }

  const finish = async () => {
    if (!session || !sc) return
    setEnding(true)
    session.shutdown()
    let summary = null
    let error = null
    try {
      summary = await api.endSession(session.sessionId)
    } catch (e) {
      error = toApiError(e).messageKo
    }
    const said = session.snapshot.captions.flatMap((c) => (c.kind === 'user' && c.final && c.text.trim() ? [c.text.trim()] : []))
    app.setLastSession({ sessionId: session.sessionId, scenarioId: sc.scenario_id, opts: `${difficulty}:${historyOptIn ? 1 : 0}`, said, summary, error, historyOptIn, endedAt: Date.now() })
    navigate('summary')
  }

  if (!sc) {
    return (
      <main className="page">
        <Notice tone="warn">시나리오를 찾을 수 없어요.</Notice>
        <a className="btn" href="#/home">홈으로</a>
      </main>
    )
  }

  if (!session) {
    const rt = app.health?.modes.realtime
    return (
      <main className="page narrow" aria-labelledby="talk-title">
        <header className="page-head">
          <h1 id="talk-title">{sc.title_ko}</h1>
          <p className="page-meta">
            <Badge tone="brand">실시간 회화</Badge> <Badge>난이도 {DIFFICULTY_LABEL[difficulty]}</Badge> <Badge>{historyOptIn ? '기록 저장' : '기록 저장 안 함'}</Badge>
          </p>
          <p className="lead">{sc.setting_ko}</p>
        </header>
        <RoleCard sc={sc} goals={[]} />
        <Notice icon="headset">헤드셋을 쓰면 AI 목소리가 마이크로 다시 들어가지 않아 더 자연스럽게 대화할 수 있어요.</Notice>
        {startError && <Notice tone="danger" icon="alert">{startError}</Notice>}
        <div className="page-actions">
          <ActionButton
            className="btn btn-primary btn-lg"
            icon="mic"
            onClick={() => void begin()}
            disabledReason={starting ? '연결하는 중…' : rt && !rt.available ? rt.reason_ko ?? '실시간 회화를 지금 사용할 수 없어요' : !app.health ? '모델 상태를 확인하는 중이에요' : null}
          >
            대화 시작
          </ActionButton>
          <a className="btn btn-ghost" href="#/home">
            돌아가기
          </a>
        </div>
      </main>
    )
  }

  return <LiveTalk sc={sc} session={session} ending={ending} onEnd={() => void finish()} hintLevel={hintLevel} setHintLevel={setHintLevel} />
}

function GoalList({ sc, goals }: { sc: Scenario; goals: Snapshot['goals'] }) {
  return (
    <ol className="goals">
      {sc.goals.map((g) => {
        const done = goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')
        return (
          <li key={g.goal_id} className={done ? 'done' : ''}>
            <span className="goal-mark">
              <Icon name={done ? 'check' : 'circle'} size={16} />
            </span>
            <span>
              {g.ko}
              <span className="sr-only">{done ? ' — 달성' : ' — 아직'}</span>
              <span className="muted small block" lang="en">
                {g.en}
              </span>
            </span>
          </li>
        )
      })}
    </ol>
  )
}

function Roles({ sc }: { sc: Scenario }) {
  return (
    <p className="roles">
      <span>
        AI · <strong>{sc.ai_role_ko}</strong>
      </span>
      <span>
        나 · <strong>{sc.user_role_ko}</strong>
      </span>
    </p>
  )
}

function RoleCard({ sc, goals }: { sc: Scenario; goals: Snapshot['goals'] }) {
  return (
    <section className="card role-card" aria-labelledby="goals-h">
      <Roles sc={sc} />
      <h2 id="goals-h" className="card-title">
        이번 대화 목표
      </h2>
      <GoalList sc={sc} goals={goals} />
    </section>
  )
}

/** Narrow screens: goals fold into one line so the conversation stays in the first viewport. */
function RoleFold({ sc, goals }: { sc: Scenario; goals: Snapshot['goals'] }) {
  const done = sc.goals.filter((g) => goals.some((x) => x.goal_id === g.goal_id && x.status === 'done')).length
  return (
    <details className="role-fold">
      <summary>
        <span>
          목표 <strong>{done}/{sc.goals.length}</strong> 달성
        </span>
        <span className="muted small">
          AI · {sc.ai_role_ko} / 나 · {sc.user_role_ko}
        </span>
      </summary>
      <GoalList sc={sc} goals={goals} />
    </details>
  )
}

function useWide(query: string): boolean {
  const get = () => matchMedia(query).matches
  return useSyncExternalStore(
    (fn) => {
      const m = matchMedia(query)
      m.addEventListener('change', fn)
      return () => m.removeEventListener('change', fn)
    },
    get,
  )
}

export function LiveTalk({
  sc,
  session,
  ending,
  onEnd,
  hintLevel,
  setHintLevel,
}: {
  sc: Scenario
  session: RealtimeSession
  ending: boolean
  onEnd(): void
  hintLevel: number
  setHintLevel(n: number): void
}) {
  const s = useSyncExternalStore(session.subscribe, () => session.snapshot)
  const getLevel = useCallback(() => session.micLevel, [session])
  const wide = useWide('(min-width: 821px)')

  // New AI turn → hint ladder starts again. Hints (and the translation) belong to this newest AI line only.
  const lastAiCaption = [...s.captions].reverse().find((c) => c.kind === 'ai')
  const lastAi = lastAiCaption?.response_id
  const aiLine = lastAiCaption ? lastAiCaption.segments.filter((x) => x.status !== 'dropped').map((x) => x.text).join(' ') : ''
  const [gloss, setGloss] = useState<{ id: string; ko: string } | null>(null)
  const [glossWaitFor, setGlossWaitFor] = useState<string | null>(null)
  const [hintOpen, setHintOpen] = useState(false)
  useEffect(() => setHintLevel(0), [lastAi, setHintLevel])
  // Keep the level-1 translation for this line while the learner climbs to levels 2 and 3.
  useEffect(() => {
    const h = s.hint
    if (!h || h.source !== 'server') return
    if (h.response_id === lastAi && lastAi) {
      const g = splitHint(h.text_ko).gloss
      if (g) setGloss({ id: lastAi, ko: g })
    }
    // Any server reply ends the wait: a reply for another line, or one without a line id, carries no usable translation.
    setGlossWaitFor(null)
  }, [s.hint, lastAi])

  const nextHint = () => {
    const level = Math.min(3, hintLevel + 1) as 1 | 2 | 3
    setHintLevel(level)
    const local = localHint(sc, s, level, lastAi)
    if (local) session.showLocalHint(local)
    if (level === 1 && lastAi && gloss?.id !== lastAi) setGlossWaitFor(lastAi)
    session.requestHint(level)
  }
  const shownHint = s.hint && hintLevel > 0 && (!s.hint.response_id || !lastAi || s.hint.response_id === lastAi) ? s.hint : null

  const closed = s.state === 'CLOSED'
  const canStop = s.aiSpeaking || s.responseActive
  const stopReason = !canStop ? 'AI가 말하고 있을 때 쓸 수 있어요' : null

  // Keyboard shortcuts (ignored while typing or when a control has focus for Space/Enter).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement
      if (t.closest('input, textarea, select') || e.metaKey || e.ctrlKey || e.altKey || closed) return
      const k = e.key.toLowerCase()
      if (k === 'escape') session.interrupt()
      else if (k === 'm') session.setMuted(!session.snapshot.muted)
      else if (k === 'h') nextHint()
      else if (k === 'r') session.replayLast()
      else if (k === ' ' && t === document.body) {
        e.preventDefault()
        if (session.snapshot.pushToTalk) {
          if (!e.repeat) session.pushToTalk(true)
        } else session.commit()
      }
    }
    const onUp = (e: KeyboardEvent) => {
      if (e.key === ' ' && session.snapshot.pushToTalk) session.pushToTalk(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('keyup', onUp)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('keyup', onUp)
    }
  })

  return (
    <main className="talk is-live" aria-labelledby="live-title">
      <header className="talk-head">
        <h1 id="live-title">{sc.title_ko}</h1>
        <StateChip s={s} />
        {/* Explicit name: the visually shortened mobile label must not change what assistive tech hears. */}
        <button type="button" className="btn btn-danger-soft talk-end" onClick={onEnd} disabled={ending} aria-label={ending ? '요약 만드는 중' : '종료하고 요약 보기'}>
          <Icon name="close" />
          {ending ? (
            '요약 만드는 중…'
          ) : (
            <span aria-hidden="true">
              종료<span className="end-long">하고 요약 보기</span>
            </span>
          )}
        </button>
      </header>

      <div className="talk-body">
        {wide && (
          <aside className="talk-side">
            <RoleCard sc={sc} goals={s.goals} />
          </aside>
        )}

        <section className="talk-main" aria-label="대화">
          {!wide && <RoleFold sc={sc} goals={s.goals} />}
          {s.error && (
            <Notice tone={s.error.recoverable ? 'warn' : 'danger'} icon="alert">
              {s.error.message}{' '}
              {s.error.code === 'LLM_FAILED' && (
                <button type="button" className="link" onClick={() => session.retryResponse()}>
                  AI 답변 다시 받기
                </button>
              )}{' '}
              {s.error.recoverable && (
                <button type="button" className="link" onClick={() => session.clearError()}>
                  닫기
                </button>
              )}
            </Notice>
          )}
          {s.warning && <Notice tone="warn">{s.warning}</Notice>}
          {s.backlogged && (
            <Notice tone="warn" icon="alert">
              음성 전송이 2초 넘게 밀리고 있어요. 말한 내용이 늦게 전달돼요. 잠시 멈춘 뒤 다시 시작하거나, 계속되면 회화를 끝내고 녹음 연습을 이용해 주세요.{' '}
              {!s.paused && (
                <button type="button" className="link" onClick={() => session.setPaused(true)}>
                  일시정지
                </button>
              )}
            </Notice>
          )}
          {s.echoCount > 0 && !s.pushToTalk && (
            <Notice tone="warn" icon="headset">
              {s.echoCount >= 2 ? 'AI 목소리가 계속 마이크로 들어와서 자동 끼어들기를 껐어요. ' : 'AI 목소리가 마이크로 다시 들어간 것 같아요. 헤드셋을 권장해요. '}
              <button type="button" className="link" onClick={() => session.setPushToTalkMode(true)}>
                눌러 말하기로 전환
              </button>
            </Notice>
          )}
          <CaptionList captions={s.captions} />
          {shownHint && (
            <HintPanel
              hint={shownHint}
              level={hintLevel}
              aiLine={aiLine}
              gloss={gloss && gloss.id === lastAi ? gloss.ko : null}
              glossPending={glossWaitFor !== null && glossWaitFor === lastAi}
              collapsible={!wide}
              open={hintOpen}
              onToggle={() => setHintOpen(!hintOpen)}
            />
          )}

          <div className="control-dock" role="group" aria-label="대화 조작">
            <div className="dock-primary">
              <MicStatus s={s} onToggleMute={() => session.setMuted(!s.muted)} onPtt={(p) => session.pushToTalk(p)} />
              <div className="dock-level">
                <span className="dock-level-label" aria-hidden="true">
                  내 목소리
                </span>
                <LevelMeter getLevel={getLevel} label="내 목소리 크기" />
              </div>
              <div className="dock-turn">
                {!s.pushToTalk && (
                  <ActionButton quietReason className="btn btn-soft" icon="send" onClick={() => session.commit()} disabledReason={closed ? '종료됨' : s.canCommit ? null : '말을 시작하면 누를 수 있어요'} aria-keyshortcuts="Space">
                    말하기 완료
                  </ActionButton>
                )}
                <ActionButton quietReason className={`btn btn-stop ${canStop ? 'is-armed' : ''}`} icon="stop" onClick={() => session.interrupt()} disabledReason={stopReason} aria-keyshortcuts="Escape">
                  멈추기(끼어들기)
                </ActionButton>
              </div>
            </div>
            <div className="dock-tools">
              <ActionButton quietReason className="btn btn-tool" icon="hint" onClick={nextHint} disabledReason={closed ? '종료됨' : hintLevel >= 3 ? '힌트를 모두 봤어요' : null} aria-keyshortcuts="H">
                힌트 {Math.min(3, hintLevel + 1)}/3<span className="hint-step"> · {HINT_STEP[Math.min(2, hintLevel)]}</span>
              </ActionButton>
              <ActionButton quietReason className="btn btn-tool" icon="replay" onClick={() => session.replayLast()} disabledReason={!session.canReplay ? '다시 들을 AI 말이 아직 없어요' : s.aiSpeaking ? 'AI가 말하는 중이에요' : null} aria-keyshortcuts="R">
                다시 듣기
              </ActionButton>
              <ActionButton quietReason className="btn btn-tool" icon="slow" onClick={() => session.setSlow(!s.slow)} aria-pressed={s.slow} disabledReason={closed ? '종료됨' : null}>
                더 천천히{s.slow && <span className="tool-state"> 켜짐</span>}
              </ActionButton>
              <ActionButton quietReason className="btn btn-tool" icon={s.paused ? 'play' : 'pause'} onClick={() => session.setPaused(!s.paused)} aria-pressed={s.paused} disabledReason={closed ? '종료됨' : null}>
                {s.paused ? '다시 시작' : '일시정지'}
              </ActionButton>
            </div>
            <p className="shortcuts muted small">
              <span className="sr-only">단축키: </span>
              <kbd>Space</kbd> {s.pushToTalk ? '누르고 말하기' : '말하기 완료'} · <kbd>Esc</kbd> 멈추기 · <kbd>M</kbd> 음소거 · <kbd>H</kbd> 힌트 · <kbd>R</kbd> 다시 듣기
            </p>
            {(s.slow || s.pushToTalk) && (
              <p className="dock-foot small muted">
                {s.slow && '더 천천히는 다음 문장부터 적용돼요. '}
                {s.pushToTalk && (
                  <button type="button" className="link" onClick={() => session.setPushToTalkMode(false)}>
                    자동 대화로 돌아가기
                  </button>
                )}
              </p>
            )}
          </div>
        </section>
      </div>
    </main>
  )
}
