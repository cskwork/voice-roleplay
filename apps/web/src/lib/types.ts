// Shapes of gateway data used by the client. Scenario and feedback follow the contract;
// response wrappers the contract leaves open are parsed defensively in api.ts.

export type Difficulty = 'easy' | 'normal' | 'hard'
export type Mode = 'realtime' | 'recorded'

export interface TextItem {
  text_id: string
  en: string
  ko: string
}

export interface Goal {
  goal_id: string
  en: string
  ko: string
}

export interface Hint {
  hint_id: string
  goal_id: string
  ko: string
  keywords: string[]
  example_en: string
}

export interface FreeAnswerExercise {
  exercise_id: string
  question_en: string
  question_ko: string
  sample_answer_en: string
}

export interface Scenario {
  scenario_id: string
  version: string
  title_ko: string
  title_en: string
  ai_role: string
  ai_role_ko: string
  user_role_ko: string
  setting_ko: string
  default_voice_id: string
  facts: Record<string, string>
  opening_line: TextItem
  goals: Goal[]
  allowed_flow: string[]
  difficulty: Record<Difficulty, { guidance_en: string; silence_ms: number }>
  hints: Hint[]
  model_expressions: (TextItem & { goal_id: string })[]
  exercises: {
    reading: TextItem[]
    shadowing: TextItem[]
    free_answer: FreeAnswerExercise[]
  }
}

export interface Voice {
  voice_id: string
  label: string
  license_note?: string
}

export interface ComponentHealth {
  ready: boolean
  reachable?: boolean
  device?: string
  model_id?: string
  model?: string
  revision?: string
  model_revision?: string
  backend?: string
  streaming_mode?: string
  sample_rate?: number
  voices?: Voice[]
  /** Pronunciation worker only: {aligner, phones} model ids and revisions. */
  models?: Record<string, { model_id: string; revision: string } | null>
  bands_enabled?: boolean
}

export interface ModeAvailability {
  available: boolean
  reason_ko?: string | null
  session_active?: boolean
  feedback_available?: boolean
  model_audio_available?: boolean
  blocked_by_realtime?: boolean
}

/** Normalized from `GET /api/health` ({gateway, workers:{asr,tts,llm,vad,pron?}, modes, benchmark}). */
export interface Health {
  ready: boolean
  /** `pron` (optional pronunciation worker) is listed only when the gateway has it configured; not part of `ready`. */
  components: Partial<Record<'gateway' | 'vad' | 'asr' | 'tts' | 'llm' | 'pron', ComponentHealth>>
  /** What a new recorded attempt would get (PROTOCOL §12.4). */
  pronunciation: PronunciationStatusTop
  modes: Record<Mode, ModeAvailability>
  benchmark?: { status?: string } | null
}

export interface Settings {
  difficulty: Difficulty
  /** null = use the difficulty default (easy 1200, normal 900, hard 700). */
  silence_ms: number | null
  auto_barge_in: boolean
  voice_id: string | null
  history_opt_in: boolean
}

export const DEFAULT_SILENCE_MS: Record<Difficulty, number> = { easy: 1200, normal: 900, hard: 700 }

export type FeedbackStatus = 'observed' | 'suggested' | 'needs_confirmation' | 'unavailable'

export interface Feedback {
  feedback_id: string
  category: 'grammar' | 'expression' | 'vocabulary' | 'goal' | 'fluency_metric'
  severity: 'required' | 'optional'
  status: FeedbackStatus
  evidence_type: 'asr_text' | 'user_confirmed_text' | 'vad_metric' | 'target_diff' | 'word_timing' | 'prosody_contour' | 'phone_gop'
  source_turn_id?: string
  attempt_id?: string
  transcript_revision: number
  evidence_quote: string
  suggestion: string
  explanation_ko: string
}

export interface GoalState {
  goal_id: string
  status: 'done' | 'pending'
  evidence_turn_id?: string
}

export interface SessionSummary {
  improvements: Feedback[]
  goals: GoalState[]
  /** ok | held (nothing said) | unavailable (LLM not ready / failed) */
  status: string
  reason?: string | null
}

export type ExerciseType = 'reading' | 'shadowing' | 'free_answer' | 'roleplay_turn' | 'drill'

export type JobState =
  | 'queued'
  | 'transcribing'
  | 'analyzing'
  | 'synthesizing'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'

export const TERMINAL_JOB_STATES: readonly JobState[] = ['completed', 'failed', 'cancelled', 'expired']

