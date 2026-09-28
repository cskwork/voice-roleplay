import { MicCapture, Player, type PlaybackEvent } from '../audio/engine'
import { BACKLOG_WARN_MS } from '../lib/backlog'
import { captionsReducer, type Caption, type CaptionAction } from '../lib/captions'
import type { OutputAudioHeader } from '../lib/envelope'
import { messageFor } from '../lib/errors'
import { endedBy, type EndedBy } from '../lib/sessionEnd'
import { LevelVad } from '../lib/levelVad'
import { PlaybackController } from '../lib/playbackQueue'
import { pcm16ToFloat } from '../lib/pcm'
import type { GoalState, SessionSummary } from '../lib/types'
import { RealtimeSocket, type ServerEvent } from './socket'

export type ServerState =
  | 'CONNECTING'
  | 'READY'
  | 'LISTENING'
  | 'FINALIZING'
  | 'RESPONDING'
  | 'INTERRUPTING'
  | 'PAUSED'
  | 'RECOVERABLE_ERROR'
  | 'CLOSED'

export interface HintView {
  level: 1 | 2 | 3
  text_ko?: string
  keywords?: string[]
  example_en?: string
  /** 'local' = built instantly from the scenario file; 'server' = the gateway's reply. */
  source: 'local' | 'server'
  /** The AI line (response_id) the hint was built for; absent when the server did not say. */
  response_id?: string
}

export interface Snapshot {
  state: ServerState
  captions: Caption[]
  goals: GoalState[]
  hint: HintView | null
  muted: boolean
  paused: boolean
  aiSpeaking: boolean
  responseActive: boolean
  canCommit: boolean
  autoBargeIn: boolean
  pushToTalk: boolean
  pttPressed: boolean
  slow: boolean
  echoCount: number
  /** More than 2 s of mic audio is waiting to be sent (PRD §10.2). Nothing is dropped; the UI warns. */
  backlogged: boolean
  warning: string | null
  error: { code?: string; message: string; recoverable: boolean } | null
  summary: SessionSummary | null
  disconnected: boolean
  /** The gateway ended this conversation: from another tab or screen (`ended`) or because something new started. */
  endedBy: EndedBy | null
}

const REPLAY_KEY = 'local:replay'

interface ReplayBuffer {
  id: string
  rate: number
  chunks: Float32Array[]
}

function wireSegmentId(seg: string): number | string {
  return /^\d+$/.test(seg) ? Number(seg) : seg
}

/**
 * One realtime conversation: mic -> gateway, gateway audio -> speaker, captions, goals,
 * hints, barge-in. UI reads `snapshot` via subscribe().
 */
export class RealtimeSession {
  private snap: Snapshot
  private listeners = new Set<() => void>()
  private socket: RealtimeSocket
  private mic: MicCapture | null = null
  private player: Player | null = null
  private playback: PlaybackController | null = null
  private readonly vad = new LevelVad()
  private activeResponse: string | null = null
  private playingKeys = new Set<string>()
  // Source-rate audio of the current and last heard response, kept in memory for 다시 듣기.
  private replay: ReplayBuffer | null = null
  private lastReplay: ReplayBuffer | null = null
  // Id of the newest hint.request; the gateway echoes it so an older, slower reply can be recognised.
  private hintSeq = 0

  constructor(
    readonly sessionId: string,
    opts: { autoBargeIn: boolean },
  ) {
    this.snap = {
      state: 'CONNECTING',
      captions: [],
      goals: [],
      hint: null,
      muted: false,
      paused: false,
      aiSpeaking: false,
      responseActive: false,
      canCommit: false,
      autoBargeIn: opts.autoBargeIn,
      pushToTalk: false,
      pttPressed: false,
      slow: false,
      echoCount: 0,
      backlogged: false,
      warning: null,
      error: null,
      summary: null,
      disconnected: false,
      endedBy: null,
    }
    this.socket = new RealtimeSocket(sessionId, {
      onEvent: (e) => this.onServerEvent(e),
      onAudio: (h, pcm) => this.onAudio(h, pcm),
      onClose: (info) => this.onSocketClose(info),
    })
  }

