// Page init script (runs before the app). Test instrumentation only; the app code is unchanged.
//
// FAKE-MIC-SOURCE: navigator.mediaDevices.getUserMedia({audio}) returns a MediaStream from a separate 48 kHz
// AudioContext (MediaStreamAudioDestinationNode). The test schedules fixture WAVs into it, so the app's real
// path runs: MediaStreamSource -> vr-capture AudioWorklet (downmix, sinc resample to 16 kHz, 20 ms PCM16
// frames) -> WebSocket envelope -> gateway. Set window.__e2e.realMic = true to use the browser's own device
// (e.g. Chromium --use-file-for-fake-audio-capture).
//
// Clocks: everything is recorded with performance.now() (one monotonic browser clock, PRD §15.3).
//  - mic: when = AudioContext time a fixture starts; perf(when) = perf at scheduling + (when - currentTime).
//  - playback: the app's vr-playback worklet posts started/completed/stopped/flushed from the render thread;
//    we timestamp the message on arrival and also keep the app context's base/output latency.
;(() => {
  if (window.__e2e) return
  const S = (window.__e2e = { log: [], fixtures: new Map(), realMic: false, capFrames: 0, outFrames: 0 })
  const now = () => performance.now()
  const push = (kind, data) => S.log.push({ t: now(), kind, ...data })

  // ---------------------------------------------------------------- fake microphone
  let micCtx = null
  let bus = null
  let noise = null
  const ensureCtx = () => {
    if (!micCtx) {
      micCtx = new AudioContext({ sampleRate: 48000 })
      bus = micCtx.createGain()
      // A MediaStreamDestination only pulls while connected; keep the bus rendering.
      const sink = micCtx.createGain()
      sink.gain.value = 0
      bus.connect(sink)
      sink.connect(micCtx.destination)
    }
    return micCtx
  }
  S.micReady = async () => {
    const c = ensureCtx()
    if (c.state !== 'running') await c.resume()
    return c.state
  }

  const origGum = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices)
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    if (S.realMic || !constraints || !constraints.audio) return origGum(constraints)
    await S.micReady()
    const dest = micCtx.createMediaStreamDestination()
    dest.channelCount = 1
    bus.connect(dest)
    push('gum', { rate: micCtx.sampleRate })
    return dest.stream
  }

  /** Decode a fixture and find its first/last voiced sample (10 ms RMS > 2% of the loudest window). */
  S.load = async (name) => {
    if (S.fixtures.has(name)) return S.fixtures.get(name).info
    const res = await fetch(`/__e2e/fixtures/${name}.wav`)
    if (!res.ok) throw new Error(`fixture ${name}: ${res.status}`)
    const ab = await ensureCtx().decodeAudioData(await res.arrayBuffer())
    const x = ab.getChannelData(0)
    const win = Math.round(ab.sampleRate / 100)
    const rms = []
    for (let i = 0; i + win <= x.length; i += win) {
      let s = 0
      for (let j = i; j < i + win; j++) s += x[j] * x[j]
      rms.push(Math.sqrt(s / win))
    }
    const peak = Math.max(...rms)
    const voiced = rms.map((r) => r > peak * 0.02)
    const first = voiced.indexOf(true)
    const last = voiced.lastIndexOf(true)
    const info = { name, duration: ab.duration, firstVoiced: first / 100, lastVoiced: (last + 1) / 100 }
    S.fixtures.set(name, { ab, info })
    return info
  }

  /** Schedule a loaded fixture into the fake mic; returns its timing on the performance.now() clock. */
  S.play = (name, delayS = 0.1, gain = 1) => {
    const f = S.fixtures.get(name)
    if (!f) throw new Error(`fixture ${name} not loaded`)
    const c = ensureCtx()
    const src = c.createBufferSource()
    src.buffer = f.ab
    const g = c.createGain()
    g.gain.value = gain
    src.connect(g)
    g.connect(bus)
    const p0 = now()
    const t0 = c.currentTime
    const when = t0 + delayS
    src.start(when)
    const startPerf = p0 + delayS * 1000
    const r = {
      name,
      startPerf,
      firstVoicedPerf: startPerf + f.info.firstVoiced * 1000,
      lastVoicedPerf: startPerf + f.info.lastVoiced * 1000,
      endPerf: startPerf + f.info.duration * 1000,
    }
    push('mic_play', r)
    return r
  }

  /** Steady background noise (white, level in dBFS RMS), or null to stop. */
  S.setNoise = (dbfs) => {
    const c = ensureCtx()
    if (noise) {
      noise.stop()
      noise = null
    }
    if (dbfs == null) return
    const len = c.sampleRate * 2
    const buf = c.createBuffer(1, len, c.sampleRate)
    const d = buf.getChannelData(0)
    const amp = Math.pow(10, dbfs / 20) * Math.sqrt(3) // uniform noise RMS = amp / sqrt(3)
    for (let i = 0; i < len; i++) d[i] = (Math.random() * 2 - 1) * amp
    noise = c.createBufferSource()
    noise.buffer = buf
    noise.loop = true
    noise.connect(bus)
    noise.start()
    push('noise', { dbfs })
  }

  // ---------------------------------------------------------------- app audio worklets
  const NativeAWN = window.AudioWorkletNode
  window.AudioWorkletNode = class extends NativeAWN {
    constructor(ctx, name, opts) {
      super(ctx, name, opts)
      if (name === 'vr-playback') {
        S.appCtx = ctx
        this.port.addEventListener('message', (e) => {
          const d = e.data || {}
          push('play', { type: d.type, key: d.key, seg: d.seg, playedMs: d.playedMs })
        })
      } else if (name === 'vr-capture') {
        let loud = false
        let quietFrames = 0
        this.port.addEventListener('message', (e) => {
          S.capFrames++
          const db = 20 * Math.log10(Math.max(1e-9, e.data.rms))
          if (db > -45) {
            quietFrames = 0
            if (!loud) {
              loud = true
              push('cap_onset', { db })
            }
          } else if (loud && ++quietFrames >= 15) {
            loud = false
            push('cap_offset', {})
          }
        })
      }
    }
  }

  S.outputLatencyMs = () =>
    S.appCtx ? ((S.appCtx.baseLatency || 0) + (S.appCtx.outputLatency || 0)) * 1000 : null

  // ---------------------------------------------------------------- realtime WebSocket
  const NativeWS = window.WebSocket
  const KEYS = ['type', 'session_id', 'turn_id', 'response_id', 'epoch', 'segment_id', 'text', 'reason', 'discarded', 'goals',
    'changed', 'code', 'state', 'input_state', 'output_state', 'transcript_revision', 'opening', 'count',
    'auto_barge_in', 'silence_ms', 'level', 'text_ko', 'example_en', 'keywords']
  const pick = (o) => {
    const r = {}
    for (const k of KEYS) if (o[k] !== undefined) r[k] = o[k]
    return r
  }
  window.WebSocket = class extends NativeWS {
    constructor(url, protocols) {
      super(url, protocols)
      S.ws = this
      push('ws_open', { url: String(url) })
      this.addEventListener('message', (m) => {
        if (typeof m.data === 'string') {
          try {
            push('ws_in', pick(JSON.parse(m.data)))
          } catch {
            push('ws_in', { type: '(unparsable)' })
          }
          return
        }
        const buf = m.data instanceof ArrayBuffer ? m.data : null
        if (!buf) return
        try {
          const n = new DataView(buf).getUint32(0, true)
          const h = JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, n)))
          S.outFrames++
          push('ws_audio', { response_id: h.response_id, epoch: h.epoch, segment_id: h.segment_id, seq: h.seq,
            sample_count: h.sample_count, sample_rate: h.sample_rate })
        } catch {
          push('ws_audio', { bad: true })
        }
      })
      this.addEventListener('close', (e) => push('ws_close', { code: e.code }))
    }
    send(data) {
      if (typeof data === 'string') {
        try {
          push('ws_out', pick(JSON.parse(data)))
        } catch {
          /* not JSON */
        }
      } else {
        S.sentFrames = (S.sentFrames || 0) + 1
      }
      return super.send(data)
    }
  }

  /** Log entries since index `from`, optionally filtered by kind/type. */
  S.since = (from, kind, type) => S.log.slice(from).filter((e) => (!kind || e.kind === kind) && (!type || e.type === type))
})()
