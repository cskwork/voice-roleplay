/** 16-bit PCM mono WAV encoder (RIFF little-endian). */
export function encodeWav(samples: Int16Array, sampleRate: number): ArrayBuffer {
  const dataBytes = samples.length * 2
  const buf = new ArrayBuffer(44 + dataBytes)
  const v = new DataView(buf)
  const ascii = (offset: number, s: string) => {
    for (let i = 0; i < s.length; i++) v.setUint8(offset + i, s.charCodeAt(i))
  }
  ascii(0, 'RIFF')
  v.setUint32(4, 36 + dataBytes, true)
  ascii(8, 'WAVE')
  ascii(12, 'fmt ')
  v.setUint32(16, 16, true) // fmt chunk size
  v.setUint16(20, 1, true) // PCM
  v.setUint16(22, 1, true) // channels
  v.setUint32(24, sampleRate, true)
  v.setUint32(28, sampleRate * 2, true) // byte rate
  v.setUint16(32, 2, true) // block align
  v.setUint16(34, 16, true) // bits per sample
  ascii(36, 'data')
  v.setUint32(40, dataBytes, true)
  for (let i = 0; i < samples.length; i++) v.setInt16(44 + i * 2, samples[i]!, true)
  return buf
}

/** Minimal PCM16 WAV reader (for decoding gateway TTS responses and tests). Mono-mixes stereo. */
export function decodeWav(buf: ArrayBuffer): { sampleRate: number; samples: Int16Array } {
  const v = new DataView(buf)
  const tag = (o: number) => String.fromCharCode(v.getUint8(o), v.getUint8(o + 1), v.getUint8(o + 2), v.getUint8(o + 3))
  if (buf.byteLength < 12 || tag(0) !== 'RIFF' || tag(8) !== 'WAVE') throw new Error('not a WAV file')
  let offset = 12
  let sampleRate = 0
  let channels = 0
  while (offset + 8 <= buf.byteLength) {
    const id = tag(offset)
    const size = v.getUint32(offset + 4, true)
    const body = offset + 8
    if (id === 'fmt ') {
      if (v.getUint16(body, true) !== 1 || v.getUint16(body + 14, true) !== 16) throw new Error('not PCM16')
      channels = v.getUint16(body + 2, true)
      sampleRate = v.getUint32(body + 4, true)
    } else if (id === 'data') {
      if (!sampleRate || !channels) throw new Error('data before fmt')
      const end = Math.min(buf.byteLength, body + size)
      const frames = Math.floor((end - body) / (2 * channels))
      const samples = new Int16Array(frames)
      for (let i = 0; i < frames; i++) {
        let acc = 0
        for (let c = 0; c < channels; c++) acc += v.getInt16(body + (i * channels + c) * 2, true)
        samples[i] = Math.round(acc / channels)
      }
      return { sampleRate, samples }
    }
    offset = body + size + (size & 1)
  }
  throw new Error('no data chunk')
}
