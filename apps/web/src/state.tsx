import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { api } from './lib/api'
import { ApiError, toApiError } from './lib/errors'
import type { AttemptResult, ExerciseType, Health, Scenario, SessionSummary, Settings } from './lib/types'

export const DEFAULT_SETTINGS: Settings = {
  difficulty: 'normal',
  silence_ms: null,
  auto_barge_in: true,
  voice_id: null,
  history_opt_in: false,
}

const MIC_KEY = 'vr.micDeviceId'

export interface AttemptMeta {
  scenarioId: string
  exerciseType: ExerciseType
  opts: string
  itemId?: string
  target?: { en: string; ko?: string; textId?: string }
  sample?: string
  previousAttemptId?: string
}

export interface StoredResult {
  result: AttemptResult
  meta: AttemptMeta
}

export interface FinishedSession {
  sessionId: string
  scenarioId: string
  summary: SessionSummary | null
  error: string | null
  historyOptIn: boolean
  endedAt: number
}

interface AppState {
  health: Health | null
  healthError: ApiError | null
  refreshHealth(): Promise<void>
  scenarios: Scenario[]
  scenariosError: ApiError | null
  scenario(id: string | undefined): Scenario | undefined
  settings: Settings
  saveSettings(patch: Partial<Settings>): Promise<void>
  micDeviceId: string
  setMicDeviceId(id: string): void
  results: Map<string, StoredResult>
  rememberResult(r: StoredResult): void
  lastSession: FinishedSession | null
  setLastSession(s: FinishedSession | null): void
}

const Ctx = createContext<AppState | null>(null)

export function AppProvider({ children }: { children: ReactNode }) {
  const [health, setHealth] = useState<Health | null>(null)
  const [healthError, setHealthError] = useState<ApiError | null>(null)
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [scenariosError, setScenariosError] = useState<ApiError | null>(null)
  const [settings, setSettings] = useState<Settings>(DEFAULT_SETTINGS)
  const [micDeviceId, setMic] = useState(() => localStorage.getItem(MIC_KEY) ?? '')
  const [results, setResults] = useState(() => new Map<string, StoredResult>())
  const [lastSession, setLastSession] = useState<FinishedSession | null>(null)

  const refreshHealth = useCallback(async () => {
    try {
      setHealth(await api.health())
      setHealthError(null)
    } catch (e) {
      setHealthError(toApiError(e))
    }
  }, [])

  useEffect(() => {
    void refreshHealth()
    api.scenarios().then(setScenarios, (e: unknown) => setScenariosError(toApiError(e)))
    api.settings().then((s) => setSettings({ ...DEFAULT_SETTINGS, ...s }), () => {})
  }, [refreshHealth])

  const value = useMemo<AppState>(
    () => ({
      health,
      healthError,
      refreshHealth,
      scenarios,
      scenariosError,
      scenario: (id) => scenarios.find((s) => s.scenario_id === id),
      settings,
      saveSettings: async (patch) => {
        const saved = await api.saveSettings(patch)
        setSettings({ ...DEFAULT_SETTINGS, ...settings, ...patch, ...saved })
      },
      micDeviceId,
      setMicDeviceId: (id) => {
        localStorage.setItem(MIC_KEY, id)
        setMic(id)
      },
      results,
      rememberResult: (r) => setResults((m) => new Map(m).set(r.result.attempt_id, r)),
      lastSession,
      setLastSession,
    }),
    [health, healthError, refreshHealth, scenarios, scenariosError, settings, micDeviceId, results, lastSession],
  )
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>
}

export function useApp(): AppState {
  const v = useContext(Ctx)
  if (!v) throw new Error('useApp outside AppProvider')
  return v
}

/** Voice for TTS: user setting, else the scenario default, else the first available voice. */
export function useVoiceId(scenario?: Scenario): string {
  const { settings, health } = useApp()
  return settings.voice_id ?? scenario?.default_voice_id ?? health?.components.tts?.voices?.[0]?.voice_id ?? ''
}

// --- tiny hash router ---------------------------------------------------------
export function useRoute(): string[] {
  const [hash, setHash] = useState(() => location.hash)
  useEffect(() => {
    const on = () => setHash(location.hash)
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])
  return hash.replace(/^#\/?/, '').split('/').filter(Boolean).map(decodeURIComponent)
}

export function navigate(...parts: string[]): void {
  location.hash = '/' + parts.map(encodeURIComponent).join('/')
}
