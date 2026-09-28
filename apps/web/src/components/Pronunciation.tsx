import { useEffect, useId, useMemo, useRef, useState } from 'react'
import { playWav } from '../hooks/audio'
import { api } from '../lib/api'
import { toApiError } from '../lib/errors'
import {
  BAND_LABEL,
  contourPath,
  EXPERIMENTAL_LABEL,
  guideEntries,
  guideReviewed,
  ipa,
  PAUSE_SHOW_MS,
  relativeContour,
  seconds,
  slicePcm,
} from '../lib/pronunciation'
import { learnerTake } from '../lib/takes'
import type { GuideEntry, Pronunciation, PronunciationGuide, PronWord } from '../lib/types'
import { decodeWav, encodeWav } from '../lib/wav'
import { Icon } from './Icon'
import { Badge, Notice } from './ui'

// Series colours (validated with the dataviz palette checker: CVD ΔE 17.5, contrast ≥ 3:1 on white);
// the model line is also dashed and both lines are named in a legend, so colour is never the only cue.
const LEARNER_COLOR = '#6a3fb5'
const MODEL_COLOR = '#008574'

let guideCache: Promise<PronunciationGuide | null> | null = null
function loadGuide(): Promise<PronunciationGuide | null> {
  guideCache ??= api.pronunciationGuide().catch(() => {
    guideCache = null
    return null
  })
  return guideCache
}

function Guide({ entries, reviewed }: { entries: GuideEntry[]; reviewed: boolean }) {
  return (
    <details className="pron-guide">
      <summary>이 단어 발음 가이드</summary>
      <p className="muted small">
        이 단어에 들어 있는 소리의 설명이에요. 내 발음을 판정한 것이 아니에요.
        {!reviewed && ' 설명은 초안이며 원어민·음성학 검수 전이에요.'}
      </p>
      {entries.map((e) => (
        <section key={e.entry_id} className="pron-guide-entry" aria-label={e.title_ko}>
          <h4>
            {e.title_ko} <span lang="en">({e.title_en})</span>
          </h4>
          <p>{e.tip_ko}</p>
          <p className="muted small">{e.why_ko}</p>
          {e.minimal_pairs.length > 0 && (
            <p className="small">
              비슷한 단어 짝:{' '}
              <span lang="en">
                {e.minimal_pairs
                  .slice(0, 4)
                  .map((p) => `${p.a.word} / ${p.b.word}`)
                  .join(', ')}
              </span>
            </p>
          )}
        </section>
      ))}
    </details>
  )
}

function WordButton({ w, selected, banded, onSelect }: { w: PronWord; selected: boolean; banded: boolean; onSelect(): void }) {
  const band = banded && w.band ? BAND_LABEL[w.band] : null
  const uncertain = w.in_transcript === false
  const label = [w.word, band ? `${band.text} (${EXPERIMENTAL_LABEL})` : null, uncertain ? '음성 인식에는 없던 단어라 위치가 불확실해요' : null].filter(Boolean).join(', ')
  return (
    <button
      type="button"
      className={`pron-word${selected ? ' is-on' : ''}${band ? ` band-${w.band}` : ''}${uncertain ? ' is-uncertain' : ''}`}
      aria-pressed={selected}
      aria-label={label}
      onClick={onSelect}
    >
      {band && <Icon name={band.icon} size={14} />}
      <span lang="en">{w.word}</span>
      {uncertain && <span aria-hidden="true">?</span>}
    </button>
  )
}

