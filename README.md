# voice-roleplay

내 컴퓨터에서만 돌아가는 영어 말하기 연습 서비스입니다. 브라우저에서 AI와 영어로 역할극 대화를 하거나(실시간 모드), 문장 읽기·따라 말하기·자유 답변을 녹음해 전사와 텍스트 피드백을 받습니다(녹음형 모드). 음성 인식(Qwen3-ASR-0.6B), 음성 합성(Fun-CosyVoice3-0.5B-2512), 대화 모델(Qwen3-4B-Instruct-2507, llama.cpp)이 모두 로컬에서 실행되고, 설치가 끝난 뒤에는 인터넷 연결 없이 동작합니다.

제품 요구사항은 `docs/PRD.md`, 컴포넌트 간 계약은 `contracts/PROTOCOL.md`에 있습니다.

## 현재 상태

개발 중인 버전이며 출시 기준(PRD §15, §18)을 통과하지 않았습니다.

측정된 것 (Apple M3 Pro 36 GB, macOS 26.6.2, 2026-09-27. 다른 작업이 같은 기기에서 돌고 있었으므로 참고용):

| 항목 | 결과 | 출처 |
|---|---|---|
| TTS 스트리밍 첫 청크 p50 / RTF p50·p95, MLX fp16 (기본값) | 1.15 s / 0.55 · 0.72 (48문장) | `workers/tts/benchmarks/2026-09-27T064944+0000-mlx-default-fp16-s5-h50-runs2.json` |
| TTS 같은 항목, PyTorch hybrid (이전 기본값) | 2.97 s / 1.91 · 2.83 | `workers/tts/README.md`, `workers/tts/benchmarks/` |
| LLM 첫 토큰(프리픽스 예열 후 첫 턴) | 264 ms | `config/llm/README.md`, `workers/feedback/bench/results/` |
| ASR 장치 비교 | MPS가 CPU fp32보다 약 2배 빠름 | `workers/asr/README.md` |
| 전체 스택 1회 실행 (`tests/integration/stack_smoke.py`) | 마지막 발화 샘플 → 첫 AI 오디오 프레임 전송 5.6 s(턴 종료 침묵 0.94 s 포함), 끼어들기 음성 시작 → `response.cancelled` 0.32 s, 3초 읽기 녹음 작업 1.0 s | 단일 실행, 벤치마크 아님. 서버 전송 시각이며 실제 재생 시각이 아님 |

측정되지 않았거나 미달인 것:

- TTS는 Apple Silicon용 MLX 변환(`mlx-community/Fun-CosyVoice3-0.5B-2512-fp16`, 공식 가중치의 제3자 변환)으로 바꾼 뒤 TTS 단독 측정에서 RTF 목표(p95 ≤ 0.8)를 충족했습니다. ASR·LLM과 함께 올린 전체 스택에서 다시 재지는 않았으므로, 실시간 응답 시작 목표(p50 ≤ 2초)는 아직 확인되지 않았습니다. 위 전체 스택 스모크 수치는 PyTorch TTS 기준입니다.
- `./app benchmark`는 아직 틀만 있습니다(`benchmarks/run.sh`). 200턴 전체 스택 측정, 학습자 평가셋 WER, 60분 안정성 테스트는 하지 않았습니다.
- 실제 브라우저 종단 간 테스트(Playwright, `tests/e2e/`)는 작성 중에 작업이 중단되어 실행·검증되지 않았습니다. 위 스모크 테스트는 macOS `say`로 만든 음성을 WebSocket에 20 ms 프레임으로 넣은 결과입니다.

Apple Silicon에서의 차이 (PRD 기준 프로필은 Linux + NVIDIA):

- ASR은 vLLM 스트리밍 대신 transformers 백엔드(MPS)에서 부분 재디코딩으로 임시 자막을 만듭니다. vLLM 어댑터는 있지만 실행해 본 적이 없습니다.
- TTS는 기본값으로 MLX 백엔드(`VR_TTS_BACKEND=auto`)를 씁니다. `VR_TTS_BACKEND=torch`로 공식 PyTorch 코드를 쓸 수 있으며, 이때는 LLM을 CPU, flow를 MPS, HiFT를 CPU에 나눠 올립니다(전부 MPS에 올리면 Metal이 충돌).
- LLM은 Homebrew llama-server(Metal)를 씁니다.

출시 전에 바꿔야 하는 것:

