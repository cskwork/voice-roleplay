// Pure helpers for the pronunciation card (PROTOCOL §12.4, PRD §8.4). Nothing here computes a judgement:
// it slices audio by the worker's word timings and rescales pitch contours for display.
import type { Band, GuideEntry, PronunciationGuide } from './types'

/** Word slices get a little context on both sides: aligner times come in 80 ms steps. */
export const SLICE_PAD_MS = 60

/** Samples of [startMs - pad, endMs + pad), clamped to the recording. Returns a copy. */
export function slicePcm(samples: Int16Array, rate: number, startMs: number, endMs: number, padMs = SLICE_PAD_MS): Int16Array {
  const from = Math.max(0, Math.floor(((startMs - padMs) / 1000) * rate))
  const to = Math.min(samples.length, Math.ceil(((endMs + padMs) / 1000) * rate))
  return samples.slice(from, Math.max(from, to))
}

export interface ContourPoint {
  /** 0..1 across the spoken part (first word start to last word end). */
  x: number
  /** Semitones relative to the speaker's own median pitch in that span; null = unvoiced (gap in the line). */
  st: number | null
}

/**
 * Pitch contour between fromMs and toMs, as semitones relative to the median voiced pitch of that span.
 * Using each voice's own median makes a low and a high voice comparable in shape; values are clamped to ±12.
 */
export function relativeContour(f0: number[], hopMs: number, fromMs: number, toMs: number): ContourPoint[] {
  const a = Math.max(0, Math.floor(fromMs / hopMs))
  const b = Math.min(f0.length, Math.ceil(toMs / hopMs) + 1)
  if (b <= a || toMs <= fromMs) return []
  const voiced = f0.slice(a, b).filter((v) => v > 0).sort((x, y) => x - y)
  if (voiced.length === 0) return []
  const median = voiced[Math.floor(voiced.length / 2)]!
  const out: ContourPoint[] = []
  for (let k = a; k < b; k++) {
    const v = f0[k]!
    const x = (k * hopMs - fromMs) / (toMs - fromMs)
    out.push({ x: Math.min(1, Math.max(0, x)), st: v > 0 ? Math.max(-12, Math.min(12, 12 * Math.log2(v / median))) : null })
  }
  return out
}

/** SVG path for a contour; unvoiced frames break the line. y maps +12 st to the top, -12 st to the bottom. */
export function contourPath(points: ContourPoint[], width: number, height: number): string {
  let d = ''
  let pen = false
  for (const p of points) {
    if (p.st == null) {
      pen = false
      continue
    }
    const x = (p.x * width).toFixed(1)
    const y = (((12 - p.st) / 24) * height).toFixed(1)
    d += `${pen ? 'L' : 'M'}${x} ${y}`
    pen = true
  }
  return d
}

/** Seconds, e.g. 420 → "0.42초", 1500 → "1.5초". Durations only; never a quality number. */
export function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(ms < 1000 ? 2 : 1)}초`
}

/** A pause worth showing between two words (the same 0.5 s threshold as the fluency metrics would be too coarse here). */
export const PAUSE_SHOW_MS = 300

/** Experimental bands: colour plus icon plus text, never colour alone (PRD §12, PROTOCOL §12.3). No numbers. */
export const BAND_LABEL: Record<Band, { text: string; icon: 'check' | 'info' | 'replay'; tone: 'ok' | 'warn' | 'danger' }> = {
  good: { text: '비슷하게 들림', icon: 'check', tone: 'ok' },
  check: { text: '한 번 확인', icon: 'info', tone: 'warn' },
  practice: { text: '연습 추천', icon: 'replay', tone: 'danger' },
}

export const EXPERIMENTAL_LABEL = '실험적 — 한국어 학습자 검증 전'

/** IPA for display; "" from the worker means no sound was heard at that position. */
export function ipa(sym: string | null | undefined): string {
  return sym ? `/${sym}/` : '(소리 없음)'
}

export function guideEntries(guide: PronunciationGuide | null, ids: string[]): GuideEntry[] {
  if (!guide) return []
  return ids.map((id) => guide.entries.find((e) => e.entry_id === id)).filter((e): e is GuideEntry => !!e)
}

export function guideReviewed(guide: PronunciationGuide | null): boolean {
  return !!guide && Object.values(guide.review_status ?? {}).every((s) => s === 'reviewed')
}

/** Short Korean reason for `status: "unavailable"` (the note itself comes from the gateway). */
export function unavailableReason(reason: string | null): string {
  switch (reason) {
    case 'NOT_CONFIGURED':
      return '발음 분석 기능이 설치되어 있지 않아요.'
    case 'MODEL_NOT_READY':
      return '발음 분석 모델이 아직 준비되지 않았어요.'
    case 'NO_SPEECH':
      return '말소리가 충분하지 않았어요.'
    case 'TEXT_EMPTY':
      return '기준이 될 문장이 없었어요.'
    case 'LOCAL_BUSY':
      return '실시간 회화 중에는 발음 분석을 하지 않아요.'
    case 'TIMEOUT':
      return '분석 시간이 초과됐어요.'
    default:
      return '분석 중 문제가 생겼어요.'
  }
}
