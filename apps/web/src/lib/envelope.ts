/** Binary audio frame (PROTOCOL §6.3): u32 LE header length + UTF-8 JSON header + PCM16LE payload. */
export const MAX_HEADER_BYTES = 1024
export const MAX_PAYLOAD_BYTES = 64 * 1024

export interface InputAudioHeader {
  v: 1
  kind: 'input_audio'
  session_id: string
  turn_id: string
  epoch: number
  seq: number
  sample_rate: number
  sample_count: number
}

export interface OutputAudioHeader {
  v: 1
  kind: 'output_audio'
  session_id: string
  response_id: string
  epoch: number
  seq: number
  sample_rate: number
  sample_count: number
  segment_id?: number | string
}

export type AudioHeader = InputAudioHeader | OutputAudioHeader

export class FrameError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'FrameError'
  }
}

const encoder = new TextEncoder()
const decoder = new TextDecoder('utf-8', { fatal: true })

export function encodeFrame(header: AudioHeader, pcm: Int16Array): ArrayBuffer {
  if (header.sample_count !== pcm.length) throw new FrameError('sample_count mismatch')
  const headerBytes = encoder.encode(JSON.stringify(header))
  if (headerBytes.length > MAX_HEADER_BYTES) throw new FrameError('header too large')
  if (pcm.length * 2 > MAX_PAYLOAD_BYTES) throw new FrameError('payload too large')
  const buf = new ArrayBuffer(4 + headerBytes.length + pcm.length * 2)
  const view = new DataView(buf)
  view.setUint32(0, headerBytes.length, true)
  new Uint8Array(buf, 4, headerBytes.length).set(headerBytes)
  const offset = 4 + headerBytes.length
  for (let i = 0; i < pcm.length; i++) view.setInt16(offset + i * 2, pcm[i]!, true)
  return buf
}

export function decodeFrame(buf: ArrayBuffer): { header: AudioHeader; pcm: Int16Array } {
  if (buf.byteLength < 4) throw new FrameError('frame too short')
  const view = new DataView(buf)
  const headerLen = view.getUint32(0, true)
  if (headerLen === 0 || headerLen > MAX_HEADER_BYTES) throw new FrameError('bad header length')
  if (4 + headerLen > buf.byteLength) throw new FrameError('truncated header')
  let header: AudioHeader
  try {
    header = JSON.parse(decoder.decode(new Uint8Array(buf, 4, headerLen))) as AudioHeader
  } catch {
    throw new FrameError('header is not valid JSON')
  }
  if (header === null || typeof header !== 'object') throw new FrameError('header is not an object')
  if (header.kind !== 'input_audio' && header.kind !== 'output_audio') throw new FrameError('unknown kind')
  const payloadLen = buf.byteLength - 4 - headerLen
  if (payloadLen > MAX_PAYLOAD_BYTES) throw new FrameError('payload too large')
  if (!Number.isInteger(header.sample_count) || payloadLen !== header.sample_count * 2) {
    throw new FrameError('payload length does not match sample_count')
  }
  const pcm = new Int16Array(header.sample_count)
  const offset = 4 + headerLen
  for (let i = 0; i < pcm.length; i++) pcm[i] = view.getInt16(offset + i * 2, true)
  return { header, pcm }
}
