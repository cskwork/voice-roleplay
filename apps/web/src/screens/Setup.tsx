import { useCallback, useEffect, useRef, useState } from 'react'
import { listMicrophones, MicCapture, Player } from '../audio/engine'
import { Icon } from '../components/Icon'
import { ActionButton, Badge, LevelMeter, Notice } from '../components/ui'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import { decodeWav } from '../lib/wav'
import type { ComponentHealth } from '../lib/types'
import { navigate, useApp, useVoiceId } from '../state'

const COMPONENTS: { key: 'vad' | 'asr' | 'tts' | 'llm'; label: string; role: string }[] = [
  { key: 'asr', label: '음성 인식', role: 'Qwen3-ASR 0.6B' },
  { key: 'tts', label: '음성 합성', role: 'CosyVoice3 0.5B' },
  { key: 'llm', label: '대화 모델', role: 'Qwen3-4B (llama.cpp)' },
  { key: 'vad', label: '발화 감지', role: 'Silero VAD' },
]

/** Two soft tones at 24 kHz — exercises the same resampling path as TTS audio. */
function chime(): Float32Array {
  const rate = 24000
  const out = new Float32Array(rate * 0.9)
  const notes = [
    [0, 0.35, 523.25],
    [0.4, 0.9, 783.99],
  ] as const
  for (const [s, e, f] of notes) {
    for (let i = Math.floor(s * rate); i < e * rate; i++) {
      const t = i / rate - s
      const env = Math.min(1, t * 40) * Math.exp(-t * 5)
      out[i] = 0.25 * env * Math.sin(2 * Math.PI * f * t)
    }
  }
  return out
}

function ReadyRow({ label, role, c }: { label: string; role: string; c?: ComponentHealth }) {
  const ready = c?.ready ?? false
  return (
    <li className="ready-row">
      <span className={`ready-icon ${ready ? 'ok' : 'wait'}`}>
        <Icon name={ready ? 'check' : 'circle'} size={18} />
      </span>
      <span className="ready-name">
        <strong>{label}</strong>
        <span className="muted small">{role}</span>
      </span>
      <span className="ready-status">
        {ready ? <Badge tone="ok">준비됨</Badge> : <Badge tone="warn">{c?.reachable === false ? '연결 안 됨' : c ? '준비 중' : '확인 불가'}</Badge>}
        {c?.device && <span className="muted small">{c.device.toUpperCase()}</span>}
      </span>
      {!ready && (
        <span className="ready-detail small">
          {!c ? '상태 정보를 받지 못했어요.' : c.reachable === false ? '프로세스에 연결할 수 없어요. ./app start 로 실행 중인지 확인해 주세요.' : '모델을 불러오는 중이에요. 잠시 기다려 주세요.'}
        </span>
      )}
    </li>
  )
}