  // --- store ---------------------------------------------------------------
  get snapshot(): Snapshot {
    return this.snap
  }
  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  private set(patch: Partial<Snapshot>): void {
    this.snap = { ...this.snap, ...patch }
    for (const l of this.listeners) l()
  }
  private caption(a: CaptionAction): void {
    this.set({ captions: captionsReducer(this.snap.captions, a) })
  }

  get micLevel(): number {
    return this.mic?.level ?? 0
  }

  // --- lifecycle -----------------------------------------------------------
  /** `silenceMs` null = keep the session's difficulty default. */
  async start(deviceId: string | undefined, silenceMs: number | null): Promise<void> {
    this.player = await Player.create()
    this.player.onEvent = (e) => this.onPlayback(e)
    this.playback = new PlaybackController(this.player, this.player.rate)
    this.mic = await MicCapture.open(deviceId, (pcm, level) => this.onMicFrame(pcm, level))
    await this.socket.connect()
    this.socket.send('session.start', {})
    this.socket.send('settings.update', { ...(silenceMs ? { silence_ms: silenceMs } : {}), auto_barge_in: this.snap.autoBargeIn })
    this.set({ state: 'READY' })
  }

  /** Stops audio in both directions and releases the mic. Safe to call twice. */
  shutdown(): void {
    this.playback?.stopNow(this.activeResponse)
    this.mic?.close()
    this.mic = null
    this.player?.close()
    this.player = null
    this.socket.close()
  }

  endConversation(): void {
    this.socket.send('session.end', {})
    this.shutdown()
    this.set({ state: 'CLOSED', aiSpeaking: false, responseActive: false })
  }

  // --- mic -----------------------------------------------------------------
  private onMicFrame(pcm: Int16Array, level: number): void {
    const s = this.snap
    const backlogged = this.socket.backlogMs > BACKLOG_WARN_MS
    if (backlogged !== s.backlogged) this.set({ backlogged })
    if (s.paused || s.muted) return
    // Local barge-in: stop the speaker at once, without waiting for the gateway.
    if (this.vad.update(level) && s.aiSpeaking && s.autoBargeIn && !s.pushToTalk) this.interrupt()
    if (s.pushToTalk && !s.pttPressed) return
    this.socket.sendAudio(pcm)
    if (!this.snap.canCommit && this.socket.turnFrames > 0) this.set({ canCommit: true })
  }

  setMuted(muted: boolean): void {
    this.mic?.setMuted(muted)
    this.socket.send('mic.state', { muted })
    this.set({ muted })
  }

  setPaused(paused: boolean): void {
    if (paused) {
      this.interrupt()
      this.socket.send('session.pause', {})
    } else {
      this.socket.send('session.resume', {})
    }
    this.mic?.setMuted(paused || this.snap.muted)
    this.set({ paused, state: paused ? 'PAUSED' : this.snap.state })
  }

  /** `말하기 완료`. */
  commit(): void {
    if (this.socket.commit()) this.set({ canCommit: false })
  }

  /** Stop button / barge-in: silence now, then tell the gateway. */
  interrupt(): void {
    const id = this.activeResponse
    this.playback?.stopNow(id)
    if (id) {
      this.socket.send('response.cancel', { response_id: id })
      this.caption({ type: 'response.cancelled', response_id: id })
    }
    this.activeResponse = null
    this.set({ aiSpeaking: false, responseActive: false })
  }

  pushToTalk(pressed: boolean): void {
    if (!this.snap.pushToTalk || this.snap.pttPressed === pressed) return
    if (pressed) {
      if (this.snap.aiSpeaking || this.snap.responseActive) this.interrupt()
      this.socket.startManualTurn()
      this.set({ pttPressed: true })
    } else {
      this.set({ pttPressed: false })
      this.commit()
    }
  }

  setPushToTalkMode(on: boolean): void {
    this.set({ pushToTalk: on, autoBargeIn: on ? false : this.snap.autoBargeIn, pttPressed: false })
    if (on) this.socket.send('settings.update', { auto_barge_in: false })
  }

