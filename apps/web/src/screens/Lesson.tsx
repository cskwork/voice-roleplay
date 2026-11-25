import { useEffect, useRef, useState } from 'react'
import { Icon } from '../components/Icon'
import { ModelAudio } from '../components/ModelAudio'
import { Recorder } from '../components/Recorder'
import { JobProgress, Notice } from '../components/ui'
import { useAttemptRunner } from '../hooks/attempt'
import { stopModelAudio, type Take } from '../hooks/audio'
import { canAdvanceLesson, lessonComparison, lessonSteps, type LessonStep } from '../lib/lesson'
import type { AttemptResult, Difficulty, Scenario } from '../lib/types'
import { navigate, useApp, useVoiceId } from '../state'

const HINT_LABEL = ['뜻 보기', '문장 조각 보기', '예문 도움 보기']

export function Lesson({ scenarioId, opts }: { scenarioId: string; opts: string }) {
  const app = useApp()
  const sc = app.scenario(scenarioId)
  const raw = opts.split(':')[0]
  const difficulty = (['easy', 'normal', 'hard'].includes(raw ?? '') ? raw : app.settings.difficulty) as Difficulty
  const steps = sc ? lessonSteps(sc) : null
  const [index, setIndex] = useState(0)
  const [answers, setAnswers] = useState<AttemptResult[]>([])
  const [round, setRound] = useState(0)
  const focus = useRef<HTMLHeadingElement>(null)
  const completeHeading = useRef<HTMLHeadingElement>(null)
  useEffect(() => {
    if (index === 3) completeHeading.current?.focus()
    else if (index > 0 || round > 0) focus.current?.focus()
  }, [index, round])

  if (!sc && app.scenariosLoading) return <main className="page narrow"><Notice>연습할 상황을 불러오는 중이에요…</Notice><a href="#/home">학습 홈으로</a></main>
  if (!sc || !steps) return (
    <main className="page narrow">
      <h1>이 상황의 짧은 연습을 준비하지 못했어요</h1>
      <Notice tone="warn">{app.scenariosError?.messageKo ?? (!sc ? '이 주소의 상황을 찾을 수 없어요. 다른 상황을 골라 주세요.' : '서로 다른 목표와 예문 3개가 필요해요. 자유 대화나 녹음 연습은 계속할 수 있어요.')}</Notice>
      <div className="row">
        <a className="btn btn-primary" href="#/home">다른 상황 고르기</a>
        {sc && <><a className="btn" href={`#/talk/${encodeURIComponent(sc.scenario_id)}/${difficulty}:0`}>자유 대화</a><a className="btn" href={`#/practice/${encodeURIComponent(sc.scenario_id)}/${difficulty}:0`}>녹음 연습</a></>}
      </div>
    </main>
  )

  const restart = () => { setAnswers([]); setIndex(0); setRound((n) => n + 1) }
  return (
    <main className="page lesson-page" aria-labelledby="lesson-title">
      <header className="lesson-head">
        <div><h1 id="lesson-title" ref={focus} tabIndex={-1}>{sc.title_ko}</h1><p className="muted small">듣고, 내 목소리로 한 문장씩</p></div>
        <a className="btn btn-ghost" href="#/home"><Icon name="close" size={17} /> 끝내기</a>
      </header>
      {index < 3 ? (
        <div className="lesson-layout">
          <aside className="lesson-outline" aria-label="이번 연습의 표현">
            <p className="lesson-count" role="status">표현 {index + 1} / 3</p>
            <ol>{steps.map((step, i) => <li key={step.goal.goal_id} className={i === index ? 'is-current' : i < index ? 'is-finished' : ''} aria-current={i === index ? 'step' : undefined}><span className="lesson-step-mark">{i < index ? <Icon name="check" size={16} /> : i + 1}</span><span>{step.goal.ko}</span></li>)}</ol>
            <p className="muted small">듣고 → 말하고 → 다음 표현으로.<br />맞고 틀림을 채점하지 않아요.</p>
          </aside>
          <LessonTurn key={`${round}:${index}`} sc={sc} step={steps[index]!} last={index === 2} onNext={(result) => { setAnswers((a) => [...a, result]); setIndex((n) => n + 1) }} />
        </div>
      ) : (
        <section className="lesson-complete" aria-labelledby="complete-title">
          <span className="lesson-complete-mark" aria-hidden="true"><Icon name="hand" size={30} /></span>
          <h2 id="complete-title" ref={completeHeading} tabIndex={-1}>표현 3개를 말해 봤어요</h2>
          <p className="muted">한 문장씩, 끝까지 연습했어요. 내 속도로 한 번 더 해 볼까요?</p>
          <ol className="lesson-recap">{steps.map((step, i) => {
            const result = answers[i]!
            const compare = lessonComparison(result)
            return <li key={step.goal.goal_id}><h3>{step.goal.ko}</h3><p lang="en" className="lesson-recap-target">{step.expression.en}</p><details className="lesson-recap-heard"><summary>내가 한 말 보기</summary><p lang="en">{result.transcript}</p></details>{compare.kind !== 'same' && <span className="lesson-review-note">예문을 듣고 다시 말해 볼 표현</span>}</li>
          })}</ol>
          <div className="lesson-complete-actions"><button className="btn btn-primary btn-lg" onClick={restart}><Icon name="replay" /> 다시 말하기</button><button className="btn" disabled={!app.health?.modes.realtime.available} onClick={() => navigate('talk', scenarioId, `${difficulty}:0`)}>이 상황으로 자유 대화</button><a className="btn btn-ghost" href="#/home">다른 상황 고르기</a></div>
          {!app.health?.modes.realtime.available && <p className="disabled-reason">{app.health?.modes.realtime.reason_ko ?? '자유 대화가 준비되면 이어 갈 수 있어요.'}</p>}
          <p className="muted small">이번 연습에서만 보여요. 정답·문법·발음 평가는 하지 않아요.</p>
        </section>
      )}
    </main>
  )
}