function ContourChart({ p, words }: { p: Pronunciation; words: PronWord[] }) {
  const learner = p.prosody.learner
  const model = p.prosody.model
  const W = 640
  const H = 160
  const first = words[0]
  const last = words.at(-1)
  const learnerPts = useMemo(
    () => (learner && first && last ? relativeContour(learner.f0_hz, learner.hop_ms, first.start_ms, last.end_ms) : []),
    [learner, first, last],
  )
  const modelPts = useMemo(() => {
    const mw = model?.words
    return model && mw?.length ? relativeContour(model.f0_hz, model.hop_ms, mw[0]!.start_ms, mw.at(-1)!.end_ms) : []
  }, [model])
  if (!first || !last || learnerPts.length === 0) return null
  const span = last.end_ms - first.start_ms || 1
  const xOf = (ms: number) => ((ms - first.start_ms) / span) * W
  // Word labels under the plot, skipped where they would overlap the previous one (all words are in the table).
  let labelEnd = -Infinity
  const showLabel = words.map((w) => {
    const x = xOf(w.start_ms) + 2
    if (x < labelEnd + 6) return false
    labelEnd = x + w.word.length * 7
    return true
  })
  return (
    <figure className="pron-chart">
      <figcaption>
        <strong>억양 곡선</strong> <Badge>참고 지표 · 점수 아님</Badge>
      </figcaption>
      <ul className="pron-legend" aria-label="범례">
        <li>
          <svg width="22" height="8" aria-hidden="true">
            <line x1="1" y1="4" x2="21" y2="4" stroke={LEARNER_COLOR} strokeWidth="2" strokeLinecap="round" />
          </svg>
          내 녹음
        </li>
        {modelPts.length > 0 && (
          <li>
            <svg width="22" height="8" aria-hidden="true">
              <line x1="1" y1="4" x2="21" y2="4" stroke={MODEL_COLOR} strokeWidth="2" strokeDasharray="5 3" strokeLinecap="round" />
            </svg>
            모범 음성
          </li>
        )}
      </ul>
      <svg
        className="pron-chart-svg"
        viewBox={`0 0 ${W} ${H + 22}`}
        role="img"
        aria-label={`음높이가 오르내린 모양${modelPts.length ? '을 내 녹음과 모범 음성으로 나란히' : ''} 보여 주는 그래프. 단어별 길이와 쉼은 아래 표에 있어요.`}
      >
        <line x1="0" x2={W} y1={H / 2} y2={H / 2} className="pron-chart-mid" />
        {words.map((w, k) => (
          <g key={w.i}>
            <rect x={xOf(w.start_ms)} y="0" width={Math.max(1, xOf(w.end_ms) - xOf(w.start_ms))} height={H} className="pron-chart-word">
              <title>{`${w.word} · ${seconds(w.duration_ms)}`}</title>
            </rect>
            {showLabel[k] && (
              <text x={xOf(w.start_ms) + 2} y={H + 16} className="pron-chart-label" lang="en">
                {w.word}
              </text>
            )}
          </g>
        ))}
        {modelPts.length > 0 && <path d={contourPath(modelPts, W, H)} fill="none" stroke={MODEL_COLOR} strokeWidth="2" strokeDasharray="5 3" strokeLinecap="round" strokeLinejoin="round" />}
        <path d={contourPath(learnerPts, W, H)} fill="none" stroke={LEARNER_COLOR} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <p className="muted small">
        목소리 높낮이의 모양만 비교해요. 각자 평소 음높이를 가운데 선에 맞췄고, 끊긴 곳은 목소리가 울리지 않은 구간이에요. 모범 음성과 달라도 틀린 것이 아니에요.
      </p>
    </figure>
  )
}

/**
 * "발음 살펴보기" (PRD §8.4, PROTOCOL §12.4). Default (`timing_only`): word timings, word-by-word compare playback
 * and pitch contours, with no judgement. `experimental_banded`: word bands (icon + text + colour, no numbers) and
 * "heard" sound candidates, always labelled experimental and phrased as a question.
 */
