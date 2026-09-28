import type { Voice } from './types'

/**
 * Voice for TTS: the saved setting if the TTS worker offers it, else the scenario default, else the first voice.
 * A saved voice can have been removed since (e.g. the former dev voices); it is then ignored instead of making
 * every playback fail. While the worker's voice list is unknown the saved voice is used unchecked.
 */
export function pickVoiceId(saved: string | null, scenarioDefault: string | undefined, voices: Voice[] | undefined): string {
  const offered = voices?.length ? voices.map((v) => v.voice_id) : null
  if (saved && (!offered || offered.includes(saved))) return saved
  return scenarioDefault ?? offered?.[0] ?? ''
}

/** Value for the settings voice picker: '' (scenario default) when the saved voice is no longer offered. */
export function savedVoiceChoice(saved: string | null, voices: Voice[]): string {
  return saved && voices.some((v) => v.voice_id === saved) ? saved : ''
}
