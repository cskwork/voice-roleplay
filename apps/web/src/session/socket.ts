import { decodeFrame, encodeFrame, FrameError, type OutputAudioHeader } from '../lib/envelope'

export interface ServerEvent {
  type: string
  event_id?: string
  session_id?: string
  epoch?: number
  event_seq?: number
  [key: string]: unknown
}

export interface SocketHandlers {
  onEvent(e: ServerEvent): void
  onAudio(header: OutputAudioHeader, pcm: Int16Array): void
  onClose(clean: boolean): void
}

const BACKLOG_WARN_BYTES = 2 * 16000 * 2 // ~2 s of 16 kHz PCM16

/** Realtime WebSocket (PROTOCOL §6.3): JSON control events + binary audio envelopes. */
export class RealtimeSocket {
  private ws: WebSocket | null = null
  private eventSeq = 0
  private audioSeq = 0
  epoch = 0
  turnId: string | null = null
  turnFrames = 0
  closedByClient = false

  constructor(
    readonly sessionId: string,
    private readonly handlers: SocketHandlers,
  ) {}

  connect(): Promise<void> {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/api/sessions/${encodeURIComponent(this.sessionId)}/realtime`)
    ws.binaryType = 'arraybuffer'
    this.ws = ws
    return new Promise((resolve, reject) => {
      ws.onopen = () => resolve()
      ws.onerror = () => reject(new Error('ws error'))
      ws.onclose = (e) => {
        reject(new Error('ws closed'))
        this.handlers.onClose(this.closedByClient || e.code === 1000)
      }
      ws.onmessage = (m: MessageEvent<string | ArrayBuffer>) => {
        if (typeof m.data === 'string') {
          let e: ServerEvent
          try {
            e = JSON.parse(m.data) as ServerEvent
          } catch {
            return
          }
          if (typeof e.epoch === 'number' && e.epoch > this.epoch) this.epoch = e.epoch
          this.handlers.onEvent(e)
          return
        }
        try {
          const { header, pcm } = decodeFrame(m.data)
          if (header.kind === 'output_audio' && header.session_id === this.sessionId) this.handlers.onAudio(header, pcm)
        } catch (err) {
          if (!(err instanceof FrameError)) throw err
          // Malformed frame: drop it, as the gateway does.
        }
      }
    })
  }

  get open(): boolean {
    return this.ws?.readyState === WebSocket.OPEN
  }

  /** True when more than ~2 s of audio is waiting to be sent (PRD §10.2). */
  get backlogged(): boolean {
    return (this.ws?.bufferedAmount ?? 0) > BACKLOG_WARN_BYTES
  }

  send(type: string, fields: Record<string, unknown> = {}): void {
    if (!this.open) return
    this.ws!.send(
      JSON.stringify({
        type,
        event_id: crypto.randomUUID(),
        session_id: this.sessionId,
        epoch: this.epoch,
        event_seq: ++this.eventSeq,
        ...fields,
      }),
    )
  }

  /**
   * Streams one 20 ms frame. The frame's `turn_id` is the client's proposal for the next
   * utterance; the gateway's VAD decides where turns start and end.
   */
  sendAudio(pcm: Int16Array): void {
    if (!this.open) return
    if (!this.turnId) {
      this.turnId = crypto.randomUUID()
      this.turnFrames = 0
    }
    const seq = ++this.audioSeq
    this.ws!.send(
      encodeFrame(
        {
          v: 1,
          kind: 'input_audio',
          session_id: this.sessionId,
          turn_id: this.turnId,
          epoch: this.epoch,
          seq,
          sample_rate: 16000,
          sample_count: pcm.length,
        },
        pcm,
      ),
    )
    this.turnFrames++
  }

  /** Push-to-talk press: `input.start` opens a manual utterance (and cancels AI output on the gateway). */
  startManualTurn(): void {
    this.turnId = crypto.randomUUID()
    this.turnFrames = 0
    this.send('input.start', { turn_id: this.turnId })
  }

  /** Manual end of turn (`말하기 완료` / push-to-talk release). */
  commit(): string | null {
    const turnId = this.turnId
    if (!turnId) return null
    this.send('input.commit', { turn_id: turnId, last_seq: this.audioSeq })
    this.endTurn(turnId)
    return turnId
  }

  /** The gateway ended this turn (speech.ended); later audio starts a new turn. */
  endTurn(turnId: string): void {
    if (this.turnId === turnId) {
      this.turnId = null
      this.turnFrames = 0
    }
  }

  close(): void {
    this.closedByClient = true
    this.ws?.close(1000)
  }
}
