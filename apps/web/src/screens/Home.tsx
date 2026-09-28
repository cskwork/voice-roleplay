import { useState } from 'react'
import { Icon } from '../components/Icon'
import { Choice, Notice } from '../components/ui'
import type { Difficulty, Mode } from '../lib/types'
import { navigate, useApp } from '../state'

export const DIFFICULTY_LABEL: Record<Difficulty, string> = { easy: '쉬움', normal: '보통', hard: '도전' }
export const DIFFICULTY_NOTE: Record<Difficulty, string> = {
  easy: '짧은 문장, 생각할 시간 길게',
  normal: '자연스러운 속도',
  hard: '긴 대화, 빠른 턴',
}

export function Home() {
  const { health, healthError, scenarios, scenariosError, settings } = useApp()
  const realtime = health?.modes.realtime
  const recorded = health?.modes.recorded
  const [mode, setMode] = useState<Mode>(() => (realtime?.available || !recorded?.available ? 'realtime' : 'recorded'))
  const [difficulty, setDifficulty] = useState<Difficulty>(settings.difficulty)
  const [optIn, setOptIn] = useState(settings.history_opt_in)
  const modeInfo = mode === 'realtime' ? realtime : recorded

  const start = (scenarioId: string) => {
    const q = `${difficulty}:${optIn ? 1 : 0}`
    navigate(mode === 'realtime' ? 'talk' : 'practice', scenarioId, q)
  }

  return (
    <main className="page" aria-labelledby="home-title">
      <header className="page-head">
        <h1 id="home-title">오늘은 어떤 상황을 연습할까요?</h1>
        <p className="lead">방식과 난이도를 정하고, 연습할 상황을 골라 주세요.</p>
      </header>

      <section className="options-bar" aria-label="연습 설정">
        <Choice<Mode>
          label="연습 방식"
          name="mode"
          value={mode}
          onChange={setMode}
          options={[
            { value: 'realtime', label: '실시간 회화', note: '말하면 AI가 바로 대답해요', disabledReason: realtime && !realtime.available ? realtime.reason_ko ?? '지금 사용할 수 없어요' : undefined },
            { value: 'recorded', label: '녹음형 연습', note: '녹음하고 제출하면 자세히 분석해요', disabledReason: recorded && !recorded.available ? recorded.reason_ko ?? '지금 사용할 수 없어요' : undefined },
          ]}
        />
        <Choice<Difficulty>
          label="난이도"
          name="difficulty"
          value={difficulty}
          onChange={setDifficulty}
          options={(['easy', 'normal', 'hard'] as const).map((d) => ({ value: d, label: DIFFICULTY_LABEL[d], note: DIFFICULTY_NOTE[d] }))}
        />
        <label className="toggle">
          <input type="checkbox" checked={optIn} onChange={(e) => setOptIn(e.target.checked)} />
          <span className="toggle-track" aria-hidden="true" />
          <span>
            <strong>이번 연습 기록 저장</strong>
            <span className="muted small block">전사와 피드백만 이 컴퓨터에 저장해 복습에 써요. 음성은 저장하지 않아요.</span>
          </span>
        </label>
      </section>

      {!health && !healthError && <Notice>모델 상태를 확인하는 중이에요…</Notice>}
      {healthError && (
        <Notice tone="danger" icon="alert">
          {healthError.messageKo} <a href="#/setup">장비 점검으로 이동</a>
        </Notice>
      )}
      {health && modeInfo && !modeInfo.available && (
        <Notice tone="warn" icon="alert">
          {modeInfo.reason_ko ?? '선택한 연습 방식을 지금 사용할 수 없어요.'}{' '}
          <a href="#/setup">장비 점검으로 이동</a>
        </Notice>
      )}
      {scenariosError && !healthError && <Notice tone="danger" icon="alert">{scenariosError.messageKo}</Notice>}

      <section aria-labelledby="sc-h" className="scenarios">
        <h2 id="sc-h" className="section-title">
          상황 고르기
        </h2>
        <ul className="scenario-grid">
          {scenarios.map((s) => (
            <li key={s.scenario_id} className="card scenario">
              <div className="scenario-title">
                <h3>{s.title_ko}</h3>
                <p className="muted" lang="en">
                  {s.title_en}
                </p>
              </div>
              <p className="roles">
                <span>
                  AI · <strong>{s.ai_role_ko}</strong>
                </span>
                <span>
                  나 · <strong>{s.user_role_ko}</strong>
                </span>
              </p>
              <p className="scene">{s.setting_ko}</p>
              <ul className="goal-preview" aria-label="학습 목표">
                {s.goals.map((g) => (
                  <li key={g.goal_id}>{g.ko}</li>
                ))}
              </ul>
              <button
                type="button"
                className="btn btn-soft"
                disabled={!modeInfo?.available}
                aria-describedby={!modeInfo?.available ? 'mode-off' : undefined}
                onClick={() => start(s.scenario_id)}
              >
                이 상황으로 연습 <Icon name="play" size={16} />
              </button>
            </li>
          ))}
        </ul>
        {!modeInfo?.available && (
          <p id="mode-off" className="disabled-reason">
            {modeInfo?.reason_ko ?? '모델이 준비되면 시작할 수 있어요.'}
          </p>
        )}
      </section>
    </main>
  )
}