export function PronunciationCard({ attemptId, p, modelUrl, canSaveReview }: { attemptId: string; p: Pronunciation; modelUrl?: string; canSaveReview: boolean }) {
  const [sel, setSel] = useState<number | null>(null)
  const [guide, setGuide] = useState<PronunciationGuide | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saved, setSaved] = useState<Set<number>>(() => new Set())
  const modelWav = useRef<{ samples: Int16Array; rate: number } | null>(null)
  const panelId = useId()
  const banded = p.status === 'experimental_banded'
  const words = p.words

  const [, rerender] = useState(0)
  const take = learnerTake(attemptId)

  useEffect(() => {
    if (words.some((w) => w.guide_ids.length)) void loadGuide().then(setGuide)
  }, [words])
  // The browser copy of the take expires; re-render then so the button says why it is off.
  useEffect(() => {
    if (!take) return
    const t = setTimeout(() => rerender((n) => n + 1), Math.max(0, take.expiresAt - Date.now()) + 50)
    return () => clearTimeout(t)
  }, [take?.expiresAt])

  if (p.status === 'unavailable') return null
  const current = sel == null ? null : (words.find((w) => w.i === sel) ?? null)
  const modelWord = current ? p.prosody.model?.words.find((w) => w.i === current.i) : undefined

  const playMine = async (w: PronWord) => {
    setError(null)
    const t = learnerTake(attemptId)
    if (!t) return
    await playWav(encodeWav(slicePcm(t.samples, t.rate, w.start_ms, w.end_ms), t.rate)).catch(() => setError('재생하지 못했어요.'))
  }
  const playModel = async (mw: { start_ms: number; end_ms: number }) => {
    setError(null)
    try {
      if (!modelWav.current && modelUrl) {
        const d = decodeWav(await api.audioUrl(modelUrl))
        modelWav.current = { samples: d.samples, rate: d.sampleRate }
      }
      const m = modelWav.current
      if (m) await playWav(encodeWav(slicePcm(m.samples, m.rate, mw.start_ms, mw.end_ms), m.rate))
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }
  const saveReview = async (w: PronWord) => {
    setError(null)
    try {
      await api.addReview({ text_en: w.word, text_ko: '발음 연습 단어 (실험 기능 표시)', source_type: 'attempt', source_id: attemptId })
      setSaved((s) => new Set(s).add(w.i))
    } catch (e) {
      setError(toApiError(e).messageKo)
    }
  }

  const band = current && banded && current.band ? BAND_LABEL[current.band] : null
  const weak = current?.weak_sounds ?? []
  const entries = current ? guideEntries(guide, current.guide_ids) : []

  return (
    <section className="card pron" aria-labelledby="pron-h">
      <div className="card-head">
        <h2 id="pron-h">단어별로 들어 보기</h2>
        {banded && <Badge tone="warn">{EXPERIMENTAL_LABEL}</Badge>}
      </div>
      <p className="pron-note">
        <Icon name="info" size={16} /> {p.note_ko}
      </p>
      {p.mode === 'unscripted' && <p className="muted small">자유 발화는 음성 인식이 받아쓴 문장을 기준으로 단어 위치를 찾아요.</p>}

      <div className="pron-words" role="group" aria-label="단어를 누르면 내 발음과 모범 음성을 비교해 들을 수 있어요">
        {words.map((w) => (
          <span key={w.i} className="pron-slot">
            {w.gap_before_ms >= PAUSE_SHOW_MS && (
              <span className="pron-pause">쉼 {seconds(w.gap_before_ms)}</span>
            )}
            <WordButton w={w} selected={sel === w.i} banded={banded} onSelect={() => setSel(sel === w.i ? null : w.i)} />
          </span>
        ))}
      </div>
      {banded && (
        <p className="muted small pron-band-legend">
          {(['good', 'check', 'practice'] as const).map((b) => (
            <span key={b} className={`pron-band-key band-${b}`}>
              <Icon name={BAND_LABEL[b].icon} size={14} /> {BAND_LABEL[b].text}
            </span>
          ))}
        </p>
      )}

      <div id={panelId} className="pron-panel" aria-live="polite">
        {current ? (
          <>
            <p className="pron-panel-head">
              <strong lang="en">{current.word}</strong>{' '}
              <span className="muted small">
                길이 {seconds(current.duration_ms)} · 앞 쉼 {seconds(current.gap_before_ms)}
                {modelWord ? ` · 모범 음성 길이 ${seconds(modelWord.end_ms - modelWord.start_ms)}` : ''}
              </span>
            </p>
            {current.in_transcript === false && <Notice tone="warn">음성 인식에는 이 단어가 없었어요. 이 구간 위치는 불확실해요.</Notice>}
            <div className="row" role="group" aria-label="비교해 듣기">
              <button type="button" className="btn btn-soft" onClick={() => void playMine(current)} disabled={!take}>
                <Icon name="play" /> 내 발음
              </button>
              <button type="button" className="btn btn-soft" onClick={() => modelWord && void playModel(modelWord)} disabled={!modelWord || !modelUrl}>
                <Icon name="speaker" /> 모범 음성
              </button>
            </div>
            {!take && <p className="disabled-reason">내 녹음은 녹음 후 5분까지만 이 브라우저 메모리에 남아요. 지금은 지워졌어요.</p>}
            {!modelWord && <p className="disabled-reason">{p.mode === 'unscripted' ? '자유 발화에는 같은 문장의 모범 음성이 없어요.' : '모범 음성의 단어 위치가 없어요.'}</p>}

            {band && (
              <div className="pron-judgement">
                <p>
                  <Badge tone={band.tone}>
                    <Icon name={band.icon} size={14} /> {band.text}
                  </Badge>{' '}
                  <span className="muted small">{EXPERIMENTAL_LABEL}. 틀릴 수 있어요.</span>
                </p>
                {weak.map((s, k) => (
                  <div key={k} className="pron-heard">
                    <Badge tone="warn">확인 필요</Badge>{' '}
                    <span>
                      {ipa(s.expected_ipa)} 자리가 {ipa(s.heard_ipa)}처럼 들렸을 수도 있어요. 맞나요? 두 소리를 직접 비교해 들어 보세요.
                    </span>
                    {guideEntries(guide, s.guide_ids).map((e) => (
                      <span key={e.entry_id} className="muted small">
                        {' '}
                        참고: {e.title_ko}
                      </span>
                    ))}
                  </div>
                ))}
                {p.mode === 'unscripted' && <p className="muted small">자유 발화는 인식된 문장이 틀렸을 수도 있어서, 이 표시는 전사를 확인하기 전까지 확인이 필요한 상태예요.</p>}
                {(current.band === 'check' || current.band === 'practice') &&
                  (saved.has(current.i) ? (
                    <p className="small">
                      <Icon name="check" size={16} /> 복습에 넣었어요.
                    </p>
                  ) : (
                    <>
                      <button type="button" className="btn btn-ghost" onClick={() => void saveReview(current)} disabled={!canSaveReview}>
                        복습에 추가
                      </button>
                      {!canSaveReview && <p className="disabled-reason">설정에서 기록 저장을 켜야 복습에 넣을 수 있어요.</p>}
                    </>
                  ))}
              </div>
            )}
            {entries.length > 0 && <Guide entries={entries} reviewed={guideReviewed(guide)} />}
          </>
        ) : (
          <p className="muted small">단어를 눌러 보세요.</p>
        )}
        {error && <Notice tone="danger" icon="alert">{error}</Notice>}
      </div>

      <ContourChart p={p} words={words} />

      <details className="fine-print">
        <summary>단어별 길이와 쉼 표로 보기</summary>
        <table className="pron-table">
          <thead>
            <tr>
              <th scope="col">단어</th>
              <th scope="col">시작</th>
              <th scope="col">길이</th>
              <th scope="col">앞 쉼</th>
              {p.prosody.model && <th scope="col">모범 음성 길이</th>}
            </tr>
          </thead>
          <tbody>
            {words.map((w) => {
              const mw = p.prosody.model?.words.find((m) => m.i === w.i)
              return (
                <tr key={w.i}>
                  <th scope="row" lang="en">
                    {w.word}
                  </th>
                  <td>{seconds(w.start_ms)}</td>
                  <td>{seconds(w.duration_ms)}</td>
                  <td>{seconds(w.gap_before_ms)}</td>
                  {p.prosody.model && <td>{mw ? seconds(mw.end_ms - mw.start_ms) : '—'}</td>}
                </tr>
              )
            })}
          </tbody>
        </table>
        <p className="muted small">
          단어 위치는 정렬 모델(Qwen3-ForcedAligner)이 찾은 것이에요. 위치를 찾았다고 발음이 맞았다는 뜻은 아니에요. 시간 단위는 약 0.08초예요.
        </p>
      </details>
    </section>
  )
}

/** Shown instead of the card when analysis did not run; the rest of the result is unaffected. */
export function PronunciationUnavailable({ p, reason }: { p: Pronunciation; reason: string }) {
  return (
    <p className="pron-note">
      <Icon name="info" size={16} /> {p.note_ko} {reason}
    </p>
  )
}
