import { describe, expect, it } from 'vitest'
import { decodeWav, encodeWav } from '../src/lib/wav'

describe('WAV encoder', () => {
  it('writes a valid 44-byte PCM16 mono header and little-endian samples', () => {
    const pcm = Int16Array.of(0, 1, -1, 32767, -32768)
    const buf = encodeWav(pcm, 16000)
    const v = new DataView(buf)
    const str = (o: number) => String.fromCharCode(...new Uint8Array(buf, o, 4))
    expect(buf.byteLength).toBe(44 + 10)
    expect(str(0)).toBe('RIFF')
    expect(v.getUint32(4, true)).toBe(36 + 10)
    expect(str(8)).toBe('WAVE')
    expect(str(12)).toBe('fmt ')
    expect(v.getUint32(16, true)).toBe(16)
    expect(v.getUint16(20, true)).toBe(1) // PCM
    expect(v.getUint16(22, true)).toBe(1) // mono
    expect(v.getUint32(24, true)).toBe(16000)
    expect(v.getUint32(28, true)).toBe(32000)
    expect(v.getUint16(32, true)).toBe(2)
    expect(v.getUint16(34, true)).toBe(16)
    expect(str(36)).toBe('data')
    expect(v.getUint32(40, true)).toBe(10)
    expect(v.getInt16(44 + 6, true)).toBe(32767)
    expect(v.getInt16(44 + 8, true)).toBe(-32768)
  })

  it('round-trips through the decoder', () => {
    const pcm = Int16Array.from({ length: 16000 }, (_, i) => Math.round(8000 * Math.sin(i / 10)))
    const d = decodeWav(encodeWav(pcm, 16000))
    expect(d.sampleRate).toBe(16000)
    expect(Array.from(d.samples)).toEqual(Array.from(pcm))
  })

  it('decoder mixes stereo to mono and rejects non-WAV', () => {
    const buf = new ArrayBuffer(44 + 8)
    const v = new DataView(buf)
    const w = (o: number, s: string) => [...s].forEach((c, i) => v.setUint8(o + i, c.charCodeAt(0)))
    w(0, 'RIFF')
    v.setUint32(4, 44, true)
    w(8, 'WAVE')
    w(12, 'fmt ')
    v.setUint32(16, 16, true)
    v.setUint16(20, 1, true)
    v.setUint16(22, 2, true)
    v.setUint32(24, 48000, true)
    v.setUint32(28, 192000, true)
    v.setUint16(32, 4, true)
    v.setUint16(34, 16, true)
    w(36, 'data')
    v.setUint32(40, 8, true)
    v.setInt16(44, 100, true)
    v.setInt16(46, 300, true)
    v.setInt16(48, -200, true)
    v.setInt16(50, -400, true)
    expect(Array.from(decodeWav(buf).samples)).toEqual([200, -300])
    expect(() => decodeWav(new ArrayBuffer(20))).toThrow()
  })
})
