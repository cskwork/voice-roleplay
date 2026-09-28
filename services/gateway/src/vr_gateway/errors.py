"""Error envelope `{"error": {"code", "message_ko"}}` shared by HTTP and WS."""

from __future__ import annotations

from fastapi.responses import JSONResponse

MESSAGES_KO: dict[str, str] = {
    "UNSUPPORTED_MODE": "지원하지 않는 모드입니다.",
    "AUDIO_TOO_LONG": "녹음이 너무 깁니다. 120초 이내로 다시 녹음해 주세요.",
    "AUDIO_TOO_LARGE": "파일이 너무 큽니다. 32 MiB 이하 WAV만 받을 수 있습니다.",
    "AUDIO_INVALID": "지원하지 않는 오디오 형식입니다. PCM16 WAV(1~2채널, 16/24/44.1/48 kHz)를 사용해 주세요.",
    "AUDIO_EXPIRED": "녹음이 만료되었습니다. 다시 녹음해 주세요.",
    "QUEUE_FULL": "분석 대기열이 가득 찼습니다. 잠시 후 다시 시도해 주세요.",
    "LOCAL_BUSY": "이전 실시간 회화를 끝내는 중입니다. 잠시 후 다시 시도해 주세요.",
    "MODEL_NOT_READY": "모델이 아직 준비되지 않았습니다.",
    "OUT_OF_MEMORY": "메모리가 부족합니다. 다른 작업을 종료한 뒤 다시 시도해 주세요.",
    "NOT_FOUND": "요청한 항목을 찾을 수 없습니다.",
    "AUTH_REQUIRED": "로컬 세션이 없습니다. 페이지를 새로고침해 주세요.",
    "ORIGIN_DENIED": "허용되지 않은 출처의 요청입니다.",
    "CSRF_INVALID": "요청 검증에 실패했습니다. 페이지를 새로고침해 주세요.",
    "IDEMPOTENCY_CONFLICT": "같은 요청 키가 다른 요청에 이미 사용되었습니다.",
    "INVALID_STATE": "지금 상태에서는 할 수 없는 요청입니다.",
    "INVALID_REQUEST": "요청 형식이 올바르지 않습니다.",
    "BODY_TOO_LARGE": "요청 본문이 너무 큽니다.",
    "WORKER_FAILED": "로컬 모델 처리 중 오류가 발생했습니다. 다시 시도해 주세요.",
    # Realtime-only codes
    "FRAME_INVALID": "잘못된 오디오 프레임을 무시했습니다.",
    "EVENT_INVALID": "잘못된 이벤트를 무시했습니다.",
    "ASR_FAILED": "음성 인식에 실패했습니다. 다시 말해 주세요.",
    "LLM_FAILED": "AI 답변 생성에 실패했습니다. 다시 시도해 주세요.",
    "TTS_FAILED": "음성 합성에 실패했습니다. 텍스트로 답변을 확인해 주세요.",
}

STATUS: dict[str, int] = {
    "UNSUPPORTED_MODE": 400,
    "AUDIO_TOO_LONG": 413,
    "AUDIO_TOO_LARGE": 413,
    "AUDIO_INVALID": 422,
    "AUDIO_EXPIRED": 410,
    "QUEUE_FULL": 429,
    "LOCAL_BUSY": 409,
    "MODEL_NOT_READY": 503,
    "OUT_OF_MEMORY": 503,
    "NOT_FOUND": 404,
    "AUTH_REQUIRED": 401,
    "ORIGIN_DENIED": 403,
    "CSRF_INVALID": 403,
    "IDEMPOTENCY_CONFLICT": 409,
    "INVALID_STATE": 409,
    "INVALID_REQUEST": 422,
    "BODY_TOO_LARGE": 413,
    "WORKER_FAILED": 502,
}


class ApiError(Exception):
    def __init__(self, code: str, status: int | None = None, message_ko: str | None = None):
        super().__init__(code)
        self.code = code
        self.status = status or STATUS.get(code, 400)
        self.message_ko = message_ko or MESSAGES_KO.get(code, "오류가 발생했습니다.")


def error_body(code: str, message_ko: str | None = None) -> dict:
    return {"error": {"code": code, "message_ko": message_ko or MESSAGES_KO.get(code, "오류가 발생했습니다.")}}


def error_response(code: str, status: int | None = None) -> JSONResponse:
    return JSONResponse(error_body(code), status_code=status or STATUS.get(code, 400))