export interface Job {
  job_id: string
  attempt_id?: string
  kind?: 'analyze' | 'reanalyze'
  state: JobState
  error_code?: string | null
  transcript_revision?: number | null
}

export interface Metrics {
  wpm: number | null
  speech_span_s: number
  word_count: number
  pause_count: number
  total_pause_s: number
  metrics_version: string
  definition_ko?: string
}

export interface TranscriptRevision {
  revision: number
  text: string
  source: 'asr' | 'user' | string
}

export interface ServerDiffOp {
  op: 'equal' | 'different' | 'missing' | 'extra'
  target: string | null
  heard: string | null
}

export interface ModelAudioRef {
  audio_id: string
  kind: 'target' | 'suggestion' | 'sample_answer' | 'next_ai' | string
  text: string
  url: string
}

export type PronunciationStatusTop = 'assessment_unavailable' | 'timing_only' | 'experimental_banded'
export type Band = 'good' | 'check' | 'practice'

/** A sound the phone model (experimental only) found closer to another sound; `heard_ipa` "" = no sound heard. */
export interface WeakSound {
  expected_ipa: string
  heard_ipa: string | null
  guide_ids: string[]
}

export interface PronWord {
  i: number
  word: string
  start_ms: number
  end_ms: number
  duration_ms: number
  gap_before_ms: number
  /** Scripted only: false = the aligner placed a word the ASR did not hear (interval uncertain); null = unknown. */
  in_transcript: boolean | null
  band: Band | null
  heard_ipa: string[] | null
  weak_sounds: WeakSound[] | null
  /** Guide entries whose sounds occur in this word ("this sound is in the word", never a judgement). */
  guide_ids: string[]
}

export interface PronWordStats {
  i: number
  mean_f0: number
  f0_range_st: number
  duration_ms: number
}

export interface Contour {
  hop_ms: number
  f0_hz: number[]
  per_word: PronWordStats[]
}

/** `pronunciation` of an attempt result (PROTOCOL §12.4). No numbers about quality ever reach the client. */
export interface Pronunciation {
  status: 'unavailable' | 'timing_only' | 'experimental_banded'
  reason: string | null
  mode: 'scripted' | 'unscripted' | null
  transcript_revision: number
  words: PronWord[]
  prosody: {
    learner: Contour | null
    model: (Contour & { audio_id: string; words: { i: number; word: string; start_ms: number; end_ms: number }[] }) | null
  }
  calibration_version: string | null
  model_revisions: { aligner?: string | null; phones?: string | null }
  evidence_types: string[]
  note_ko: string
}

/** content/pronunciation/guide.json (PA-8), served by `GET /api/pronunciation/guide`. */
export interface GuideEntry {
  entry_id: string
  category: string
  title_ko: string
  title_en: string
  target_ipa: string[]
  why_ko: string
  tip_ko: string
  minimal_pairs: { pair_id: string; a: { word: string; ipa?: string[] }; b: { word: string; ipa?: string[] } }[]
  practice_sentences: { sentence_id: string; en: string; ko: string }[]
}

export interface PronunciationGuide {
  version: string
  accent: string
  review_status: Record<string, string>
  entries: GuideEntry[]
}

export interface AttemptResult {
  attempt_id: string
  exercise_type: ExerciseType
  scenario_id: string
  exercise_ref?: string | null
  attempt_index?: number
  no_speech?: boolean
  transcript_revision: number | null
  transcript: string | null
  original_transcript?: string | null
  revisions: TranscriptRevision[]
  feedback: Feedback[]
  feedback_status?: string | null
  feedback_revision?: number | null
  metrics: Metrics | null
  target_en?: string | null
  target_diff?: ServerDiffOp[] | null
  question_en?: string | null
  next_ai?: { status: 'ok' | 'unavailable' | string; text?: string } | null
  model_audio?: ModelAudioRef[]
  pronunciation_score: null
  pronunciation_status: PronunciationStatusTop
  /** Absent from older gateways. */
  pronunciation?: Pronunciation | null
}

export interface ReviewItem {
  item_id: string
  text_en: string
  text_ko?: string | null
  source_type?: string
  source_id?: string | null
  box: number
  due_at: number // epoch seconds
  last_result?: string | null
}

/** One row of the review screen's history list, flattened from `GET /api/history`. */
export interface HistoryEntry {
  kind: 'session' | 'attempt'
  id: string
  scenario_id?: string
  exercise_type?: string
  created_at: number // epoch seconds
  text: string
  feedback_count: number
}
