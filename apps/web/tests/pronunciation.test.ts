import { describe, expect, it } from 'vitest'
import { normalizeHealth } from '../src/lib/api'
import { BAND_LABEL, contourPath, guideEntries, guideReviewed, ipa, relativeContour, seconds, slicePcm } from '../src/lib/pronunciation'
import { forgetTake, learnerTake, rememberTake, TAKE_TTL_MS } from '../src/lib/takes'
import type { PronunciationGuide } from '../src/lib/types'
import { encodeWav } from '../src/lib/wav'

describe('slicePcm', () => {
  const pcm = Int16Array.from({ length: 16000 }, (_, i) => i % 1000)
  it('cuts [start - pad, end + pad) at the sample rate', () => {
    const s = slicePcm(pcm, 16000, 200, 400, 50)
    expect(s.length).toBe(Math.round(0.3 * 16000))
    expect(s[0]).toBe(pcm[Math.round(0.15 * 16000)])
  })
  it('clamps to the recording and never goes negative', () => {
    expect(slicePcm(pcm, 16000, 0, 5000).length).toBe(16000)
    expect(slicePcm(pcm, 16000, 2000, 3000, 0).length).toBe(0)
  })
})

describe('relativeContour', () => {
  it('is semitones against the median voiced pitch, with gaps for unvoiced frames', () => {
    const f0 = [0, 100, 200, 0, 100]
    const pts = relativeContour(f0, 10, 0, 40)
    expect(pts.map((p) => p.st)).toEqual([null, 0, 12, null, 0])
    expect(pts[0]!.x).toBe(0)
    expect(pts.at(-1)!.x).toBe(1)
  })
  it('makes a low and a high voice with the same shape identical', () => {
    const low = relativeContour([100, 110, 120, 130], 10, 0, 30)
    const high = relativeContour([200, 220, 240, 260], 10, 0, 30)
    expect(high.map((p) => p.st!.toFixed(6))).toEqual(low.map((p) => p.st!.toFixed(6)))
  })
  it('clamps to ±12 and returns nothing without voiced frames', () => {
    expect(relativeContour([100, 100, 100, 1000], 10, 0, 30).at(-1)!.st).toBe(12)
    expect(relativeContour([0, 0, 0], 10, 0, 20)).toEqual([])
  })
  it('path breaks at unvoiced frames', () => {
    const d = contourPath([{ x: 0, st: 0 }, { x: 0.5, st: null }, { x: 1, st: 12 }], 100, 24)
    expect(d).toBe('M0.0 12.0M100.0 0.0')
  })
})

describe('labels', () => {
  it('bands carry icon and text, never numbers', () => {
    for (const b of Object.values(BAND_LABEL)) {
      expect(b.text).not.toMatch(/\d/)
      expect(b.icon).toBeTruthy()
    }
    expect(new Set(Object.values(BAND_LABEL).map((b) => b.icon)).size).toBe(3)
  })
  it('formats durations and IPA', () => {
    expect(seconds(420)).toBe('0.42초')
    expect(seconds(1500)).toBe('1.5초')
    expect(ipa('θ')).toBe('/θ/')
    expect(ipa('')).toBe('(소리 없음)')
  })
  it('looks up guide entries in order and reports review state', () => {
    const guide = {
      version: '1',
      accent: 'en-US',
      review_status: { english: 'pending' },
      entries: [{ entry_id: 'r_l' }, { entry_id: 'ae_e' }],
    } as unknown as PronunciationGuide
    expect(guideEntries(guide, ['ae_e', 'missing', 'r_l']).map((e) => e.entry_id)).toEqual(['ae_e', 'r_l'])
    expect(guideReviewed(guide)).toBe(false)
    expect(guideEntries(null, ['r_l'])).toEqual([])
  })
})

describe('browser copy of the take', () => {
  it('is kept in memory for 5 minutes after recording, then dropped', () => {
    const wav = encodeWav(Int16Array.from([1, 2, 3, 4]), 16000)
    const t0 = 1_000_000
    rememberTake('a1', wav, t0, t0 + 1000)
    const kept = learnerTake('a1', t0 + 1000)
    expect(kept?.rate).toBe(16000)
    expect(Array.from(kept!.samples)).toEqual([1, 2, 3, 4])
    expect(learnerTake('a1', t0 + TAKE_TTL_MS + 1)).toBeNull()
    expect(learnerTake('a1', t0 + 1000)).toBeNull() // gone for good
    rememberTake('a2', wav, t0, t0 + TAKE_TTL_MS + 1) // already expired: not kept
    expect(learnerTake('a2', t0)).toBeNull()
    forgetTake('a1')
  })
})

describe('normalizeHealth pronunciation', () => {
  it('reads pronunciation_assessment and the optional pron worker without affecting readiness', () => {
    const ready = { ready: true }
    const h = normalizeHealth({
      workers: { asr: ready, tts: ready, llm: ready, vad: ready, pron: { ready: false, reachable: true } },
      modes: {},
      pronunciation_assessment: 'timing_only',
    })
    expect(h.pronunciation).toBe('timing_only')
    expect(h.components.pron?.ready).toBe(false)
    expect(h.ready).toBe(true)
    expect(normalizeHealth({}).pronunciation).toBe('assessment_unavailable')
    expect(normalizeHealth({ pronunciation_assessment: 'assessed_banded' }).pronunciation).toBe('assessment_unavailable')
  })
})