export function Setup() {
  const { health, healthError, refreshHealth, micDeviceId, setMicDeviceId, scenarios } = useApp()
  const [mics, setMics] = useState<MediaDeviceInfo[]>([])
  const [mic, setMic] = useState<MicCapture | null>(null)
  const [micError, setMicError] = useState<string | null>(null)
  const [heard, setHeard] = useState(false)
  const [playError, setPlayError] = useState<string | null>(null)
  const player = useRef<Player | null>(null)
  const voiceId = useVoiceId(scenarios[0])

  // Poll readiness while something is still warming up.
  useEffect(() => {
    if (health?.ready) return
    const t = setInterval(() => void refreshHealth(), 3000)
    return () => clearInterval(t)
  }, [health?.ready, refreshHealth])

  const openMic = useCallback(
    async (deviceId: string) => {
      mic?.close()
      setMicError(null)
      setHeard(false)
      try {
        const m = await MicCapture.open(deviceId || undefined, (_pcm, rms) => {
          if (rms > 0.02) setHeard(true)
        })
        setMic(m)
        setMics(await listMicrophones())
      } catch (e) {
        setMic(null)
        setMicError(toApiError(e).messageKo)
      }
    },
    [mic],
  )

  useEffect(() => () => mic?.close(), [mic])
  useEffect(() => () => player.current?.close(), [])

  const getLevel = useCallback(() => mic?.level ?? 0, [mic])

  const ensurePlayer = async () => (player.current ??= await Player.create())

  const testSound = async () => {
    setPlayError(null)
    try {
      ;(await ensurePlayer()).playClip('local:test', chime(), 24000)
    } catch (e) {
      setPlayError(toApiError(e).messageKo)
    }
  }

  const testVoice = async () => {
    setPlayError(null)
    const first = scenarios[0]
    if (!first) return
    try {
      const { samples, sampleRate } = decodeWav(await api.cachedTts(voiceId, first.opening_line.text_id))
      const f = new Float32Array(samples.length)
      for (let i = 0; i < samples.length; i++) f[i] = samples[i]! / 0x8000
      ;(await ensurePlayer()).playClip('local:test', f, sampleRate)
    } catch (e) {
      setPlayError(toApiError(e).messageKo)
    }
  }

  const ttsReady = health?.components.tts?.ready ?? false

  return (
    <main className="page" aria-labelledby="setup-title">
      <header className="page-head">
        <p className="eyebrow">시작 전 점검</p>
        <h1 id="setup-title">마이크와 모델을 확인할게요</h1>
        <p className="lead">모든 처리는 이 컴퓨터 안에서만 이뤄져요. 음성은 저장하지 않아요.</p>
      </header>

      <div className="grid-2">
        <section className="card" aria-labelledby="mic-h">
          <h2 id="mic-h">
            <Icon name="mic" /> 마이크
          </h2>
          {!mic ? (
            <ActionButton className="btn btn-primary" icon="mic" onClick={() => void openMic(micDeviceId)}>
              마이크 켜고 확인하기
            </ActionButton>
          ) : (
            <>
              <label className="field">
                <span>입력 장치</span>
                <select
                  value={micDeviceId}
                  onChange={(e) => {
                    setMicDeviceId(e.target.value)
                    void openMic(e.target.value)
                  }}
                >
                  <option value="">시스템 기본 마이크</option>
                  {mics.map((d) => (
                    <option key={d.deviceId} value={d.deviceId}>
                      {d.label || '이름 없는 마이크'}
                    </option>
                  ))}
                </select>
              </label>
              <LevelMeter getLevel={getLevel} />
              <p className="status-line" aria-live="polite">
                <Icon name={heard ? 'check' : 'mic'} size={18} />
                {heard ? '목소리가 잘 들려요.' : '아무 말이나 해 보세요. 막대가 움직이면 정상이에요.'}
              </p>
              <p className="muted small">장치 샘플레이트 {mic.deviceRate.toLocaleString()} Hz → 16,000 Hz로 변환해 보내요.</p>
              <button type="button" className="btn btn-ghost" onClick={() => { mic.close(); setMic(null) }}>
                마이크 끄기
              </button>
            </>
          )}
          {micError && <Notice tone="danger" icon="alert">{micError}</Notice>}
          <Notice icon="headset">스피커 소리가 마이크로 다시 들어가면 AI 말을 내 말로 착각할 수 있어요. 헤드셋이나 이어폰을 권장해요.</Notice>
        </section>

        <section className="card" aria-labelledby="out-h">
          <h2 id="out-h">
            <Icon name="speaker" /> 소리 출력
          </h2>
          <div className="row">
            <ActionButton className="btn btn-soft" icon="play" onClick={() => void testSound()}>
              테스트 소리 재생
            </ActionButton>
            <ActionButton
              className="btn btn-soft"
              icon="speaker"
              onClick={() => void testVoice()}
              disabledReason={!ttsReady ? '음성 합성 모델이 준비되면 들을 수 있어요' : !scenarios.length ? '시나리오를 불러오지 못했어요' : null}
            >
              AI 목소리 들어 보기
            </ActionButton>
          </div>
          {playError && <Notice tone="danger" icon="alert">{playError}</Notice>}
        </section>
      </div>

      <section className="card" aria-labelledby="model-h">
        <div className="card-head">
          <h2 id="model-h">모델 준비 상태</h2>
          <button type="button" className="btn btn-ghost" onClick={() => void refreshHealth()}>
            <Icon name="replay" /> 다시 확인
          </button>
        </div>
        {healthError && <Notice tone="danger" icon="alert">{healthError.messageKo}</Notice>}
        <ul className="ready-list" aria-live="polite">
          {COMPONENTS.map((c) => (
            <ReadyRow key={c.key} label={c.label} role={c.role} c={health?.components[c.key]} />
          ))}
        </ul>
        <h3>사용 가능한 모드</h3>
        <ul className="mode-list">
          {(
            [
              ['realtime', '실시간 회화'],
              ['recorded', '녹음형 연습'],
            ] as const
          ).map(([k, label]) => {
            const m = health?.modes[k]
            return (
              <li key={k}>
                <Icon name={m?.available ? 'check' : 'close'} size={18} />
                <strong>{label}</strong> {m?.available ? '사용 가능' : `사용 불가 — ${m?.reason_ko ?? '모델 준비를 기다리고 있어요'}`}
              </li>
            )
          })}
        </ul>
      </section>

      <div className="page-actions">
        <button type="button" className="btn btn-primary btn-lg" onClick={() => { mic?.close(); setMic(null); navigate('home') }}>
          학습 홈으로
        </button>
      </div>
    </main>
  )
}