  setAutoBargeIn(on: boolean): void {
    this.socket.send('settings.update', { auto_barge_in: on })
    this.set({ autoBargeIn: on })
  }

  setSlow(slow: boolean): void {
    this.socket.send('settings.update', { slow })
    this.set({ slow })
  }

  requestHint(level: 1 | 2 | 3): void {
    this.hintSeq += 1
    this.socket.send('hint.request', { level, request_id: `h${this.hintSeq}` })
  }

  /** Plays the last AI reply again from memory (no new synthesis, not reported to the gateway). */
  replayLast(): boolean {
    const r = this.lastReplay
    if (!this.player || !r || this.snap.aiSpeaking) return false
    const total = r.chunks.reduce((n, c) => n + c.length, 0)
    const all = new Float32Array(total)
    let o = 0
    for (const c of r.chunks) {
      all.set(c, o)
      o += c.length
    }
    this.player.playClip(REPLAY_KEY, all, r.rate)
    return true
  }

  get canReplay(): boolean {
    return this.lastReplay !== null
  }

  // --- gateway events --------------------------------------------------------
  private onAudio(h: OutputAudioHeader, pcm: Int16Array): void {
    if (!this.playback?.handleAudio(h, pcm)) return
    if (this.replay?.id !== h.response_id) this.replay = { id: h.response_id, rate: h.sample_rate, chunks: [] }
    this.replay.chunks.push(pcm16ToFloat(pcm))
  }

  private onPlayback(e: PlaybackEvent): void {
    if (e.type === 'flushed') {
      this.playingKeys.clear()
      this.set({ aiSpeaking: false })
      return
    }
    if (e.key === REPLAY_KEY) return
    const seg = wireSegmentId(e.seg)
    if (e.type === 'started') {
      this.playingKeys.add(e.key)
      this.socket.send('playback.started', { response_id: e.key, segment_id: seg })
      this.caption({ type: 'segment.status', response_id: e.key, segment_id: e.seg, status: 'playing' })
      this.set({ aiSpeaking: true })
    } else if (e.type === 'completed') {
      this.playingKeys.delete(e.key)
      this.socket.send('playback.completed', { response_id: e.key, segment_id: seg })
      this.caption({ type: 'segment.status', response_id: e.key, segment_id: e.seg, status: 'played' })
      if (this.replay?.id === e.key) this.lastReplay = this.replay
      this.set({ aiSpeaking: this.playingKeys.size > 0 })
    } else {
      this.socket.send('playback.stopped', { response_id: e.key, segment_id: seg, played_ms: Math.round(e.playedMs) })
      this.caption({ type: 'segment.status', response_id: e.key, segment_id: e.seg, status: 'interrupted' })
    }
  }

