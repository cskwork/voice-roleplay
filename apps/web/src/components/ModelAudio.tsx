import { useId } from 'react'
import { useModelAudio, type ModelAudioSource } from '../hooks/audio'
import { Icon } from './Icon'

/** 모범 음성: normal and slow playback of reviewed/model sentences (never the learner's audio). */
export function ModelAudio({ voiceId, source, ttsReady, label = '모범 음성' }: { voiceId: string; source: ModelAudioSource; ttsReady: boolean; label?: string }) {
  const { play, busy, error } = useModelAudio(voiceId)
  const id = useId()
  const reason = !ttsReady ? '음성 합성 모델이 준비되지 않았어요' : !voiceId ? '사용할 음성이 없어요' : busy ? '불러오는 중…' : null
  return (
    <div className="model-audio" role="group" aria-label={label}>
      <span className="model-audio-label">{label}</span>
      <button type="button" className="btn btn-soft" onClick={() => void play(source, false)} disabled={!!reason} aria-describedby={reason ? id : undefined}>
        <Icon name="speaker" /> 보통 속도
      </button>
      <button type="button" className="btn btn-soft" onClick={() => void play(source, true)} disabled={!!reason} aria-describedby={reason ? id : undefined}>
        <Icon name="slow" /> 느리게
      </button>
      {reason && (
        <span id={id} className="disabled-reason">
          {reason}
        </span>
      )}
      {error && <span className="error-text">{error}</span>}
    </div>
  )
}
