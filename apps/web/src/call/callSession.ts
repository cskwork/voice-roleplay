import { audioContext, MicCapture, Player, type PlaybackEvent, type PlaybackPosition } from '../audio/engine'
import { api } from '../lib/api'
import type { OutputAudioHeader } from '../lib/envelope'
import { ApiError, messageFor, toApiError } from '../lib/errors'
import { floatToPcm16, pcm16ToFloat } from '../lib/pcm'
import { PlaybackController, type PlaybackSink } from '../lib/playbackQueue'
import { resample } from '../lib/resample'
import { endedBy, guardSessionEnd, type LeaveGuard } from '../lib/sessionEnd'
import type { CallPlan, Difficulty } from '../lib/types'
import { RealtimeSocket, type ServerEvent } from '../session/socket'
import { MouthTrack, type Mouth } from './mouth'
import { concat, Wsola } from './pitch'
import { connected, hungUp, ring } from './sounds'

/** Seconds and milliseconds measured on the reference calls (2026-11-25); the screens move on these beats. */
export const BEAT = { ring: 4600, fade: 250, gap: 220, unlock: 300, react: 200, empty: 300, bye: 900, out: 340 }

export type CallPhase = 'dialing' | 'pickup' | 'speaking' | 'locked' | 'turn' | 'listening' | 'sending' | 'thinking' | 'retry' | 'ended'

export interface HamLine {
  who: 'ham'
  key: string
  responseId: string
  seg: string
  en: string
  ko: string | null
}
export interface MyLine {
  who: 'me'
  key: string
  text: string
}
export type CallLine = HamLine | MyLine

/** The line being heard right now: position, mouth and word times come from its audio. */
export interface Voice {
  position(): number
  mouth(): Mouth | null
  words(): number[]
}

export interface CallSnapshot {
  phase: CallPhase
  title: { ko: string; en: string } | null
  /** Learner turns answered so far, out of `turns`. */
  turn: number
  turns: number
  line: HamLine | null
  lines: CallLine[]
  tip: string
  slow: boolean
  /** Why the call could not start or broke off. */
  error: ApiError | null
  endedBy: 'bye' | 'quit' | 'error' | null
}

/** A call before anything has happened. */
export const IDLE: CallSnapshot = { phase: 'dialing', title: null, turn: 0, turns: 4, line: null, lines: [], tip: '', slow: false, error: null, endedBy: null }

/** What the screen does with the character at each moment. */
export interface CallView {
  line(voice: Voice, act: string | null): void
  lineEnd(): void
  phase(phase: CallPhase): void
}

interface Segment {
  line: HamLine
  act: string | null
  voice: { mouth: MouthTrack; wsola: Wsola | null; rate: number; chunks: Float32Array[]; header: OutputAudioHeader; complete: boolean } | null
  state: 'waiting' | 'playing' | 'done'
}

interface Reply {
  id: string
  step: string | null
  final: boolean
  done: boolean
  segments: Map<string, Segment>
}

const REPLAY = 'local:replay:'
/** Reading time of a line whose voice could not be made. */
const readMs = (text: string) => 900 + text.length * 55

/** Holds audio back while the phone is still ringing. */
class HeldSink implements PlaybackSink {
  private held: (() => void)[] | null = []
  constructor(private readonly inner: PlaybackSink) {}
  push(id: string, seg: string, x: Float32Array): void {
    this.run(() => this.inner.push(id, seg, x))
  }
  end(id: string, seg: string): void {
    this.run(() => this.inner.end(id, seg))
  }
  flush(): void {
    if (this.held) this.held = []
    this.inner.flush()
  }
  release(): void {
    const held = this.held ?? []
    this.held = null
    for (const f of held) f()
  }
  private run(f: () => void): void {
    if (this.held) this.held.push(f)
    else f()
  }
}

/**
 * One character call: dial, pick up, then turns (the character speaks, the learner taps to speak and taps to send)
 * until the character says goodbye. Nothing is stored: lines and audio live in this object only.
 */
