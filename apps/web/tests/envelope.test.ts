import { describe, expect, it } from 'vitest'
import { decodeFrame, encodeFrame, FrameError, MAX_PAYLOAD_BYTES, type InputAudioHeader, type OutputAudioHeader } from '../src/lib/envelope'

const input = (n: number, extra: Partial<InputAudioHeader> = {}): InputAudioHeader => ({
  v: 1,
  kind: 'input_audio',
  session_id: 's1',
  turn_id: 't1',
  epoch: 0,
  seq: 1,
  sample_rate: 16000,
  sample_count: n,
  ...extra,
})

function rawFrame(header: object, payloadBytes: number): ArrayBuffer {
  const h = new TextEncoder().encode(JSON.stringify(header))
  const buf = new ArrayBuffer(4 + h.length + payloadBytes)
  new DataView(buf).setUint32(0, h.length, true)
  new Uint8Array(buf, 4).set(h)
  return buf
}

describe('binary envelope', () => {
  it('round-trips a 20 ms input frame with exact little-endian layout', () => {
    const pcm = Int16Array.from({ length: 320 }, (_, i) => (i * 97 - 16000) | 0)
    pcm[0] = -32768
    pcm[1] = 32767
    const buf = encodeFrame(input(320), pcm)
    const view = new DataView(buf)
    const headerLen = view.getUint32(0, true)
    expect(JSON.parse(new TextDecoder().decode(new Uint8Array(buf, 4, headerLen)))).toEqual(input(320))
    expect(buf.byteLength).toBe(4 + headerLen + 640)
    expect(view.getInt16(4 + headerLen, true)).toBe(-32768)
    const { header, pcm: out } = decodeFrame(buf)
    expect(header).toEqual(input(320))
    expect(Array.from(out)).toEqual(Array.from(pcm))
  })

  it('decodes output frames whose payload starts at an odd offset', () => {
    const h: OutputAudioHeader = { v: 1, kind: 'output_audio', session_id: 's', response_id: 'r1', epoch: 3, seq: 9, sample_rate: 24000, sample_count: 3, segment_id: 2 }
    const buf = encodeFrame(h, Int16Array.of(1, -2, 300))
    expect((4 + new DataView(buf).getUint32(0, true)) % 2).toBe(1) // odd offset really exercised
    const d = decodeFrame(buf)
    expect(d.header).toEqual(h)
    expect(Array.from(d.pcm)).toEqual([1, -2, 300])
  })

  it('rejects payload length that does not match sample_count', () => {
    expect(() => decodeFrame(rawFrame(input(320), 638))).toThrow(FrameError)
    expect(() => encodeFrame(input(10), new Int16Array(9))).toThrow(FrameError)
  })

  it('rejects oversized header, oversized payload, unknown kind, garbage', () => {
    expect(() => decodeFrame(rawFrame({ ...input(0), pad: 'x'.repeat(1100) }, 0))).toThrow(/header length/)
    const big = MAX_PAYLOAD_BYTES / 2 + 1
    expect(() => decodeFrame(rawFrame(input(big), big * 2))).toThrow(/payload too large/)
    expect(() => decodeFrame(rawFrame({ ...input(1), kind: 'video' }, 2))).toThrow(/unknown kind/)
    expect(() => decodeFrame(new ArrayBuffer(2))).toThrow(FrameError)
    const bad = new ArrayBuffer(8)
    new DataView(bad).setUint32(0, 4, true)
    new Uint8Array(bad, 4).set([0x7b, 0x7b, 0x7b, 0x7b])
    expect(() => decodeFrame(bad)).toThrow(/JSON/)
  })
})