  private onServerEvent(e: ServerEvent): void {
    if (typeof e.epoch === 'number') this.playback?.observeEpoch(e.epoch)
    const str = (k: string) => (typeof e[k] === 'string' ? (e[k] as string) : '')
    switch (e.type) {
      case 'session.state': {
        const state = str('state') as ServerState
        if (state) this.set({ state: this.snap.paused && state !== 'CLOSED' ? 'PAUSED' : state })
        break
      }
      case 'speech.started':
        this.caption({ type: 'speech.started', turn_id: str('turn_id') })
        if (this.snap.aiSpeaking && this.snap.autoBargeIn && !this.snap.pushToTalk) this.interrupt()
        break
      case 'speech.ended':
        this.socket.endTurn(str('turn_id'))
        this.set({ canCommit: false, warning: null })
        break
      case 'asr.partial':
        this.caption({ type: 'asr.partial', turn_id: str('turn_id'), text: str('text') })
        break
      case 'asr.final':
        this.caption({
          type: 'asr.final',
          turn_id: str('turn_id'),
          text: str('text'),
          revision: typeof e.transcript_revision === 'number' ? e.transcript_revision : undefined,
        })
        break
      case 'response.started':
        this.activeResponse = str('response_id')
        this.set({ responseActive: true, hint: null })
        break
      case 'response.text': {
        const id = str('response_id')
        if (this.playback?.isStale(id, typeof e.epoch === 'number' ? e.epoch : this.socket.epoch)) break
        this.caption({ type: 'response.text', response_id: id, segment_id: String(e.segment_id ?? '0'), text: str('text') })
        break
      }
      case 'response.done': {
        const id = str('response_id')
        this.playback?.endResponse(id)
        this.caption({ type: 'response.done', response_id: id })
        if (this.activeResponse === id) this.activeResponse = null
        this.set({ responseActive: this.activeResponse !== null })
        break
      }
      case 'response.cancelled': {
        const id = str('response_id')
        this.playback?.cancel(id)
        this.caption({ type: 'response.cancelled', response_id: id })
        if (this.activeResponse === id) this.activeResponse = null
        this.set({ responseActive: this.activeResponse !== null })
        break
      }
      case 'turn.warning':
        this.set({ warning: '40초가 지났어요. 45초가 되면 지금까지 말한 내용이 자동으로 제출돼요.' })
        break
      case 'hint': {
        const level = (e.level as 1 | 2 | 3) ?? 1
        const responseId = typeof e.response_id === 'string' ? e.response_id : undefined
        // Only the reply to the newest request counts: a slow level-1 reply (it waits on the LLM translation)
        // must not replace the answer to a later request.
        const cur = this.snap.hint
        if (typeof e.request_id === 'string') {
          if (e.request_id !== `h${this.hintSeq}`) break
        } else if (cur && cur.level > level && (!responseId || cur.response_id === responseId)) {
          break // no request id (older gateway): replies can still overtake each other, never step back a level
        }
        this.set({
          hint: {
            level,
            text_ko: e.text_ko as string | undefined,
            keywords: e.keywords as string[] | undefined,
            example_en: e.example_en as string | undefined,
            source: 'server',
            response_id: responseId,
          },
        })
        break
      }
      case 'goal.update':
        if (Array.isArray(e.goals)) this.set({ goals: e.goals as GoalState[] })
        break
      case 'feedback.ready':
        break
      case 'echo.suspected': {
        this.caption({ type: 'echo.suspected' })
        const echoCount = typeof e.count === 'number' ? e.count : this.snap.echoCount + 1
        const autoBargeIn = typeof e.auto_barge_in === 'boolean' ? e.auto_barge_in : echoCount < 2 && this.snap.autoBargeIn
        this.set({ echoCount, autoBargeIn })
        break
      }
      case 'error': {
        const code = str('code')
        if (code === 'FRAME_INVALID' || code === 'EVENT_INVALID') break // dropped silently, nothing for the learner to do
        this.set({
          error: { code, message: messageFor(code, str('message_ko')), recoverable: e.recoverable !== false },
          ...(code === 'LLM_FAILED' ? { responseActive: false } : {}),
        })
        break
      }
    }
  }

  private onSocketClose({ clean, code }: { clean: boolean; code: number }): void {
    const by = this.socket.closedByClient ? null : endedBy(code)
    if (by) {
      // Ended on purpose by the gateway (the `session.state CLOSED` event came first): calm notice, not an error.
      this.playback?.stopNow(this.activeResponse)
      this.mic?.close()
      this.mic = null
      this.set({ state: 'CLOSED', aiSpeaking: false, responseActive: false, disconnected: false, error: null, warning: null, endedBy: by })
      return
    }
    if (this.snap.state === 'CLOSED') return
    this.playback?.stopNow(this.activeResponse)
    this.mic?.close()
    this.mic = null
    this.set({
      state: 'CLOSED',
      aiSpeaking: false,
      responseActive: false,
      disconnected: !clean,
      error: clean ? this.snap.error : { message: '서버와의 연결이 끊겼어요. 마이크와 재생을 멈췄어요.', recoverable: false },
    })
  }

  /** Regenerate the reply to the last turn after LLM_FAILED (PRD §17). */
  retryResponse(): void {
    this.socket.send('response.retry', {})
    this.set({ error: null })
  }

  clearError(): void {
    this.set({ error: null })
  }

  clearHint(): void {
    this.set({ hint: null })
  }

  showLocalHint(h: HintView): void {
    this.set({ hint: h })
  }
}
