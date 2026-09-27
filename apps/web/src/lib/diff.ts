export type DiffOp =
  | { op: 'same'; target: string; heard: string }
  | { op: 'changed'; target: string; heard: string }
  | { op: 'missing'; target: string }
  | { op: 'extra'; heard: string }

const norm = (w: string) => w.toLowerCase().replace(/[^a-z0-9']/g, '')

function words(s: string): string[] {
  return s.split(/\s+/).filter((w) => norm(w) !== '')
}

/**
 * Word-level comparison of a target sentence and a transcript (LCS on normalized words).
 * Adjacent missing+extra runs are paired as "changed". Used for "다르게 인식된 부분" —
 * a recognition difference, not a pronunciation verdict.
 */
export function wordDiff(target: string, heard: string): DiffOp[] {
  const a = words(target)
  const b = words(heard)
  const n = a.length
  const m = b.length
  const lcs: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--)
    for (let j = m - 1; j >= 0; j--)
      lcs[i]![j] = norm(a[i]!) === norm(b[j]!) ? lcs[i + 1]![j + 1]! + 1 : Math.max(lcs[i + 1]![j]!, lcs[i]![j + 1]!)

  const raw: DiffOp[] = []
  let i = 0
  let j = 0
  while (i < n || j < m) {
    if (i < n && j < m && norm(a[i]!) === norm(b[j]!)) raw.push({ op: 'same', target: a[i++]!, heard: b[j++]! })
    else if (j < m && (i >= n || lcs[i]![j + 1]! >= lcs[i + 1]![j]!)) raw.push({ op: 'extra', heard: b[j++]! })
    else raw.push({ op: 'missing', target: a[i++]! })
  }

  // Pair runs of missing/extra between matches into "changed" entries.
  const out: DiffOp[] = []
  let k = 0
  while (k < raw.length) {
    if (raw[k]!.op === 'same') {
      out.push(raw[k++]!)
      continue
    }
    const missing: string[] = []
    const extra: string[] = []
    while (k < raw.length && raw[k]!.op !== 'same') {
      const r = raw[k++]!
      if (r.op === 'missing') missing.push(r.target)
      else if (r.op === 'extra') extra.push(r.heard)
    }
    const pairs = Math.min(missing.length, extra.length)
    for (let p = 0; p < pairs; p++) out.push({ op: 'changed', target: missing[p]!, heard: extra[p]! })
    for (const t of missing.slice(pairs)) out.push({ op: 'missing', target: t })
    for (const h of extra.slice(pairs)) out.push({ op: 'extra', heard: h })
  }
  return out
}
