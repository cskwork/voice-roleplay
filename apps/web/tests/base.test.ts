import { describe, expect, it } from 'vitest'
import { PREFIX, withPrefix } from '../src/lib/base'

describe('withPrefix', () => {
  it('is a no-op for the default base', () => {
    expect(PREFIX).toBe('')
    expect(withPrefix('/api/health')).toBe('/api/health')
  })

  it('puts API paths under a proxy prefix', () => {
    expect(withPrefix('/api/sessions/s_1/end', '/voice')).toBe('/voice/api/sessions/s_1/end')
    expect(withPrefix('relative', '/voice')).toBe('relative')
  })
})
