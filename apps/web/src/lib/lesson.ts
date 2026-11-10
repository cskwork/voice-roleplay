import type { AttemptResult, Goal, Hint, Scenario, TextItem } from './types'

export interface LessonStep { goal: Goal; expression: TextItem; hint?: Hint }
const filled = (v: unknown): v is string => typeof v === 'string' && !!v.trim()

/** The first authored expression of each goal is the curriculum, never an AI-generated substitute. */
export function lessonSteps(sc: Scenario): LessonStep[] | null {
  const goals = sc.goals?.slice(0, 3)
  if (!goals || goals.length !== 3 || new Set(goals.map((g) => g.goal_id)).size !== 3) return null
  const steps: LessonStep[] = []
  for (const goal of goals) {
    const expression = sc.model_expressions?.find((e) => e.goal_id === goal.goal_id)
    if (!filled(goal.goal_id) || !filled(goal.ko) || !expression || !filled(expression.text_id) || !filled(expression.en) || !filled(expression.ko)) return null
    steps.push({ goal, expression, hint: sc.hints?.find((h) => h.goal_id === goal.goal_id) })
  }
  if (new Set(steps.map((s) => s.expression.text_id)).size !== 3 || new Set(steps.map((s) => s.expression.en.trim().toLowerCase())).size !== 3) return null
  return steps
}

export function canAdvanceLesson(result: AttemptResult | null): boolean {
  return !!result && !result.no_speech && filled(result.transcript)
}

export type LessonComparison = { kind: 'same' | 'unavailable' | 'different'; target?: string; heard?: string }
export function lessonComparison(result: AttemptResult): LessonComparison {
  const ops = result.target_diff
  if (!canAdvanceLesson(result) || !Array.isArray(ops) || ops.length === 0) return { kind: 'unavailable' }
  if (ops.some((o) => !['equal', 'different', 'missing', 'extra'].includes(o.op))) return { kind: 'unavailable' }
  const difference = ops.find((o) => o.op !== 'equal')
  if (!difference) return ops.every((o) => filled(o.target) && filled(o.heard)) ? { kind: 'same' } : { kind: 'unavailable' }
  if (!filled(difference.target) && !filled(difference.heard)) return { kind: 'unavailable' }
  return { kind: 'different', target: difference.target?.trim() || undefined, heard: difference.heard?.trim() || undefined }
}
