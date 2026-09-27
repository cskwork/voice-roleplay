import { wordDiff, type DiffOp } from '../lib/diff'
import type { ServerDiffOp } from '../lib/types'

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

/**
 * "다르게 인식된 부분": recognition differences only, never a pronunciation judgement.
 * Uses the gateway's diff when the result has one, else compares locally.
 */
export function WordDiff({ target, heard, serverOps }: { target: string; heard: string; serverOps?: ServerDiffOp[] | null }) {
  const ops = serverOps ? fromServer(serverOps) : wordDiff(target, heard)
  const diffs = ops.filter((o) => o.op !== 'same').length
  return (
    <div className="word-diff">
      <h4>다르게 인식된 부분 {diffs === 0 ? '— 없음' : `— ${diffs}곳`}</h4>
      <p lang="en" className="diff-line">
        {ops.map((o, i) =>
          o.op === 'same' ? (
            <span key={i}>{o.target} </span>
          ) : o.op === 'changed' ? (
            <span key={i} className="diff-changed">
              <del>{o.target}</del>
              <ins>{o.heard}</ins>{' '}
            </span>
          ) : o.op === 'missing' ? (
            <span key={i} className="diff-missing">
              <del>{o.target}</del>{' '}
            </span>
          ) : (
            <span key={i} className="diff-extra">
              <ins>{o.heard}</ins>{' '}
            </span>
          ),
        )}
      </p>
      <p className="muted small">
        <del className="legend">목표 단어</del> <ins className="legend">인식된 단어</ins> · 음성 인식 결과가 달랐다는 뜻이며, 발음이 틀렸다는 판정이 아니에요.
      </p>
    </div>
  )
}
