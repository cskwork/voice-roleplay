import { useEffect } from 'react'
import { Icon } from './components/Icon'
import { Home } from './screens/Home'
import { Lesson } from './screens/Lesson'
import { Practice } from './screens/Practice'
import { Result } from './screens/Result'
import { Review } from './screens/Review'
import { Settings } from './screens/Settings'
import { Setup } from './screens/Setup'
import { Summary } from './screens/Summary'
import { Talk } from './screens/Talk'
import { useApp, useRoute } from './state'

const NAV = [
  ['home', '학습 홈'],
  ['review', '복습'],
  ['setup', '장비 점검'],
  ['settings', '설정'],
] as const

function Screen({ route }: { route: string[] }) {
  const [name, ...rest] = route
  switch (name) {
    case 'home':
      return <Home />
    case 'lesson':
      return <Lesson scenarioId={rest[0] ?? ''} opts={rest[1] ?? ''} />
    case 'talk':
      return <Talk scenarioId={rest[0] ?? ''} opts={rest[1] ?? ''} />
    case 'summary':
      return <Summary />
    case 'practice':
      return <Practice parts={rest} />
    case 'result':
      return <Result attemptId={rest[0] ?? ''} />
    case 'review':
      return <Review />
    case 'settings':
      return <Settings />
    default:
      return <Setup />
  }
}

export function App() {
  const route = useRoute()
  const { health } = useApp()
  const current = route[0] ?? 'setup'
  const inTalk = current === 'talk'
  const inLesson = current === 'lesson'
  const learning = inTalk || inLesson || current === 'home'
  const routeKey = route.join('/')
  // A new screen starts at its top, not at the previous screen's scroll position.
  useEffect(() => {
    window.scrollTo(0, 0)
    document.getElementById('main-content')?.scrollTo(0, 0)
  }, [routeKey])
  return (
    <div className={`shell ${learning ? 'shell-learning' : ''} ${inTalk ? 'shell-fixed' : ''}`} >
      <a className="skip" href="#main-content" onClick={(e) => { e.preventDefault(); document.getElementById('main-content')?.focus() }}>
        본문으로 건너뛰기
      </a>
      <header className="topbar">
        <a className="brand" href="#/home" aria-label="말하기 연습 홈">
          <span className="brand-mark" aria-hidden="true">
            <Icon name="mic" size={18} />
          </span>
          말하기 연습
        </a>
        {!inTalk && !inLesson && (
          <nav aria-label="주요 메뉴">
            {NAV.map(([id, label]) => (
              <a key={id} href={`#/${id}`} aria-current={current === id ? 'page' : undefined}>
                {label}
              </a>
            ))}
          </nav>
        )}
        <span className={`health-dot ${health?.ready ? 'ok' : 'wait'}`}>
          <Icon name={health?.ready ? 'check' : 'circle'} size={14} />
          {health?.ready ? '연습 준비됨' : '준비 확인 중'}
        </span>
      </header>
      <div id="main-content" tabIndex={-1}>
        <Screen key={routeKey} route={route} />
      </div>
    </div>
  )
}
