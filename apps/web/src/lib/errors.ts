const MESSAGES: Record<string, string> = {
  UNSUPPORTED_MODE: '지금 장비에서는 이 모드를 사용할 수 없어요.',
  AUDIO_TOO_LONG: '녹음이 너무 길어요. 120초 이내로 다시 녹음해 주세요.',
  AUDIO_TOO_LARGE: '녹음 파일이 너무 커요. 더 짧게 다시 녹음해 주세요.',
  AUDIO_INVALID: '녹음 파일을 읽을 수 없어요. 다시 녹음해 주세요.',
  AUDIO_EXPIRED: '녹음이 만료되어 분석할 수 없어요. 다시 녹음해 주세요.',
  QUEUE_FULL: '분석 대기열이 가득 찼어요. 잠시 후 다시 제출해 주세요.',
  LOCAL_BUSY: '실시간 회화가 진행 중이에요. 회화를 끝낸 뒤 다시 시도해 주세요.',
  MODEL_NOT_READY: '모델이 아직 준비되지 않았어요. 시작 화면에서 상태를 확인해 주세요.',
  OUT_OF_MEMORY: '메모리가 부족해 처리하지 못했어요. 다른 작업을 줄이고 다시 시도해 주세요.',
  NOT_FOUND: '요청한 항목을 찾을 수 없어요. 만료되었을 수 있어요.',
  AUTH_REQUIRED: '로컬 인증이 만료되었어요. 페이지를 새로고침해 주세요.',
  ORIGIN_DENIED: '허용되지 않은 주소에서 접속했어요. http://127.0.0.1:8710 으로 열어 주세요.',
  CSRF_INVALID: '보안 토큰이 만료되었어요. 페이지를 새로고침해 주세요.',
  IDEMPOTENCY_CONFLICT: '같은 요청이 이미 다른 내용으로 처리되었어요.',
  INVALID_STATE: '지금 상태에서는 할 수 없는 작업이에요.',
  WORKER_FAILED: '모델 처리 중 오류가 났어요. 다시 시도해 주세요.',
  FRAME_INVALID: '음성 데이터 전송에 문제가 있었어요.',
  NETWORK: '로컬 서버에 연결할 수 없어요. 서비스가 실행 중인지 확인해 주세요.',
  MIC_DENIED: '마이크 권한이 거부되었어요. 브라우저 주소창의 권한 설정에서 마이크를 허용해 주세요.',
  MIC_NOT_FOUND: '사용할 수 있는 마이크가 없어요. 마이크를 연결해 주세요.',
  MIC_FAILED: '마이크를 시작하지 못했어요. 다른 앱이 사용 중인지 확인해 주세요.',
}

export class ApiError extends Error {
  constructor(
    readonly code: string,
    readonly messageKo: string,
    readonly status = 0,
  ) {
    super(code)
    this.name = 'ApiError'
  }
}

/** Korean message for an error code; server-provided text wins when present. */
export function messageFor(code: string, serverMessage?: string): string {
  return serverMessage || MESSAGES[code] || '알 수 없는 오류가 발생했어요. 다시 시도해 주세요.'
}

export function toApiError(e: unknown): ApiError {
  if (e instanceof ApiError) return e
  if (e instanceof DOMException) {
    if (e.name === 'NotAllowedError' || e.name === 'SecurityError') return new ApiError('MIC_DENIED', messageFor('MIC_DENIED'))
    if (e.name === 'NotFoundError' || e.name === 'OverconstrainedError') return new ApiError('MIC_NOT_FOUND', messageFor('MIC_NOT_FOUND'))
    if (e.name === 'NotReadableError' || e.name === 'AbortError') return new ApiError('MIC_FAILED', messageFor('MIC_FAILED'))
  }
  if (e instanceof TypeError) return new ApiError('NETWORK', messageFor('NETWORK'))
  return new ApiError('UNKNOWN', messageFor('UNKNOWN'))
}
