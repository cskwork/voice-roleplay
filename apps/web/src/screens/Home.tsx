import { useState } from 'react'
import { Icon } from '../components/Icon'
import { Choice, Notice } from '../components/ui'
import { lessonSteps } from '../lib/lesson'
import type { Difficulty, Scenario } from '../lib/types'
import { navigate, useApp } from '../state'

export const DIFFICULTY_LABEL: Record<Difficulty, string> = { easy: '쉬움', normal: '보통', hard: '도전' }
export const DIFFICULTY_NOTE: Record<Difficulty, string> = { easy: '짧은 문장, 생각할 시간 길게', normal: '자연스러운 속도', hard: '긴 대화, 빠른 턴' }
type LearningMode = 'lesson' | 'talk' | 'practice'
const MODE_NOTE: Record<LearningMode, string> = { lesson: '듣고 한 번씩 녹음하며 표현 3개를 연습해요.', talk: 'AI와 자유롭게 대화해요. 막히면 뜻과 표현 도움을 받아요.', practice: '문장 읽기·따라 말하기·자유 답변·역할극을 골라 녹음해요.' }

export function Home() {
  const { health, healthError, scenarios, scenariosError, scenariosLoading, settings } = useApp()
  const [mode, setMode] = useState<LearningMode>('lesson')
  const [difficulty, setDifficulty] = useState<Difficulty>(settings.difficulty)
  const [optIn, setOptIn] = useState(settings.history_opt_in)
  const modeInfo = health?.modes[mode === 'talk' ? 'realtime' : 'recorded']
  const sorted = [...scenarios].sort((a, b) => {
    const order = ['friend_intro', 'snack_choice', 'weekend_plan', 'lost_and_found', 'cafe_order', 'directions']
    const rank = (s: Scenario) => { const i = order.indexOf(s.scenario_id); return i < 0 ? order.length : i }
    return rank(a) - rank(b)
  })
  const [selected, setSelected] = useState<string | null>(null)
  const sc = sorted.find((s) => s.scenario_id === selected) ?? sorted[0]
  const steps = sc ? lessonSteps(sc) : null
  const unavailable = healthError?.messageKo ?? (!health ? '모델 상태를 확인하는 중이에요.' : !modeInfo?.available ? modeInfo?.reason_ko ?? '이 연습을 지금 사용할 수 없어요.' : mode === 'lesson' && !steps ? '이 상황에는 표현 3개 연습이 준비되지 않았어요. 자유 대화나 녹음 연습을 골라 주세요.' : null)
  return (
    <main className="page learning-home" aria-labelledby="home-title">
      <header className="page-head"><h1 id="home-title">한 문장부터,<br /><span>편하게 말해 봐요.</span></h1><p className="lead">익숙한 상황을 고르고, 듣고 말하며 영어를 연습해요.</p></header>
      <details className="home-preferences"><summary><span>{mode === 'lesson' ? '짧은 말하기로 시작해요' : mode === 'talk' ? '자유 대화로 연습해요' : '녹음 연습으로 시작해요'}</span><span className="home-preferences-label">방식·난이도 바꾸기 <Icon name="chevronDown" size={16} /></span></summary><section className="home-choices" aria-label="연습 설정">
        <Choice<LearningMode> label="이렇게 연습해요" name="learning-mode" value={mode} onChange={setMode} options={[
          { value: 'lesson', label: '짧은 말하기', note: MODE_NOTE.lesson },
          { value: 'talk', label: '자유 대화', note: MODE_NOTE.talk },
          { value: 'practice', label: '녹음 연습', note: MODE_NOTE.practice },
        ]} />
        <Choice<Difficulty> label="난이도" name="difficulty" value={difficulty} onChange={setDifficulty} options={(['easy', 'normal', 'hard'] as const).map((d) => ({ value: d, label: DIFFICULTY_LABEL[d], note: mode === 'talk' ? DIFFICULTY_NOTE[d] : '짧은 말하기와 문장 읽기는 검수된 같은 예문을 사용해요. 난이도는 자유 대화로 이어 갈 때 적용해요.' }))} />
      </section></details>
      {healthError && <Notice tone="danger" icon="alert">{healthError.messageKo} <a href="#/setup">장비 점검</a></Notice>}
      {scenariosError && <Notice tone="danger" icon="alert">{scenariosError.messageKo}</Notice>}
      {!sc && !scenariosError && <Notice>{scenariosLoading ? '연습할 상황을 불러오는 중이에요…' : '아직 등록된 상황이 없어요. 서버의 상황 목록을 확인해 주세요.'}</Notice>}
      {sc && <div className="home-scenes">
        <section className="home-feature" aria-labelledby="featured-title">
          <div className="home-feature-title"><h2 id="featured-title">{sc.title_ko}</h2><span lang="en" className="muted small">{sc.title_en}</span></div>
          <p className="roles"><span>AI · <strong>{sc.ai_role_ko}</strong></span><span>나 · <strong>{sc.user_role_ko}</strong></span></p>
          <p className="home-first-goal">{sc.goals[0]?.ko ?? sc.setting_ko}</p>
          <div className="home-dialogue" aria-label="이번 상황의 대화 예시"><div className="learning-partner"><span className="partner-symbol" aria-hidden="true"><Icon name="hand" size={24} /></span><span>{sc.ai_role_ko}<span className="muted small block">먼저 말을 걸어요</span></span></div><p className="home-opening" lang="en">{sc.opening_line.en}</p><div className="home-my-line"><span className="muted small">이렇게 말해 볼까요?</span><p className="home-expression" lang="en">{(steps?.[0]?.expression ?? sc.model_expressions?.[0])?.en}</p></div></div>
          <div className="learning-path" aria-label="연습 순서"><span><Icon name="speaker" size={17} /> 듣기</span><Icon name="arrowRight" size={14} /><span><Icon name="mic" size={17} /> 말하기</span><Icon name="arrowRight" size={14} /><span><Icon name="replay" size={17} /> 다시 말하기</span></div>
          <details className="home-scene-details"><summary>어떤 상황인지 더 볼까요?</summary><p>{sc.setting_ko}</p><ul className="home-goals" aria-label="이번에 해볼 말">{sc.goals.slice(1, 3).map((g) => <li key={g.goal_id}>{g.ko}</li>)}</ul></details>
          {mode !== 'lesson' && !health?.shared && <label className="toggle"><input type="checkbox" checked={optIn} onChange={(e) => setOptIn(e.target.checked)} /><span className="toggle-track" aria-hidden="true" /><span>이번 연습 기록 저장<span className="muted small block">전사·피드백만 이 컴퓨터에 저장해요. 음성은 저장하지 않아요.</span></span></label>}
          <div className="home-start"><button type="button" className="btn btn-primary btn-lg" disabled={!!unavailable} aria-describedby={unavailable ? 'home-unavailable' : 'home-session-note'} onClick={() => navigate(mode, sc.scenario_id, `${difficulty}:${mode === 'lesson' || health?.shared ? 0 : optIn ? 1 : 0}`)}>{mode === 'lesson' ? '표현 3개 연습하기' : mode === 'talk' ? '자유 대화 시작하기' : '녹음 연습 열기'}<Icon name="play" size={17} /></button><p id="home-session-note" className="muted small">{mode === 'lesson' ? '이번 연습에서만 결과를 보여요. 기록은 저장하지 않아요.' : health?.shared ? '공유 모드는 기록을 저장하지 않아요.' : '시작을 누르기 전에는 마이크를 켜지 않아요.'}</p></div>
          {unavailable && <Notice tone="warn"><span id="home-unavailable">{unavailable}</span> <a href="#/setup">장비 점검</a></Notice>}
        </section>
        <section className="home-other" aria-labelledby="scene-list-title"><h2 id="scene-list-title">어떤 상황에서 말해 볼까요?</h2><ul>{sorted.map((s) => <li key={s.scenario_id}><button type="button" className={sc.scenario_id === s.scenario_id ? 'is-selected' : ''} aria-pressed={sc.scenario_id === s.scenario_id} onClick={() => setSelected(s.scenario_id)}><span><strong>{s.title_ko}</strong><span className="muted small block">{s.goals[0]?.ko}</span></span><Icon name={sc.scenario_id === s.scenario_id ? 'check' : 'play'} size={17} /></button></li>)}</ul></section>
      </div>}
    </main>
  )
}
