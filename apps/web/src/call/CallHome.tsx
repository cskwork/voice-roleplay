import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import type { CallInfo, Difficulty } from '../lib/types'
import { navigate, useApp } from '../state'
import './call.css'
import { pastCalls, topicOfTheCall, type PastCall } from './history'
import { Stage } from './stage'
import { TalkList } from './TalkList'
import { transferNotice } from '../lib/privacy'

const LEVEL: Record<Difficulty, string> = { easy: '쉬움', normal: '보통', hard: '도전' }

export function CallHome() {
  const { health, healthError, settings } = useApp()
  const [calls, setCalls] = useState<CallInfo[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const [level, setLevel] = useState<Difficulty>(settings.difficulty)
  const [past, setPast] = useState<PastCall | null>(null)
  const recents = useSyncExternalStore(pastCalls.subscribe, pastCalls.get)
  const face = useRef<HTMLDivElement>(null)
  const pastDialog = useRef<HTMLDialogElement>(null)
  const stage = useRef<Stage | null>(null)

  useEffect(() => {
    const s = new Stage(face.current!, 168, 'ring')
    stage.current = s
    return () => s.stop()
  }, [])
  useEffect(() => {
    let alive = true
    api.calls().then(
      (list) => alive && setCalls(list),
      (e: unknown) => alive && setError(toApiError(e).messageKo),
    )
    return () => {
      alive = false
    }
  }, [])
  useEffect(() => {
    if (past) pastDialog.current?.showModal()
  }, [past])

  const chats = calls?.filter((c) => c.kind === 'chat') ?? []
  const roles = calls?.filter((c) => c.kind === 'role') ?? []
  const call = calls?.find((c) => c.call_id === picked) ?? topicOfTheCall(chats)
  const rt = health?.modes.realtime
  const why =
    healthError?.messageKo ?? error ?? (!health || !calls ? '햄찌를 부르는 중이에요…' : !call ? '이 서버에는 통화 주제가 없어요.' : rt && !rt.available ? rt.reason_ko ?? '지금은 통화할 수 없어요.' : null)
  const smile = (on: boolean) => stage.current?.hold(on ? 'happy' : 'ring', 220)

  return (
    <main className="hc hc-home" aria-labelledby="hc-home-title">
      <section className="hero">
        <div className="say">
          <h1 id="hc-home-title">햄찌 토크</h1>
          <p className={`online ${why ? 'wait' : ''}`}>
            <span className="dot" aria-hidden="true" />
            {why ?? '햄찌 · 전화 받을 수 있어요'}
          </p>
        </div>
        <div className="hc-face" ref={face} aria-hidden="true" />
        <div className="cta">
          {call && (
            <p className="topic">
              오늘은 <strong>{call.title_ko}</strong> <span lang="en">{call.title_en}</span>
            </p>
          )}
          <button
            type="button"
            className="hc-btn call"
            disabled={!!why}
            onPointerEnter={() => smile(true)}
            onPointerLeave={() => smile(false)}
            onFocus={() => smile(true)}
            onBlur={() => smile(false)}
            onClick={() => call && navigate('call', call.call_id, `${level}:0`)}
          >
            <svg viewBox="0 0 24 24" aria-hidden="true">
              <path d="M4 6.5h9.5A2.5 2.5 0 0 1 16 9v6a2.5 2.5 0 0 1-2.5 2.5H4A2.5 2.5 0 0 1 1.5 15V9A2.5 2.5 0 0 1 4 6.5Zm14 4 4.5-3v9l-4.5-3Z" />
            </svg>
            <span>햄찌에게 전화하기</span>
          </button>
          <p className="hc-privacy">
            {health?.api ? transferNotice(health, '이 앱은 대화와 녹음을 저장하지 않아요.') : '말할 때만 마이크를 켜고, 대화와 녹음은 저장하지 않아요.'}
          </p>
        </div>
      </section>

      <div className="rest">
        <details className="pick">
          <summary>
            주제·난이도 바꾸기 <small>{call ? `${call.title_ko} · ${LEVEL[level]}` : ''}</small>
          </summary>
          <fieldset className="levels">
            <legend>난이도</legend>
            {(['easy', 'normal', 'hard'] as const).map((d) => (
              <label key={d}>
                <input type="radio" name="hc-level" checked={level === d} onChange={() => setLevel(d)} />
                <span>{LEVEL[d]}</span>
              </label>
            ))}
          </fieldset>
          <TopicList title="이야기" list={chats} chosen={call} choose={setPicked} />
          <TopicList title="상황극" list={roles} chosen={call} choose={setPicked} />
        </details>

        <section className="recents" aria-labelledby="hc-recents-title">
          <h2 id="hc-recents-title">이번 연습의 통화</h2>
          {recents.length === 0 && <p className="empty">아직 통화가 없어요. 통화를 마치면 여기에서 다시 볼 수 있고, 창을 닫으면 사라져요.</p>}
          <ul>
            {recents.map((c) => (
              <li key={c.at.getTime()}>
                <button type="button" onClick={() => setPast(c)}>
                  <b>{c.title.ko}</b>
                  <small>
                    <span lang="en">{c.title.en}</span> · {c.at.toLocaleTimeString('ko-KR', { hour: 'numeric', minute: '2-digit' })}
                  </small>
                </button>
              </li>
            ))}
          </ul>
        </section>

        <a className="more" href="#/more">
          다른 연습: 상황별 말하기 · 자유 대화 · 녹음 연습
        </a>
      </div>

      <dialog ref={pastDialog} className="hc-sheet tall" aria-labelledby="hc-past-title" onClose={() => setPast(null)}>
        <h1 id="hc-past-title">{past?.title.ko}</h1>
        {past && <TalkList lines={past.lines} />}
        <form method="dialog">
          <button className="hc-btn" autoFocus>
            닫기
          </button>
        </form>
      </dialog>
    </main>
  )
}

function TopicList({ title, list, chosen, choose }: { title: string; list: CallInfo[]; chosen: CallInfo | undefined; choose(id: string): void }) {
  if (!list.length) return null
  return (
    <section>
      <h2>{title}</h2>
      <ul className="topics">
        {list.map((c) => (
          <li key={c.call_id}>
            <button type="button" aria-pressed={chosen?.call_id === c.call_id} onClick={() => choose(c.call_id)}>
              <b>{c.title_ko}</b>
              <small>{c.teaser_ko}</small>
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}
