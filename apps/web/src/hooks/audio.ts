import { useCallback, useEffect, useRef, useState } from 'react'
import { MicCapture } from '../audio/engine'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import { encodeWav } from '../lib/wav'
import { TERMINAL_JOB_STATES, type Job } from '../lib/types'

// --- model audio (TTS WAV from the gateway) ----------------------------------------
let current: HTMLAudioElement | null = null

export function stopModelAudio(): void {
  if (current) {
    current.pause()
    URL.revokeObjectURL(current.src)
    current = null
  }
}

async function playWav(wav: ArrayBuffer): Promise<void> {
  stopModelAudio()
  const el = new Audio(URL.createObjectURL(new Blob([wav], { type: 'audio/wav' })))
  current = el
  el.onended = () => {
    if (current === el) stopModelAudio()
  }
  await el.play()
}

/** `url`: model audio already produced for an attempt; `textId`: reviewed scenario text (cached). */
export type ModelAudioSource = { textId?: string; text: string; url?: string }

const SLOW_SPEED = 0.85 // same as the gateway's slow style

/** Plays reviewed model audio: cached by text_id at normal speed, else synthesized. */
export function useModelAudio(voiceId: string) {
  const [busy, setBusy] = useState<'normal' | 'slow' | null>(null)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => stopModelAudio, [])
  const play = useCallback(
    async (src: ModelAudioSource, slow: boolean) => {
      setBusy(slow ? 'slow' : 'normal')
      setError(null)
      try {
        const wav =
          !slow && src.url
            ? await api.audioUrl(src.url)
            : src.textId
              ? await api.cachedTts(voiceId, src.textId, slow)
              : await api.tts(voiceId, src.text, slow ? SLOW_SPEED : 1.0)
        await playWav(wav)
      } catch (e) {
        setError(toApiError(e).messageKo)
      } finally {
        setBusy(null)
      }
    },
    [voiceId],
  )
  return { play, busy, error }
}

// --- recorder -------------------------------------------------------------------
const PREVIEW_TTL_MS = 5 * 60 * 1000 // PRD §16: browser preview kept at most 5 minutes

export interface Take {
  wav: ArrayBuffer
  url: string
  durationMs: number
  id: string
}

/** Records 16 kHz mono PCM16 through the capture worklet and encodes a WAV take. */
export function useRecorder(deviceId: string, maxMs: number) {
  const [recording, setRecording] = useState(false)
  const [elapsedMs, setElapsed] = useState(0)
  const [take, setTake] = useState<Take | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [expired, setExpired] = useState(false)
  const mic = useRef<MicCapture | null>(null)
  const chunks = useRef<Int16Array[]>([])
  const samples = useRef(0)
  const stopRef = useRef<() => void>(() => {})

  const discard = useCallback(() => {
    setTake((t) => {
      if (t) URL.revokeObjectURL(t.url)
      return null
    })
  }, [])

  const stop = useCallback(() => {
    const m = mic.current
    if (!m) return
    m.close()
    mic.current = null
    setRecording(false)
    const all = new Int16Array(samples.current)
    let o = 0
    for (const c of chunks.current) {
      all.set(c, o)
      o += c.length
    }
    chunks.current = []
    const wav = encodeWav(all, 16000)
    setTake({ wav, url: URL.createObjectURL(new Blob([wav], { type: 'audio/wav' })), durationMs: (all.length / 16000) * 1000, id: crypto.randomUUID() })
  }, [])
  stopRef.current = stop

  const start = useCallback(async () => {
    discard()
    setError(null)
    setExpired(false)
    chunks.current = []
    samples.current = 0
    setElapsed(0)
    try {
      mic.current = await MicCapture.open(deviceId || undefined, (pcm) => {
        chunks.current.push(pcm)
        samples.current += pcm.length
        const ms = (samples.current / 16000) * 1000
        if (samples.current % 3200 === 0) setElapsed(ms)
        if (ms >= maxMs) stopRef.current()
      })
      setRecording(true)
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }, [deviceId, maxMs, discard])

  // Preview expires after 5 minutes; the take must then be re-recorded.
  useEffect(() => {
    if (!take) return
    const t = setTimeout(() => {
      discard()
      setExpired(true)
    }, PREVIEW_TTL_MS)
    return () => clearTimeout(t)
  }, [take, discard])

  useEffect(
    () => () => {
      mic.current?.close()
      mic.current = null
    },
    [],
  )
  useEffect(() => () => discard(), [discard])

  return { recording, elapsedMs, take, error, expired, start, stop, discard, level: () => mic.current?.level ?? 0 }
}

// --- job polling ----------------------------------------------------------------
export function useJobPoll(jobId: string | null, onDone: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null)
  const [error, setError] = useState<string | null>(null)
  const done = useRef(onDone)
  done.current = onDone
  useEffect(() => {
    if (!jobId) return
    let alive = true
    let timer = 0
    const tick = async () => {
      try {
        const j = await api.job(jobId)
        if (!alive) return
        setJob(j)
        if (TERMINAL_JOB_STATES.includes(j.state)) {
          done.current(j)
          return
        }
      } catch (e) {
        if (!alive) return
        setError(toApiError(e).messageKo)
        return
      }
      timer = window.setTimeout(tick, 600)
    }
    void tick()
    return () => {
      alive = false
      clearTimeout(timer)
    }
  }, [jobId])
  return { job, error, reset: () => setJob(null) }
}
