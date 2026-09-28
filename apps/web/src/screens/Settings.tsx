import { useEffect, useState } from 'react'
import { listMicrophones } from '../audio/engine'
import { Badge, Choice, Notice } from '../components/ui'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import { DEFAULT_SILENCE_MS, type Difficulty, type Settings as S } from '../lib/types'
import { savedVoiceChoice } from '../lib/voice'
import { useApp } from '../state'
import { DIFFICULTY_LABEL, DIFFICULTY_NOTE } from './Home'

export function Settings() {
  const app = useApp()
  const [draft, setDraft] = useState<S>(app.settings)
  const [status, setStatus] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [mics, setMics] = useState<MediaDeviceInfo[]>([])
  const [ctxRate, setCtxRate] = useState<number | null>(null)

  useEffect(() => setDraft(app.settings), [app.settings])
  useEffect(() => {
    listMicrophones().then(setMics, () => {})
    const c = new AudioContext()
    setCtxRate(c.sampleRate)
    void c.close()
  }, [])

  const voices = app.health?.components.tts?.voices ?? []
  const silence = draft.silence_ms ?? DEFAULT_SILENCE_MS[draft.difficulty]
  const set = <K extends keyof S>(k: K, v: S[K]) => {
    setDraft((d) => ({ ...d, [k]: v }))
    setStatus(null)
  }

  const save = async () => {
    setError(null)
    try {
      await app.saveSettings(draft)
      setStatus('저장했어요.')
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }

  const deleteAll = async () => {
    setError(null)
    try {
      await api.deleteHistory()
      setConfirmDelete(false)
      setStatus('저장된 모든 기록을 삭제했어요.')
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }

  return (
    <main className="page narrow" aria-labelledby="st-title">
      <header className="page-head">
        <h1 id="st-title">내 연습 환경</h1>
      </header>

      <section className="card form" aria-labelledby="learn-h">
        <h2 id="learn-h">학습</h2>
        <Choice<Difficulty>
          label="기본 난이도"
          name="difficulty"
          value={draft.difficulty}
          onChange={(d) => set('difficulty', d)}
          options={(['easy', 'normal', 'hard'] as Difficulty[]).map((d) => ({ value: d, label: DIFFICULTY_LABEL[d], note: DIFFICULTY_NOTE[d] }))}
        />
        <label className="field">
          <span>
            말이 끝났다고 판단할 침묵 시간: <strong>{(silence / 1000).toFixed(2)}초</strong>
            {draft.silence_ms == null && <span className="muted small"> (난이도 기본값)</span>}
          </span>
          <input type="range" min={700} max={1400} step={50} value={silence} onChange={(e) => set('silence_ms', Number(e.target.value))} aria-valuetext={`${silence} 밀리초`} />
          <span className="muted small">길게 하면 생각할 시간이 늘고, 짧게 하면 대화가 빨라져요. (0.7~1.4초)</span>
        </label>
        {draft.silence_ms != null && (
          <button type="button" className="link small" onClick={() => set('silence_ms', null)}>
            난이도 기본값으로 되돌리기
          </button>
        )}
        <label className="toggle">
          <input type="checkbox" checked={draft.auto_barge_in} onChange={(e) => set('auto_barge_in', e.target.checked)} />
          <span className="toggle-track" aria-hidden="true" />
          <span>
            <strong>자동 끼어들기</strong>
            <span className="muted small block">AI가 말하는 중에 내가 말하면 AI 음성을 바로 멈춰요. 스피커를 쓰면 오작동할 수 있어요.</span>
          </span>
        </label>
      </section>

      <section className="card form" aria-labelledby="dev-h">
        <h2 id="dev-h">장치와 음성</h2>
        <label className="field">
          <span>마이크</span>
          <select value={app.micDeviceId} onChange={(e) => app.setMicDeviceId(e.target.value)}>
            <option value="">시스템 기본 마이크</option>
            {mics.filter((m) => m.deviceId).map((m) => (
              <option key={m.deviceId} value={m.deviceId}>
                {m.label || '이름 없는 마이크 (권한을 허용하면 이름이 보여요)'}
              </option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>AI 음성</span>
          <select value={savedVoiceChoice(draft.voice_id, voices)} onChange={(e) => set('voice_id', e.target.value || null)} disabled={voices.length === 0}>
            <option value="">시나리오 기본 음성</option>
            {voices.map((v) => (
              <option key={v.voice_id} value={v.voice_id}>
                {v.label}
              </option>
            ))}
          </select>
          {voices.length === 0 && <span className="muted small">음성 합성 모델이 준비되면 고를 수 있어요.</span>}
          {voices.find((v) => v.voice_id === draft.voice_id)?.license_note && <span className="muted small">{voices.find((v) => v.voice_id === draft.voice_id)!.license_note}</span>}
        </label>
      </section>

      <section className="card form" aria-labelledby="data-h">
        <h2 id="data-h">데이터 보존</h2>
        <label className="toggle">
          <input type="checkbox" checked={draft.history_opt_in} onChange={(e) => set('history_opt_in', e.target.checked)} />
          <span className="toggle-track" aria-hidden="true" />
          <span>
            <strong>기록 저장을 기본으로 켜기</strong>
            <span className="muted small block">전사·피드백만 이 컴퓨터에 저장해요. 음성은 어떤 경우에도 저장하지 않아요. 저장하지 않은 요약은 15분 뒤 사라져요.</span>
          </span>
        </label>
        {!confirmDelete ? (
          <button type="button" className="btn btn-danger-soft" onClick={() => setConfirmDelete(true)}>
            저장된 기록 모두 삭제
          </button>
        ) : (
          <div className="confirm" role="alertdialog" aria-labelledby="del-q">
            <p id="del-q">
              <strong>정말 모든 기록을 삭제할까요?</strong> 되돌릴 수 없어요.
            </p>
            <div className="row">
              <button type="button" className="btn btn-danger" onClick={() => void deleteAll()} autoFocus>
                삭제
              </button>
              <button type="button" className="btn btn-ghost" onClick={() => setConfirmDelete(false)}>
                취소
              </button>
            </div>
          </div>
        )}
      </section>

      <div className="page-actions">
        <button type="button" className="btn btn-primary btn-lg" onClick={() => void save()}>
          설정 저장
        </button>
        <span role="status" className="small">
          {status}
        </span>
      </div>
      {error && <Notice tone="danger" icon="alert">{error}</Notice>}

      <section className="card" aria-labelledby="diag-h">
        <h2 id="diag-h">진단 정보</h2>
        <table className="diag">
          <thead>
            <tr>
              <th scope="col">구성 요소</th>
              <th scope="col">상태</th>
              <th scope="col">장치</th>
              <th scope="col">모델 / 버전</th>
            </tr>
          </thead>
          <tbody>
            {(['gateway', 'vad', 'asr', 'tts', 'llm', 'pron'] as const).map((k) => {
              const c = app.health?.components[k]
              if (k === 'pron' && !c) return null
              const aligner = c?.models?.aligner
              return (
                <tr key={k}>
                  <th scope="row">{k.toUpperCase()}</th>
                  <td data-label="상태">{c ? c.ready ? <Badge tone="ok">준비됨</Badge> : <Badge tone="warn">준비 안 됨</Badge> : <Badge>정보 없음</Badge>}</td>
                  <td data-label="장치">{c?.device ?? '—'}</td>
                  <td data-label="모델 / 버전" className="mono small model-cell">
                    {[c?.model_id ?? c?.model ?? aligner?.model_id, (c?.revision ?? c?.model_revision ?? aligner?.revision)?.slice(0, 12), c?.backend, c?.streaming_mode, c?.sample_rate && `${c.sample_rate} Hz`]
                      .filter(Boolean)
                      .join(' · ') || '—'}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
        <ul className="plain small">
          <li>브라우저 오디오 샘플레이트: {ctxRate ? `${ctxRate.toLocaleString()} Hz` : '—'} (마이크는 16 kHz로 변환)</li>
          <li>보안 컨텍스트: {window.isSecureContext ? '예' : '아니요 — 마이크를 쓰려면 127.0.0.1 또는 localhost로 접속하세요'}</li>
          <li>
            발음 평가:{' '}
            {app.health?.pronunciation === 'experimental_banded'
              ? '점수 없음. 실험 등급이 켜져 있어요 (한국어 학습자 검증 전)'
              : app.health?.pronunciation === 'timing_only'
                ? '점수 없음. 단어 위치와 억양 곡선만 보여 줘요'
                : '제공되지 않음 (발음 분석 워커 없음)'}
          </li>
        </ul>
        <button type="button" className="btn btn-ghost" onClick={() => void app.refreshHealth()}>
          다시 확인
        </button>
      </section>
    </main>
  )
}