- 음성 `content/voices/dev_voice_a`, `dev_voice_b`는 CosyVoice 저장소에 들어 있는 중국어 프롬프트 음성으로 만든 개발용입니다(`개발용 — 출시 전 권리 확인된 음성으로 교체 필요`). 권리가 확인된 영어 음성으로 교체해야 합니다.
- 시나리오 문장, 한국어 번역, 모범 표현은 초안입니다. 원어민 검수와 번역 검수가 필요합니다(`content/README.md`).
- 발음 점수는 없습니다. 모든 결과에 `pronunciation_score: null`, `assessment_unavailable`로 표시합니다.

## 빠른 시작

필요한 것: Apple Silicon Mac(검증: M3 Pro 36 GB), 디스크 여유 약 25 GB(모델 약 16 GB, Python 환경 약 4 GB), [uv](https://docs.astral.sh/uv/), git, Node.js 22 이상, `brew install llama.cpp`.

```sh
./app setup      # 설치 내용을 보여 주고 y/N으로 동의를 받은 뒤 진행 (--yes: 묻지 않음)
./app doctor     # 장비, 런타임, 모델 해시, 포트, 음성, 오프라인 준비 상태 표
./app start      # 워커 예열이 끝나면 http://127.0.0.1:8710 출력
./app stop       # 진행 중 작업 취소, 워커 종료, 남은 프로세스 정리
```

`./app setup`이 하는 일: 각 Python 컴포넌트를 잠금 파일 그대로 `uv sync --frozen`, `workers/tts/setup.sh`로 `vendor/CosyVoice`를 고정 커밋에 clone, `apps/web`에서 `npm ci && npm run build`, `models.lock.json`에 적힌 파일만 고정 revision으로 내려받고 SHA-256 확인. 네트워크는 setup에서만 씁니다. `./app start`는 아무것도 내려받지 않고, 모델 파일이나 환경이 없으면 시작을 거부합니다.

처음 시작할 때는 모델 로딩(약 25초)과 시나리오 첫 대사 음성 합성(한 번만, 약 1분)이 끝날 때까지 기다립니다. 첫 대사 음성은 `var/cache/tts/`에 남아 다음부터는 바로 시작합니다.

## 구조

구조도(시스템, 실시간 한 턴과 끼어들기, 세션 상태)와 설명은 [`docs/architecture/`](docs/architecture/README.md)에 있습니다.

![시스템 구조](docs/architecture/system.png)

```
 브라우저 (apps/web, React)
   │  HTTP + WebSocket, 127.0.0.1:8710, 쿠키 + CSRF + Origin 검사
   ▼
 gateway (services/gateway, FastAPI)
   ├─ Silero VAD(onnxruntime, CPU): 발화 시작/끝, 끼어들기
   ├─ 실시간 엔진: ASR 스트림 → LLM 스트림 → 문장 분할 → TTS 스트림
   ├─ 녹음형 작업 큐(1개 실행, 2개 대기, 오디오는 메모리에만)
   ├─ 피드백·목표·힌트·요약(workers/feedback 라이브러리)
   └─ SQLite(var/data, 기록 저장에 동의한 경우만)
        │ X-Worker-Token (시작할 때마다 새로 생성)
        ├──▶ ASR worker   :8711  Qwen3-ASR-0.6B (transformers, MPS)
        ├──▶ TTS worker   :8712  Fun-CosyVoice3-0.5B-2512 (MLX 기본, torch 선택)
        └──▶ llama-server :8713  Qwen3-4B-Instruct-2507 Q4_K_M (slot 0 대화, slot 1 백그라운드)
```

게이트웨이는 `./app start`에서 `--manage-workers`로 실행되어 세 워커를 띄우고 멈춥니다. 워커 로그는 `var/log/`, pid는 `var/run/`에 있습니다.

## 개인정보 기본값

- 모든 서버는 127.0.0.1에만 바인딩합니다. LAN 접속은 지원하지 않습니다.
- 마이크 음성은 디스크나 DB에 쓰지 않습니다. 녹음형 연습의 음성은 작업이 끝나거나 취소·실패하면 메모리에서 해제합니다.
- 전사와 피드백은 기본적으로 세션 메모리에만 있고, 저장하지 않은 요약은 15분 뒤 사라집니다. 설정에서 기록 저장을 켠 경우에만 `var/data/app.sqlite3`에 저장합니다. 설정 화면에서 전체 삭제할 수 있고, JSON/Markdown 내보내기는 현재 API(`POST /api/history/export`)로만 제공합니다.
- 로그에는 이벤트 이름, id, 길이, 상태, 오류 코드만 남깁니다. 전사, TTS 문장, 프롬프트, LLM 출력, 오디오는 기록하지 않습니다.
- 실행 중 외부 통신(텔레메트리, CDN, 모델 다운로드)은 없습니다. 워커는 `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`로 시작합니다.
- 앱이 파일을 쓰지 않아도 OS의 swap이나 crash dump에 흔적이 남을 수 있습니다. 엄격한 환경이라면 디스크 암호화를 따로 적용하세요.

## 문제 해결

| 증상 | 확인할 것 |
|---|---|
| `./app start`가 "port ... is in use"로 거부 | 이전 실행이 남았으면 `./app stop`. 다른 프로그램이 쓰는 포트라면 `VR_ASR_PORT`, `VR_TTS_PORT`, `VR_LLM_PORT`, `VR_GATEWAY_PORT`로 다른 포트를 지정할 수 있습니다(예: `VR_LLM_PORT=18713 ./app start`). |
| "model file ...: not downloaded" 또는 해시 불일치 | `./app setup`을 다시 실행하면 빠졌거나 다른 파일만 다시 받습니다. `./app doctor --full`은 캐시 없이 모든 해시를 다시 계산합니다. |
| 시작 중 "worker(s) exited" | `var/log/asr.log`, `tts.log`, `llm.log`를 보세요. 종료 코드 2는 설정 문제(메시지에 설치 안내), 3은 모델 로딩 실패입니다. |
| AI 음성이 늦게 나옴 | `/api/health`의 TTS `device`가 `mlx`인지 확인하세요(torch 백엔드는 실시간보다 느림). 녹음형 연습이나 다른 무거운 작업이 동시에 돌고 있지 않은지도 확인하세요. |
| AI 목소리 때문에 대화가 끊김 | 헤드셋을 쓰세요. 에코가 두 번 의심되면 자동 끼어들기가 꺼지고 "눌러 말하기"를 권합니다. |
| "전송이 2초 넘게 밀리고 있어요" 경고 | 음성이 서버로 늦게 가고 있습니다. 일시정지 후 다시 시작하거나, 계속되면 회화를 끝내고 녹음 연습을 쓰세요. 앱은 밀린 음성을 버리지 않습니다. |
| 녹음형 제출이 `LOCAL_BUSY` | 실시간 회화가 진행 중이면 녹음형 분석을 막습니다. 회화를 끝낸 뒤 다시 제출하세요. |

## 테스트

```sh
cd services/gateway && .venv/bin/python -m pytest                        # FAKE 워커(표시됨) + 실제 Silero VAD
cd services/gateway && .venv/bin/python -m pytest ../../workers/feedback/tests -m "not integration"
cd apps/web && npm run build && npx vitest run
uv run --no-project --with jsonschema --with pytest python -m pytest tests/content
services/gateway/.venv/bin/python tests/integration/stack_smoke.py      # ./app start 후 실제 모델로 실행
```

잠금 정보: `models.lock.json`(모델 파일별 SHA-256, 라이선스, 변환 출처, 런타임 버전, CosyVoice 커밋), `*/uv.lock`, `apps/web/package-lock.json`.

## English summary

voice-roleplay is a local English speaking-practice web app for Korean learners: realtime spoken roleplay with an AI partner, and recorded practice (reading, shadowing, free answers) with transcripts and text-only feedback. ASR (Qwen3-ASR-0.6B), TTS (Fun-CosyVoice3-0.5B-2512) and the LLM (Qwen3-4B-Instruct-2507 Q4_K_M via llama.cpp) all run on the machine, and after `./app setup` nothing needs the network.

Status: development build, validated only on an Apple M3 Pro (36 GB). With the MLX conversion of CosyVoice3 (default on Apple Silicon), TTS alone measured RTF p95 0.72 and first chunk p50 1.15 s; the full-stack realtime latency targets have not been re-measured with it yet. Architecture diagrams are in `docs/architecture/`. The full-stack benchmark is a stub. The two voices are development placeholders that must be replaced with properly licensed English voices, and the scenario content still needs native-speaker and translation review. There is no pronunciation scoring.

Quick start: `./app setup` (asks for consent), `./app doctor`, `./app start` (prints `http://127.0.0.1:8710`), `./app stop`. Everything binds to 127.0.0.1, audio is never written to disk, and transcripts are stored only if the learner opts in.