export class CallSession {
  private snap = IDLE
  private readonly listeners = new Set<() => void>()
  private readonly abort = new AbortController()
  private ctx: AudioContext | null = null
  private socket: RealtimeSocket | null = null
  private guard: LeaveGuard | null = null
  private mic: MicCapture | null = null
  private player: Player | null = null
  private sink: HeldSink | null = null
  private playback: PlaybackController | null = null
  private plan: CallPlan | null = null
  private reply: Reply | null = null
  private replies = new Map<string, Reply>()
  private glosses = new Map<string, string>()
  private openSegment: Segment | null = null
  private position: PlaybackPosition | null = null
  private peak = 0
  private empties = 0
  private replaying: Segment[] = []
  private mine: { key: string; pcm: Int16Array[] }[] = []
  private recording: Int16Array[] = []
  private stopRing: (() => void) | null = null
  sessionId: string | null = null
  view: CallView | null = null

  constructor(
    private readonly callId: string,
    private readonly difficulty: Difficulty,
    private readonly micDeviceId: string,
  ) {}

  // --- store ---------------------------------------------------------------
  get snapshot(): CallSnapshot {
    return this.snap
  }
  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn)
    return () => this.listeners.delete(fn)
  }
  private set(patch: Partial<CallSnapshot>): void {
    this.snap = { ...this.snap, ...patch }
    for (const l of this.listeners) l()
  }
  private go(phase: CallPhase, patch: Partial<CallSnapshot> = {}): void {
    if (this.snap.phase === 'ended') return
    this.set({ ...patch, phase })
    this.view?.phase(phase)
  }
  private line(key: string, patch: Partial<HamLine>): void {
    const lines = this.snap.lines.map((l) => (l.key === key && l.who === 'ham' ? { ...l, ...patch } : l))
    const line = this.snap.line?.key === key ? { ...this.snap.line, ...patch } : this.snap.line
    this.set({ lines, line })
  }
  private wait(ms: number): Promise<void> {
    const { signal } = this.abort
    return new Promise((resolve, reject) => {
      if (signal.aborted) return reject(signal.reason)
      const stop = () => {
        clearTimeout(t)
        reject(signal.reason)
      }
      const t = setTimeout(() => {
        signal.removeEventListener('abort', stop)
        resolve()
      }, ms)
      signal.addEventListener('abort', stop, { once: true })
    })
  }

  get micLevel(): number {
    return this.mic?.level ?? 0
  }

  // --- dialing ---------------------------------------------------------------
  /** Rings while the microphone, the session and the first line are made ready, then resolves when it is time to pick up. */
  async dial(ctx: AudioContext, startedAt = performance.now()): Promise<void> {
    const { signal } = this.abort
    this.ctx = ctx
    this.stopRing = ring(ctx)
    try {
      await Promise.all([this.openMic(), this.openSession()])
      signal.throwIfAborted()
      await this.wait(Math.max(0, BEAT.ring - (performance.now() - startedAt)))
      this.stopRing()
      connected(ctx)
    } catch (e) {
      this.stopRing()
      if (signal.aborted) throw e
      const error = toApiError(e)
      this.release()
      this.set({ error })
      throw e
    }
  }

  /** The sound could not be opened, so there was nothing to dial with. */
  failed(e: unknown): void {
    if (!this.abort.signal.aborted) this.set({ error: toApiError(e) })
  }

  private async openMic(): Promise<void> {
    const mic = await MicCapture.open(this.micDeviceId || undefined, (pcm) => this.onMicFrame(pcm)).catch((e: unknown) => {
      const known = toApiError(e)
      throw known.code === 'UNKNOWN' ? new ApiError('MIC_FAILED', messageFor('MIC_FAILED')) : known
    })
    if (this.abort.signal.aborted) return mic.close()
    mic.setMuted(true) // only listens between [눌러서 말하기] and [눌러서 보내기]
    this.mic = mic
  }

  private async openSession(): Promise<void> {
    const { session_id, scenario } = await api.createCall(this.callId, this.difficulty)
    this.sessionId = session_id
    this.guard = guardSessionEnd(session_id, { end: api.endSession, endOnUnload: api.endSessionOnUnload })
    this.stopIfAborted()
    this.plan = scenario.call
    this.set({ title: { ko: scenario.title_ko, en: scenario.title_en }, turns: scenario.call.turns })
    this.player = await Player.create()
    this.player.onEvent = (e) => this.onPlayback(e)
    this.player.watch((p) => (this.position = p))
    this.sink = new HeldSink(this.player)
    this.playback = new PlaybackController(this.sink, this.player.rate)
    this.socket = new RealtimeSocket(session_id, {
      onEvent: (e) => this.onServerEvent(e),
      onAudio: (h, pcm) => this.onAudio(h, pcm),
      onClose: ({ code }) => this.onClose(code),
    })
    await this.socket.connect()
    this.stopIfAborted()
    this.socket.send('session.start')
    this.socket.send('settings.update', { auto_barge_in: false, slow: this.snap.slow })
  }

  /** Hung up while this was being prepared: undo what release() could not see yet. */
  private stopIfAborted(): void {
    if (!this.abort.signal.aborted) return
    this.release()
    this.player?.close()
    this.abort.signal.throwIfAborted()
  }

  /** The character has picked up: the first line may be heard. */
  pickedUp(): void {
    if (this.snap.phase !== 'pickup') return
    this.go('speaking')
    this.sink?.release()
    if (this.reply) this.checkReply(this.reply)
  }

  startPickup(): void {
    this.go('pickup')
  }

  // --- learner turn ------------------------------------------------------------
  /** The big button: starts listening, or sends what was said, or asks for the answer again. */
  press(): void {
    const { phase } = this.snap
    if (phase === 'turn') {
      this.socket?.startManualTurn()
      this.recording = []
      this.mic?.setMuted(false)
      this.go('listening', { tip: '' })
    } else if (phase === 'listening') {
      this.mic?.setMuted(true)
      this.go('sending', { tip: '' })
      this.socket?.commit()
    } else if (phase === 'retry') {
      this.go('thinking', { tip: '' })
      this.socket?.send('response.retry')
    }
  }

  private onMicFrame(pcm: Int16Array): void {
    if (this.snap.phase !== 'listening') return
    this.socket?.sendAudio(pcm)
    this.recording.push(pcm.slice())
  }

  private async myTurn(tip = ''): Promise<void> {
    await this.wait(BEAT.unlock)
    this.go('turn', { tip })
  }

  private async nothingHeard(): Promise<void> {
    this.empties += 1
    await this.wait(BEAT.empty)
    this.go('turn', { tip: this.empties >= 2 ? '소리가 들리지 않았어요. 마이크 가까이에서 말해 보세요.' : '' })
  }

  // --- character voice ----------------------------------------------------------
  private onAudio(h: OutputAudioHeader, pcm: Int16Array): void {
    const reply = this.replies.get(h.response_id)
    const seg = reply?.segments.get(String(h.segment_id ?? '0'))
    if (!reply || !seg) return
    if (this.openSegment !== seg) {
      const before = this.openSegment
      this.closeVoice()
      this.openSegment = seg
      const pitch = this.plan?.voice_pitch ?? 1
      const rate = Math.round(h.sample_rate * pitch)
      seg.voice = { mouth: new MouthTrack(rate, pitch, this.peak * 0.85), wsola: pitch > 1 ? new Wsola(h.sample_rate, pitch) : null, rate, chunks: [], header: h, complete: false }
      // A short breath between two sentences of one reply.
      if (before?.line.responseId === reply.id) this.emit(seg, new Float32Array(Math.round((rate * BEAT.gap) / 1000)))
    }
    const x = pcm16ToFloat(pcm)
    this.emit(seg, seg.voice!.wsola?.push(x) ?? x)
  }

  private emit(seg: Segment, x: Float32Array): void {
    const v = seg.voice!
    if (!x.length) return
    v.mouth.push(x)
    v.chunks.push(x)
    this.playback?.handleAudio({ ...v.header, sample_rate: v.rate, sample_count: x.length }, floatToPcm16(x))
  }

  private closeVoice(): void {
    const seg = this.openSegment
    this.openSegment = null
    if (!seg?.voice || seg.voice.complete) return
    if (seg.voice.wsola) this.emit(seg, seg.voice.wsola.flush())
    seg.voice.complete = true
    this.peak = Math.max(this.peak, seg.voice.mouth.loudest)
  }

  private voiceOf(seg: Segment, key: string): Voice {
    const latency = () => this.ctx?.outputLatency || 0
    const position = () => {
      const p = this.position
      if (!p || p.key !== key || p.seg !== seg.line.seg || !this.ctx) return -1
      // Between reports the position runs on with the clock, but not past a stalled stream.
      return p.playedMs / 1000 + Math.min(0.08, this.ctx.currentTime - p.at) - latency()
    }
    return {
      position,
      mouth: () => seg.voice?.mouth.at(position()) ?? { shape: 'rest', level: 0 },
      words: () => seg.voice?.mouth.wordTimes(seg.line.en, seg.voice.complete) ?? [],
    }
  }

  private onPlayback(e: PlaybackEvent): void {
    if (e.type === 'flushed') return
    if (e.key.startsWith(REPLAY)) return this.onReplay(e.type, e.key.slice(REPLAY.length), e.seg)
    const seg = this.replies.get(e.key)?.segments.get(e.seg)
    if (!seg) return
    const wire = /^\d+$/.test(e.seg) ? Number(e.seg) : e.seg
    if (e.type === 'started') {
      this.socket?.send('playback.started', { response_id: e.key, segment_id: wire })
      seg.state = 'playing'
      if (this.snap.phase !== 'speaking') this.go('speaking')
      this.set({ line: seg.line })
      this.view?.line(this.voiceOf(seg, e.key), seg.act)
      return
    }
    this.socket?.send(e.type === 'completed' ? 'playback.completed' : 'playback.stopped', { response_id: e.key, segment_id: wire, played_ms: Math.round(e.playedMs) })
    seg.state = 'done'
    this.view?.lineEnd()
    this.checkReply(this.replies.get(e.key)!)
  }

  /** A line whose voice could not be made is shown long enough to read. */
  private showSilently(seg: Segment): void {
    seg.state = 'playing'
    if (this.snap.phase !== 'speaking') this.go('speaking')
    this.set({ line: seg.line })
    this.view?.line({ position: () => 99, mouth: () => ({ shape: 'rest', level: 0 }), words: () => [] }, seg.act)
    void this.wait(readMs(seg.line.en)).then(
      () => {
        seg.state = 'done'
        this.view?.lineEnd()
        const reply = this.replies.get(seg.line.responseId)
        if (reply) this.checkReply(reply)
      },
      () => {},
    )
  }

  private checkReply(reply: Reply): void {
    if (reply !== this.reply || !reply.done || reply.segments.size === 0) return
    const left = [...reply.segments.values()].filter((s) => s.state !== 'done')
    if (left.some((s) => s.state === 'playing')) return
    const silent = left.find((s) => !s.voice)
    if (silent) return this.showSilently(silent)
    if (left.length) return
    if (this.snap.phase === 'ended' || this.snap.phase === 'locked') return
    this.go('locked')
    if (reply.final) void this.sayGoodbye().catch(() => {})
    else void this.myTurn().catch(() => {})
  }

  private async sayGoodbye(): Promise<void> {
    this.set({ turn: this.snap.turns })
    await this.wait(BEAT.bye)
    if (this.ctx) hungUp(this.ctx)
    this.finish('bye')
  }

  // --- 다시 듣기 -----------------------------------------------------------------
  private get heard(): Segment[] {
    return [...(this.reply?.segments.values() ?? [])].filter((s) => s.voice)
  }

  /** False when the last turn was only shown as text: there is nothing to hear again. */
  get canReplay(): boolean {
    return this.snap.phase === 'turn' && this.heard.length > 0
  }

  /** The character says its last turn again (from memory, nothing is synthesised). */
  replayLast(): void {
    if (!this.canReplay) return
    this.playSegments(this.heard)
    this.go('speaking')
  }

  /** Review screen: one line again. */
  replayLine(line: CallLine): void {
    if (!this.player) return
    this.player.flush()
    if (line.who === 'me') {
      const take = this.mine.find((m) => m.key === line.key)
      if (take) this.player.playClip(`${REPLAY}me`, concat(take.pcm.map(pcm16ToFloat)), 16000)
      return
    }
    const seg = this.replies.get(line.responseId)?.segments.get(line.seg)
    if (seg?.voice) this.playSegments([seg])
  }

  canHear(line: CallLine): boolean {
    return line.who === 'me' ? this.mine.some((m) => m.key === line.key) : !!this.replies.get(line.responseId)?.segments.get(line.seg)?.voice
  }

  private playSegments(segments: Segment[]): void {
    this.replaying = segments
    for (const s of segments) {
      const v = s.voice!
      this.player?.push(REPLAY + s.line.responseId, s.line.seg, resample(concat(v.chunks), v.rate, this.player.rate))
      this.player?.end(REPLAY + s.line.responseId, s.line.seg)
    }
  }

  private onReplay(type: PlaybackEvent['type'], id: string, segId: string): void {
    if (this.snap.phase !== 'speaking') return
    const seg = this.replaying.find((s) => s.line.responseId === id && s.line.seg === segId)
    if (!seg) return
    if (type === 'started') {
      this.set({ line: seg.line })
      this.view?.line(this.voiceOf(seg, REPLAY + id), null)
    } else {
      this.view?.lineEnd()
      if (seg === this.replaying.at(-1)) {
        this.go('locked')
        void this.myTurn().catch(() => {})
      }
    }
  }

  // --- gateway events --------------------------------------------------------------
  private onServerEvent(e: ServerEvent): void {
    if (typeof e.epoch === 'number') this.playback?.observeEpoch(e.epoch)
    const str = (k: string) => (typeof e[k] === 'string' ? (e[k] as string) : '')
    switch (e.type) {
      case 'response.started': {
        const reply: Reply = { id: str('response_id'), step: str('step') || null, final: e.final === true, done: false, segments: new Map() }
        this.reply = reply
        this.replies.set(reply.id, reply)
        if (this.snap.phase === 'sending') this.go('thinking', { turn: typeof e.turn === 'number' ? e.turn : this.snap.turn })
        break
      }
      case 'response.text': {
        const reply = this.replies.get(str('response_id'))
        if (!reply) break
        const seg = String(e.segment_id ?? '0')
        const key = `${reply.id}:${seg}`
        // The meaning may arrive before the line itself: later lines are announced one by one, as their voice is made.
        const line: HamLine = { who: 'ham', key, responseId: reply.id, seg, en: str('text'), ko: str('text_ko') || this.glosses.get(key) || null }
        reply.segments.set(seg, { line, act: str('act') || (reply.step === 'bye' ? 'bye' : null), voice: null, state: 'waiting' })
        this.set({ lines: [...this.snap.lines, line] })
        break
      }
      case 'response.gloss': {
        const segId = String(e.segment_id ?? '0')
        const key = `${str('response_id')}:${segId}`
        const ko = str('text_ko')
        this.glosses.set(key, ko)
        this.line(key, { ko })
        const seg = this.replies.get(str('response_id'))?.segments.get(segId)
        if (seg) seg.line = { ...seg.line, ko }
        break
      }
      case 'response.done':
      case 'response.cancelled': {
        const reply = this.replies.get(str('response_id'))
        if (!reply) break
        reply.done = true
        if (e.type === 'response.done') {
          this.closeVoice()
          this.playback?.endResponse(reply.id)
        } else this.playback?.cancel(reply.id)
        // The first reply waits for the phone to be picked up before anything is shown.
        if (this.snap.phase !== 'dialing' && this.snap.phase !== 'pickup') this.checkReply(reply)
        break
      }
      case 'speech.ended':
        if (e.discarded === true) {
          this.mic?.setMuted(true)
          if (this.snap.phase === 'listening' || this.snap.phase === 'sending') void this.nothingHeard().catch(() => {})
        } else if (this.snap.phase === 'listening') {
          // 45 s: the gateway sent it for the learner.
          this.mic?.setMuted(true)
          this.socket?.endTurn(str('turn_id'))
          this.go('sending', { tip: '' })
        }
        break
      case 'asr.final': {
        const text = str('text').trim()
        if (!text) {
          void this.nothingHeard().catch(() => {})
          break
        }
        this.empties = 0
        const key = str('turn_id')
        if (this.snap.lines.some((l) => l.key === key)) break
        this.mine.push({ key, pcm: this.recording })
        this.recording = []
        this.set({ lines: [...this.snap.lines, { who: 'me', key, text }] })
        break
      }
      case 'turn.warning':
        this.set({ tip: '곧 여기까지 말한 내용을 보내요.' })
        break
      case 'error':
        this.onError(str('code'), e)
        break
    }
  }

  private onError(code: string, e: ServerEvent): void {
    if (code === 'ASR_FAILED') {
      this.mic?.setMuted(true)
      void this.myTurn('잘 못 들었어요. 한 번만 더 말해 주세요.').catch(() => {})
    } else if (code === 'LLM_FAILED') {
      const reply = this.reply
      if (!reply?.segments.size) return this.go('retry', { tip: '햄찌가 대답을 놓쳤어요. 아래 버튼을 눌러 주세요.' })
      // Broke off half way: go on with what was said.
      reply.done = true
      this.closeVoice()
      this.playback?.endResponse(reply.id)
      this.checkReply(reply)
    }
    // TTS_FAILED: the segment keeps no voice and is shown as text (checkReply).
    else if (code === 'TTS_FAILED') {
      const seg = this.replies.get(typeof e.response_id === 'string' ? e.response_id : '')?.segments.get(String(e.segment_id ?? '0'))
      if (seg && this.openSegment === seg) this.closeVoice()
    }
  }

  private onClose(code: number): void {
    if (this.snap.phase === 'ended' || this.socket?.closedByClient) return
    const by = endedBy(code)
    this.fail(new ApiError(by === 'superseded' ? 'SUPERSEDED' : 'NETWORK', by === 'superseded' ? '다른 연습을 시작해 통화가 끝났어요.' : by ? '통화 시간이 다 되어 끊겼어요.' : '서버와 연결이 끊겨 통화가 끝났어요.'))
  }

  private fail(error: ApiError): void {
    this.set({ error })
    if (this.snap.phase === 'dialing') this.release()
    else this.finish('error')
  }

  // --- end -------------------------------------------------------------------------
  setSlow(slow: boolean): void {
    this.socket?.send('settings.update', { slow })
    this.set({ slow })
  }

  quit(): void {
    if (this.snap.phase === 'ended') return
    if (this.ctx) hungUp(this.ctx)
    this.playback?.stopNow(this.reply?.id)
    this.finish('quit')
  }

  private finish(endedBy: NonNullable<CallSnapshot['endedBy']>): void {
    if (this.snap.phase === 'ended') return
    this.go('ended', { endedBy, tip: '' })
    this.release()
  }

  /** Mic and socket off, gateway session ended; lines and audio stay for the review. */
  private release(): void {
    if (!this.abort.signal.aborted) this.abort.abort(new DOMException('call ended', 'AbortError'))
    this.stopRing?.()
    this.mic?.close()
    this.mic = null
    this.socket?.close()
    this.guard?.release()
  }

  /** The screen is gone. */
  dispose(): void {
    this.view = null
    this.release()
    this.player?.close()
    this.player = null
  }
}

/** Microphone problems get their own advice on the dialing screen. */
export function dialAdvice(e: ApiError): string {
  if (e.code === 'MIC_DENIED') return '주소창 옆 마이크 아이콘에서 이 사이트의 마이크를 허용한 뒤 다시 걸어 주세요.'
  return messageFor(e.code, e.messageKo)
}

/** The shared audio context once it really runs, or null when the browser wants a tap first. */
export async function runningContext(timeoutMs = 900): Promise<AudioContext | null> {
  const ctx = await Promise.race([audioContext(), new Promise<null>((r) => setTimeout(() => r(null), timeoutMs))])
  return ctx?.state === 'running' ? ctx : null
}
