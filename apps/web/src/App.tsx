import { Icon } from './components/Icon'
import { Home } from './screens/Home'
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
  return (
    <>
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
        {!inTalk && (
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
          {health?.ready ? '모델 준비됨' : '모델 준비 중'}
        </span>
      </header>
      <div id="main-content" tabIndex={-1}>
        <Screen key={route.join('/')} route={route} />
      </div>
    </>
  )
}