function LessonTurn({ sc, step, last, onNext }: { sc: Scenario; step: LessonStep; last: boolean; onNext(result: AttemptResult): void }) {
  const app = useApp()
  const voiceId = useVoiceId(sc)
  const [hint, setHint] = useState(0)
  const [hintExpanded, setHintExpanded] = useState(false)
  const [result, setResult] = useState<AttemptResult | null>(null)
  const [takeKey, setTakeKey] = useState(0)
  const [recording, setRecording] = useState(false)
  const resultHeading = useRef<HTMLHeadingElement>(null)
  const runner = useAttemptRunner((r) => setResult(r))
  const recorded = app.health?.modes.recorded
  const disabled = app.healthError?.messageKo ?? (!app.health ? '모델 상태를 확인하고 있어요.' : recorded?.available ? null : recorded?.reason_ko ?? '녹음 연습을 지금 사용할 수 없어요.')
  useEffect(() => { if (result) resultHeading.current?.focus() }, [result])
  const retry = () => { runner.reset(); setResult(null); setTakeKey((n) => n + 1); stopModelAudio() }
  const send = (take: Take) => {
    stopModelAudio()
    void runner.submit(take, { exercise_type: 'shadowing', scenario_id: sc.scenario_id, text_id: step.expression.text_id, history_opt_in: false })
  }
  const compare = result ? lessonComparison(result) : null
  const canNext = runner.phase === 'done' && canAdvanceLesson(result)
  const chunks = step.hint?.keywords?.filter(Boolean).join(' · ') || step.expression.en.split(/\s+/).slice(0, 3).join(' ') + ' …'
  return (
    <section className="lesson-sheet" aria-labelledby="turn-title">
      <div className="lesson-prompt"><div className="lesson-task"><span className="partner-symbol" aria-hidden="true"><Icon name="hand" size={22} /></span><h2 id="turn-title">{step.goal.ko}</h2></div><p className="lesson-expression" lang="en">{step.expression.en}</p><p className="muted">듣고, 천천히 따라 말해요.</p><ModelAudio voiceId={voiceId} source={{ textId: step.expression.text_id, text: step.expression.en }} ttsReady={app.health?.components.tts?.ready ?? false} label="예문 듣기" disabledReason={recording ? '녹음을 마치면 예문을 들을 수 있어요.' : runner.busy ? '문장을 확인한 뒤 들을 수 있어요.' : null} /></div>
      {!result ? <>
        <Recorder key={takeKey} deviceId={app.micDeviceId} minMs={1000} maxMs={30000} submitting={runner.busy} submitDisabledReason={disabled} submitLabel="보내고 문장 확인하기" onRecordingChange={setRecording} onSubmit={send} />
        {disabled && <Notice tone="warn">{disabled} <a href="#/setup">장비 점검</a></Notice>}
        {runner.busy && <div className="job" role="status">{runner.job ? <JobProgress state={runner.job.state} /> : <p>녹음을 보내는 중이에요…</p>}<button className="btn btn-ghost" onClick={() => runner.cancel()}>보내기 취소</button></div>}
        {runner.error && <Notice tone="danger" icon="alert">{runner.error} 현재 표현은 그대로예요. 다시 제출하거나 새로 녹음해 주세요.</Notice>}
        {runner.endState && runner.endState !== 'completed' && !runner.error && <Notice tone="warn">{runner.endState === 'cancelled' ? '보내기를 취소했어요.' : '문장을 확인하지 못했어요.'} 다시 녹음해 주세요.</Notice>}
      </> : <div className="lesson-response">
        <h3 ref={resultHeading} tabIndex={-1}>{canNext ? '이렇게 들렸어요' : '말한 내용을 듣지 못했어요'}</h3>
        {canNext ? <>
          <p className="lesson-transcript" lang="en">{result.transcript}</p>
          <div className="lesson-comparison" aria-live="polite">
            {compare?.kind === 'same' ? <p>인식된 문장이 예문과 같아요.</p> : compare?.kind === 'different' ? <><p>{compare.target ? <>예문에는 <strong lang="en">“{compare.target}”</strong>가 있어요.</> : <>예문에 없는 <strong lang="en">“{compare.heard}”</strong>가 들렸어요.</>}</p><p className="muted small">{compare.target && compare.heard && <><span lang="en">“{compare.heard}”</span>로 들렸어요. </>}인식이 다르면 다시 말해 보세요.</p></> : <p>예문과의 비교 자료가 없어 차이를 확인할 수 없어요. 예문을 듣고 한 번 더 말해 보세요.</p>}
          </div>
          <p className="muted small">인식된 글을 비교한 안내예요. 뜻·문법·발음의 정답 판정이 아니에요.</p>
        </> : <Notice tone="warn">잘 안 들렸어요. 마이크 권한과 연결을 확인하고 다시 녹음해 주세요. <a href="#/setup">장비 점검</a></Notice>}
        <div className="lesson-next"><button type="button" className={`btn ${canNext ? '' : 'btn-primary'} btn-lg`} onClick={retry}><Icon name="replay" size={18} /> 한 번 더 말하기</button>{canNext && <button type="button" className="btn btn-primary btn-lg" onClick={() => onNext(result)}>{last ? '이번 연습 마치기' : '다음 표현'} <Icon name="play" size={17} /></button>}</div>
      </div>}
      <div className="lesson-help">
        <button type="button" className="btn btn-soft" onClick={() => {
          if (hint > 0 && !hintExpanded) { setHintExpanded(true); return }
          if (hint === 3) { setHintExpanded(false); return }
          setHint((n) => n + 1); setHintExpanded(true)
        }} aria-expanded={hintExpanded} aria-controls="lesson-hint"><Icon name="hint" size={18} />{hint > 0 && !hintExpanded ? '도움 다시 보기' : hint === 3 ? '도움 접기' : HINT_LABEL[hint]}</button>
        <div id="lesson-hint" className="lesson-hint" hidden={!hintExpanded || hint === 0} aria-live="polite"><p>{step.expression.ko}</p>{hint >= 2 && <p lang="en" className="lesson-chunks">{chunks}</p>}{hint >= 3 && <><p>{step.hint?.ko ?? '예문을 듣고 천천히 한 번 더 말해 보세요.'}</p><p lang="en">{step.expression.en}</p></>}</div>
      </div>
      <p className="lesson-footnote">녹음은 제출할 때 보내요. 서버에 녹음·전사 기록을 남기지 않아요. <a href="#/setup">마이크 도움말</a></p>
    </section>
  )
}
