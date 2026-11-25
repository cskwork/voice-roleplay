import { useEffect, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import { api } from '../lib/api'
import type { Difficulty } from '../lib/types'
import { navigate, useApp } from '../state'
import { BEAT, CallSession, dialAdvice, IDLE, runningContext, type CallPhase, type CallSnapshot, type Voice } from './callSession'
import './call.css'
import { pastCalls, rotateTopic } from './history'
import { Stage } from './stage'
import { TalkList } from './TalkList'

/** Opened inside another page of the same site (Content Forge): that page owns closing. */
const EMBEDDED = window.parent !== window

type CloseReason = 'done' | 'quit' | 'error'

function closeCall(reason: CloseReason): void {
  if (EMBEDDED) window.parent.postMessage({ source: 'voice-roleplay', type: 'call-closed', reason }, location.origin)
  else navigate('home')
}

export function Call({ callId, opts }: { callId: string; opts: string }) {
  const app = useApp()
  const [attempt, setAttempt] = useState(0)
  const raw = opts.split(':')[0] ?? ''
  const difficulty = (['easy', 'normal', 'hard'].includes(raw) ? raw : app.settings.difficulty) as Difficulty
  return <CallRun key={attempt} callId={callId} difficulty={difficulty} micDeviceId={app.micDeviceId} again={() => setAttempt(attempt + 1)} />
}

const noSubscribe = () => () => {}

const LABEL: Record<CallPhase, [shown: string, spoken: string, off: boolean]> = {
  dialing: ['잠깐만요', '전화를 거는 중', true],
  pickup: ['잠깐만요', '햄찌가 전화를 받는 중', true],
  speaking: ['잠깐만요', '햄찌가 말하는 중', true],
  locked: ['잠깐만요', '잠깐만요', true],
  turn: ['눌러서 말하기', '눌러서 말하기', false],
  listening: ['눌러서 보내기', '눌러서 보내기', false],
  sending: ['', '보내는 중', true],
  thinking: ['', '햄찌가 생각하는 중', true],
  retry: ['대답 다시 받기', '햄찌 대답 다시 받기', false],
  ended: ['잠깐만요', '통화가 끝났어요', true],
}

/** Which button closed a sheet ('' = Esc); reset so an old answer cannot leak into the next time. */
function chose(d: HTMLDialogElement): string {
  const value = d.returnValue
  d.returnValue = ''
  return value
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

function CallRun({ callId, difficulty, micDeviceId, again }: { callId: string; difficulty: Difficulty; micDeviceId: string; again(): void }) {
  const [session, setSession] = useState<CallSession | null>(null)
  const s = useSyncExternalStore(session?.subscribe ?? noSubscribe, () => session?.snapshot ?? IDLE)
  const [needTap, setNeedTap] = useState(false)
  const [captions, setCaptions] = useState(true)
  const [gloss, setGloss] = useState(true)
  const [leaving, setLeaving] = useState(false)
  const [review, setReview] = useState(false)
  const callingFace = useRef<HTMLDivElement>(null)
  const quitFace = useRef<HTMLDivElement>(null)
  const ham = useRef<HTMLDivElement>(null)
  const caption = useRef<HTMLSpanElement>(null)
  const speak = useRef<HTMLButtonElement>(null)
  const settings = useRef<HTMLDialogElement>(null)
  const quit = useRef<HTMLDialogElement>(null)
  const startRef = useRef<() => Promise<void>>(async () => {})
  const stageRef = useRef<Stage | null>(null)

  useEffect(() => {
    const call = new CallSession(callId, difficulty, micDeviceId)
    const small = new Stage(callingFace.current!, 152, 'ring')
    const stage = new Stage(ham.current!, 460)
    stageRef.current = stage
    let alive = true
    let before: CallPhase = 'dialing'
    const timers = new Set<ReturnType<typeof setTimeout>>()
    const later = (ms: number, when: CallPhase, f: () => void) => {
      const t = setTimeout(() => {
        timers.delete(t)
        if (alive && call.snapshot.phase === when) f()
      }, ms)
      timers.add(t)
    }
    const words = () => [...(caption.current?.querySelectorAll('i') ?? [])]
    const reveal = (voice: Voice) => () => {
      const t = voice.position()
      const shown = voice.words().filter((w) => w <= t + 0.04).length
      words().forEach((el, i) => el.classList.toggle('on', i < shown))
    }
    call.view = {
      line(voice, act) {
        stage.speak(voice.mouth, act)
        stage.onFrame = reveal(voice)
      },
      lineEnd() {
        words().forEach((el) => el.classList.add('on'))
        stage.onFrame = null
        stage.quiet()
      },
      phase(p) {
        if (p === 'locked' && call.snapshot.turn < call.snapshot.turns) stage.idle()
        if (p === 'turn' && before === 'sending') later(BEAT.react, 'turn', () => stage.wait())
        if (p === 'thinking') later(120, 'thinking', () => stage.think())
        if (p === 'listening') {
          later(BEAT.react, 'listening', () => stage.listen())
          let heard = 0
          let nodAt = performance.now() + 1500
          let floor = 0.008
          stage.onFrame = () => {
            const level = call.micLevel
            speak.current?.style.setProperty('--lv', Math.min(1, level / 0.16).toFixed(2))
            floor += (Math.min(level, floor * 3) - floor) * 0.02
            const now = performance.now()
            if (level > Math.max(0.012, floor * 2.4)) heard = now
            else if (heard && now - heard > 380 && now > nodAt) {
              // The learner paused: a small nod says "go on".
              stage.nod()
              nodAt = now + 2600
              heard = 0
            }
          }
        } else if (before === 'listening') {
          stage.onFrame = null
          speak.current?.style.removeProperty('--lv')
        }
        if (p === 'ended') {
          stage.onFrame = null
          stage.quiet()
          const { endedBy, lines, title } = call.snapshot
          if (lines.some((l) => l.who === 'me') && title) pastCalls.add({ title, at: new Date(), lines })
          if (endedBy === 'bye') {
            rotateTopic()
            stage.play('press')
            setLeaving(true)
            later(BEAT.out, 'ended', () => setReview(true))
          } else if (lines.length) setReview(true)
          else closeCall(endedBy === 'quit' ? 'quit' : 'error')
        }
        before = p
      },
    }
    let starting = false
    startRef.current = async () => {
      if (starting) return // [전화 걸기] pressed twice while the sound is being opened
      starting = true
      const startedAt = performance.now()
      const ctx = await runningContext().catch((e: unknown) => call.failed(e))
      starting = false
      if (!alive || ctx === undefined) return
      setNeedTap(!ctx)
      if (!ctx) return
      try {
        await call.dial(ctx, startedAt)
        if (!alive) return
        call.startPickup()
        small.stop()
        await sleep(BEAT.fade)
        await stage.pickUp()
        call.pickedUp()
      } catch {
        // Why is in the snapshot (or the learner hung up).
      }
    }
    setSession(call)
    const t = setTimeout(() => void startRef.current(), 0)
    return () => {
      alive = false
      clearTimeout(t)
      for (const x of timers) clearTimeout(x)
      call.dispose()
      small.stop()
      stage.stop()
    }
  }, [callId, difficulty, micDeviceId])

  // The character on the quit sheet only exists while the sheet is open.
  const askQuit = () => {
    const d = quit.current!
    const face = new Stage(quitFace.current!, 128, 'waitTilt')
    d.showModal()
    d.addEventListener('close', () => face.stop(), { once: true })
  }

  useEffect(() => {
    if (review) stageRef.current?.stop()
  }, [review])

  const dialing = s.phase === 'dialing'
  const [shown, spoken, off] = LABEL[s.phase]
  const progress = s.phase === 'ended' && s.endedBy === 'bye' ? 100 : 10 + (s.turn / s.turns) * 80

  return (
    <div className="hc hc-run" data-screen={review ? 'review' : dialing ? 'calling' : 'call'}>
      <div className="hc-app">
        <main className={`hc-screen hc-calling ${dialing ? '' : 'off'} ${dialing && !needTap && !s.error ? 'is-ringing' : ''}`} inert={!dialing} aria-labelledby="hc-calling-title">
          <div className="ring">
            <div className="hc-face" ref={callingFace} aria-hidden="true" />
          </div>
          <div className="words" role="status">
            <h1 id="hc-calling-title">{s.error ? '전화를 걸지 못했어요' : needTap ? '전화 걸 준비가 됐어요' : '햄찌에게 전화하는 중…'}</h1>
            <p>{s.error ? dialAdvice(s.error) : s.title ? `오늘은 ${s.title.ko} 이야기` : needTap ? '아래 버튼을 누르면 전화를 걸어요.' : '마이크를 허용하면 햄찌가 받아요.'}</p>
          </div>
          <div className="acts">
            {s.error ? (
              <button type="button" className="hc-btn" onClick={again}>
                다시 걸기
              </button>
            ) : (
              needTap && (
                <button type="button" className="hc-btn" onClick={() => void startRef.current()}>
                  전화 걸기
                </button>
              )
            )}
            <button type="button" className="hang" onClick={() => (session?.quit(), closeCall(s.error ? 'error' : 'quit'))}>
              <HangIcon />
              <span>{s.error ? '닫기' : '전화 끊기'}</span>
            </button>
          </div>
        </main>

        <main className={`hc-screen hc-call ${leaving ? 'out' : ''}`} data-state={s.phase} inert={dialing || review} aria-labelledby="hc-call-title">
          <h1 className="sr-only" id="hc-call-title">
            햄찌와 통화 중
          </h1>
          <div className="stage">
            <div className="room" aria-hidden="true" />
            <div className="ham" ref={ham} aria-hidden="true" />
            <div className="top">
              <button type="button" className="chip-btn" onClick={() => settings.current?.showModal()}>
                <GearIcon />
                <span>설정</span>
              </button>
              <div className="bar" role="progressbar" aria-label="통화 진행" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(progress)}>
                <span style={{ inlineSize: `${progress}%` }} />
              </div>
            </div>
            {captions && s.line && (
              <div className="caption">
                {gloss && (
                  <span className="gloss" lang="ko">
                    {s.line.ko ?? ' '}
                  </span>
                )}
                <span className="line" lang="en" ref={caption} key={s.line.key}>
                  {s.line.en.split(' ').map((w, i) => (
                    <span key={i}>
                      {i > 0 && ' '}
                      <i>{w}</i>
                    </span>
                  ))}
                </span>
              </div>
            )}
            <p className="tip" role="status">
              {s.tip}
            </p>
          </div>
          <div className="controls">
            <button type="button" className="side" aria-disabled={!session?.canReplay} onClick={() => session?.replayLast()}>
              <ReplayIcon />
              <span>다시 듣기</span>
            </button>
            <SpeakButton refTo={speak} off={off} label={spoken} phase={s.phase} press={() => session?.press()}>
              {shown}
            </SpeakButton>
            <button type="button" className="side" aria-pressed={captions} onClick={() => setCaptions(!captions)}>
              <CcIcon />
              <span>자막</span>
            </button>
          </div>
          <p className="sr-only" role="status">
            {s.line && `햄찌: ${s.line.en} ${s.line.ko ?? ''}`}
          </p>
        </main>

        {review && session && <Review s={s} session={session} gloss={gloss} />}
      </div>

      <dialog ref={settings} className="hc-sheet" aria-labelledby="hc-set-title" onClose={(e) => chose(e.currentTarget) === 'end' && askQuit()}>
        <h1 id="hc-set-title">설정</h1>
        <Switch label="우리말 뜻 보기" on={gloss} set={setGloss} />
        <Switch label="천천히 말하기" note="다음 말부터 바뀌어요" on={s.slow} set={(on) => session?.setSlow(on)} />
        <form method="dialog">
          <button className="hc-btn" autoFocus>
            다 됐어요
          </button>
          <button className="hc-btn text danger" value="end">
            통화 끝내기
          </button>
        </form>
      </dialog>

      <dialog ref={quit} className="hc-sheet dark" aria-labelledby="hc-quit-title" onClose={(e) => chose(e.currentTarget) === 'quit' && session?.quit()}>
        <div className="hc-face" ref={quitFace} aria-hidden="true" />
        <h1 id="hc-quit-title">햄찌와 통화를 끝낼까요?</h1>
        <form method="dialog">
          <button className="hc-btn" autoFocus>
            계속 통화하기
          </button>
          <button className="hc-btn text danger" value="quit">
            끊기
          </button>
        </form>
      </dialog>
    </div>
  )
}

/** Tap to start, tap to send; or hold while speaking and let go to send. */
function SpeakButton({ refTo, off, label, phase, press, children }: { refTo: React.RefObject<HTMLButtonElement | null>; off: boolean; label: string; phase: CallPhase; press(): void; children: ReactNode }) {
  const hold = useRef({ timer: 0, held: false, eat: false })
  const up = () => {
    const h = hold.current
    clearTimeout(h.timer)
    if (!h.held) return
    h.held = false
    press()
  }
  useEffect(() => {
    // The finger may leave the button before it is lifted: a press that was still too short must not start listening.
    const onUp = () => {
      clearTimeout(hold.current.timer)
      if (hold.current.held) setTimeout(up)
    }
    window.addEventListener('pointerup', onUp)
    window.addEventListener('pointercancel', onUp)
    return () => {
      window.removeEventListener('pointerup', onUp)
      window.removeEventListener('pointercancel', onUp)
    }
  })
  return (
    <button
      type="button"
      ref={refTo}
      className="hc-btn speak"
      aria-disabled={off}
      aria-label={label}
      onPointerDown={(e) => {
        const h = hold.current
        h.eat = false // a hold let go outside the button leaves no click to eat
        if (e.button || phase !== 'turn') return
        h.held = false
        h.timer = window.setTimeout(() => {
          h.held = true
          h.eat = true
          press()
        }, 350)
      }}
      onPointerUp={up}
      onPointerCancel={up}
      onClick={(e) => {
        const h = hold.current
        clearTimeout(h.timer)
        const ate = h.eat && e.detail > 0 // a key press is never the end of a hold
        h.eat = false
        if (!ate && !off) press()
      }}
    >
      <span className="ico" aria-hidden="true">
        <MicIcon />
        <span className="dots">
          <i />
          <i />
          <i />
        </span>
      </span>
      {children && <span className="lab">{children}</span>}
    </button>
  )
}

function Review({ s, session, gloss }: { s: CallSnapshot; session: CallSession; gloss: boolean }) {
  const title = useRef<HTMLHeadingElement>(null)
  const cache = useRef(new Map<string, string | null>())
  useEffect(() => title.current?.focus({ preventScroll: true }), [])
  const mine = s.lines.filter((l) => l.who === 'me').length
  const lines = gloss ? s.lines : s.lines.map((l) => (l.who === 'ham' ? { ...l, ko: null } : l))
  const meaning = async (word: string, responseId: string) => {
    const key = `${responseId}:${word}`
    if (!cache.current.has(key)) cache.current.set(key, await api.wordGloss(session.sessionId!, word, responseId))
    return cache.current.get(key)!
  }
  return (
    <main className="hc-screen hc-review" aria-labelledby="hc-review-title">
      <div className="scroll">
        <h1 id="hc-review-title" ref={title} tabIndex={-1}>
          대화를 다시 볼까요?
        </h1>
        {s.error && (
          <p className="broke" role="alert">
            {s.error.messageKo}
          </p>
        )}
        {s.endedBy === 'bye' && <p className="earn">통화 완료 · 내가 {mine}번 말했어요</p>}
        <p className="hint">낱말을 누르면 뜻이 나와요. 이 대화는 저장하지 않아요.</p>
        <TalkList lines={lines} canHear={(l) => session.canHear(l)} hear={(l) => session.replayLine(l)} gloss={meaning} />
      </div>
      <div className="foot">
        <button type="button" className="hc-btn" onClick={() => closeCall(s.endedBy === 'bye' ? 'done' : s.endedBy === 'quit' ? 'quit' : 'error')}>
          계속하기
        </button>
      </div>
    </main>
  )
}

function Switch({ label, note, on, set }: { label: string; note?: string; on: boolean; set(on: boolean): void }) {
  return (
    <label className="row">
      <span>
        {label}
        {note && <small>{note}</small>}
      </span>
      <input type="checkbox" role="switch" checked={on} onChange={(e) => set(e.target.checked)} />
    </label>
  )
}

const MicIcon = () => (
  <svg className="mic" viewBox="0 0 24 24">
    <rect x="8.5" y="2.5" width="7" height="12" rx="3.5" />
    <path d="M5 11.5a7 7 0 0 0 14 0M12 18.5V22M8.5 22h7" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" />
  </svg>
)
const HangIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 9c-3 0-5.6.7-7.7 1.9-.9.5-1.3 1.5-1 2.5l.5 1.7c.2.8 1 1.2 1.8 1l2.2-.6c.7-.2 1.2-.9 1.1-1.6l-.1-1.2c1-.3 2.1-.4 3.2-.4s2.2.1 3.2.4l-.1 1.2c-.1.7.4 1.4 1.1 1.6l2.2.6c.8.2 1.6-.2 1.8-1l.5-1.7c.3-1-.1-2-1-2.5C17.6 9.7 15 9 12 9Z" />
  </svg>
)
const ReplayIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 5a7 7 0 1 1-6.3 4" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" />
    <path d="M13 1.8 8.2 5.2 13 8.6Z" fill="currentColor" />
  </svg>
)
const CcIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <rect x="2" y="4.5" width="20" height="15" rx="4" className="box" />
    <path d="M10.5 10.2a2.4 2.4 0 1 0 0 3.6M17.5 10.2a2.4 2.4 0 1 0 0 3.6" className="cc" />
  </svg>
)
const GearIcon = () => (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M12 8.5A3.5 3.5 0 1 0 12 15.5 3.5 3.5 0 1 0 12 8.5ZM19.4 13.5l1.6 1.2-1.8 3.1-1.9-.7a7 7 0 0 1-2 1.2L15 20.5h-3.6l-.3-2.2a7 7 0 0 1-2-1.2l-2 .7-1.8-3.1 1.7-1.2a7 7 0 0 1 0-2.7L5.3 9.5l1.8-3.1 2 .7a7 7 0 0 1 2-1.2l.3-2.2H15l.3 2.2a7 7 0 0 1 2 1.2l1.9-.7 1.8 3.1-1.6 1.2a7 7 0 0 1 0 2.8Z" />
  </svg>
)
