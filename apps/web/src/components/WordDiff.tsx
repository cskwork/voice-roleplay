import { wordDiff, type DiffOp } from '../lib/diff'
import type { ServerDiffOp } from '../lib/types'
import { Icon } from './Icon'

function fromServer(ops: ServerDiffOp[]): DiffOp[] {
  return ops.map((o) =>
    o.op === 'equal'
      ? { op: 'same', target: o.target ?? '', heard: o.heard ?? '' }
      : o.op === 'different'
        ? { op: 'changed', target: o.target ?? '', heard: o.heard ?? '' }
        : o.op === 'missing'
          ? { op: 'missing', target: o.target ?? '' }
          : { op: 'extra', heard: o.heard ?? '' },
  )
}

function diffOps(target: string, heard: string, serverOps?: ServerDiffOp[] | null): DiffOp[] {
  return serverOps ? fromServer(serverOps) : wordDiff(target, heard)
}

/** How many places the recognised sentence differs from the target (0 when they match). */
export function diffCount(target: string, heard: string, serverOps?: ServerDiffOp[] | null): number {
  return diffOps(target, heard, serverOps).filter((o) => o.op !== 'same').length
}

/** One full sentence with the differing words marked, so each row stays readable however many words differ. */
function Row({ label, words, tone }: { label: string; words: { text: string; marked: boolean }[]; tone: 'target' | 'heard' }) {
  return (
    <div className={`diff-row diff-row-${tone}`}>
      <dt>{label}</dt>
      <dd lang="en">
        {words.length === 0
          ? '—'
          : words.map((w, i) => (
              <span key={i}>
                {w.marked ? <mark className={`diff-mark diff-mark-${tone}`}>{w.text}</mark> : w.text}
                {i < words.length - 1 ? ' ' : ''}
              </span>
            ))}
      </dd>
    </div>
  )
}

/**
 * "다르게 인식된 부분": recognition differences only, never a pronunciation judgement.
 * Two aligned rows (target / recognised), differing words tinted amber and indigo:
 * no red/green, no strike-through, so a difference never reads as a mistake.
 * Uses the gateway's diff when the result has one, else compares locally.
 */
export function WordDiff({ target, heard, serverOps, level = 'h3' }: { target: string; heard: string; serverOps?: ServerDiffOp[] | null; level?: 'h2' | 'h3' }) {
  const ops = diffOps(target, heard, serverOps)
  const diffs = ops.filter((o) => o.op !== 'same').length
  const H = level
  if (diffs === 0) {
    return (
      <div className="word-diff">
        <H className="card-title">다르게 인식된 부분</H>
        <p className="diff-same">
          <Icon name="check" size={18} /> 목표 문장과 같게 인식됐어요.
        </p>
      </div>
    )
  }
  const targetWords = ops.flatMap((o) => (o.op === 'extra' ? [] : [{ text: o.target, marked: o.op !== 'same' }]))
  const heardWords = ops.flatMap((o) => (o.op === 'missing' ? [] : [{ text: o.op === 'same' ? o.heard || o.target : o.heard, marked: o.op !== 'same' }]))
  return (
    <div className="word-diff">
      <H className="card-title">다르게 인식된 부분 — {diffs}곳</H>
      <dl className="diff-rows">
        <Row label="목표 문장" words={targetWords} tone="target" />
        <Row label="인식된 문장" words={heardWords} tone="heard" />
      </dl>
      <p className="muted small diff-legend">표시된 단어는 음성 인식 결과가 목표 문장과 달랐던 곳이에요. 발음이 틀렸다는 판정이 아니에요.</p>
    </div>
  )
}
