import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync } from 'node:fs'
import { canAdvanceLesson, lessonComparison, lessonSteps } from '../src/lib/lesson'
import type { AttemptResult, Scenario } from '../src/lib/types'

const scenarios = readdirSync('../../content/scenarios').filter((p) => p.endsWith('.json')).map((p) => JSON.parse(readFileSync(`../../content/scenarios/${p}`, 'utf8')) as Scenario)
const sc = scenarios[0]!
const result = (patch: Partial<AttemptResult> = {}) => ({ attempt_id: 'a', transcript: 'Hello.', no_speech: false, ...patch }) as AttemptResult

describe('authored three-expression curriculum', () => {
  for (const scenario of scenarios) it(`uses the first authored expression for each goal: ${scenario.scenario_id}`, () => {
    const steps = lessonSteps(scenario)
    expect(steps).toHaveLength(3)
    expect(steps!.map((s) => s.goal.goal_id)).toEqual(scenario.goals.slice(0, 3).map((g) => g.goal_id))
    for (const step of steps!) expect(step.expression).toBe(scenario.model_expressions.find((e) => e.goal_id === step.goal.goal_id))
  })
  it('rejects missing, duplicate and invalid authored material without silently changing curriculum', () => {
    expect(lessonSteps({ ...sc, goals: sc.goals.slice(0, 2) })).toBeNull()
    expect(lessonSteps({ ...sc, goals: [sc.goals[0]!, sc.goals[0]!, sc.goals[2]!] })).toBeNull()
    const expressions = sc.model_expressions.map((e) => ({ ...e }))
    expressions.find((e) => e.goal_id === sc.goals[0]!.goal_id)!.en = ''
    expect(lessonSteps({ ...sc, model_expressions: expressions })).toBeNull()
    const duplicate = sc.goals.map((g) => ({ goal_id: g.goal_id, en: 'Same sentence.', ko: '같아요.', text_id: 'same' }))
    expect(lessonSteps({ ...sc, model_expressions: duplicate })).toBeNull()
  })
})

describe('recognition comparison is not a correctness judgement', () => {
  it('requires a nonempty transcript and blocks no_speech', () => {
    expect(canAdvanceLesson(null)).toBe(false)
    expect(canAdvanceLesson(result({ transcript: '  ' }))).toBe(false)
    expect(canAdvanceLesson(result({ no_speech: true }))).toBe(false)
    expect(canAdvanceLesson(result({ transcript: 'Something different.' }))).toBe(true)
  })
  it('only calls an available equal diff equal', () => {
    expect(lessonComparison(result({ target_diff: [{ op: 'equal', target: 'Hello', heard: 'Hello' }] }))).toEqual({ kind: 'same' })
    for (const target_diff of [null, undefined, []]) expect(lessonComparison(result({ target_diff }))).toEqual({ kind: 'unavailable' })
  })
  it('shows exactly one recognition difference, including missing or extra text', () => {
    expect(lessonComparison(result({ target_diff: [{ op: 'equal', target: 'I', heard: 'I' }, { op: 'missing', target: 'would', heard: null }, { op: 'different', target: 'like', heard: 'liked' }] }))).toEqual({ kind: 'different', target: 'would', heard: undefined })
    expect(lessonComparison(result({ target_diff: [{ op: 'extra', target: null, heard: 'um' }] }))).toEqual({ kind: 'different', target: undefined, heard: 'um' })
    expect(lessonComparison(result({ target_diff: [{ op: 'different', target: null, heard: null }] }))).toEqual({ kind: 'unavailable' })
  })
})
