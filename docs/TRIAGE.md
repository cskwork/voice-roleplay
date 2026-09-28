# 다음 작업 트리아지 (2026-09-28, v0.1.0 이후)

v0.1.0의 녹음형 연습을 2025~2026년 영어 학습 앱의 발음 분석과 비교한 결과, 그리고 v0.1.0 릴리스 노트에 남긴 미완료 항목을 우선순위로 정리했습니다. 앱 기능 조사는 2026-09-28 기준 공개 자료이며, 출처가 마케팅 페이지뿐인 항목은 "미확인"으로 표시했습니다.

## 1. 현재 수준 판단

**결론: 발음 분석 기능으로는 최신 앱들과 견줄 수준이 아닙니다.** 녹음형 연습은 현재 문법·표현 교정과 읽기 연습 도구이며, 발음 평가는 PRD §8.2·§8.4에 따라 의도적으로 넣지 않았습니다(`pronunciation_score: null`, `assessment_unavailable`).

| 기능 | v0.1.0 | 2026년 경쟁 앱의 기본선 | 선두 앱 (ELSA 등) |
|---|---|---|---|
| 전체 발음 점수 | 없음 | 0~100 점수 | 있음 |
| 단어별 판정·색 표시 | ASR 기반 "다르게 인식된 부분"만 (발음 판정 아님) | 단어별 점수, 초록/노랑/빨강 | 있음 |
| 음소(소리) 단위 진단 | 없음 | 일부 | 소리별 %, "X 대신 Y로 발음" |
| 강세·억양(운율) | 없음 | 요구되기 시작 (ELSA, Praktika "rhythm", Azure prosody) | 단어·문장 강세, 억양 |
| 유창성 | 분당 단어 수, 쉰 횟수·시간 (참고 지표) | 유창성 하위 점수 | 머뭇거림·군말 분석 |
| 내 음성 vs 모범 음성 비교 | 모범 음성만 재생 | 나란히 재생 | 파형·나란히 재생 |
| 입 모양·혀 위치 안내 | 없음 | 드묾 | 애니메이션 (ELSA) |
| 자유 발화 중 발음 피드백 | 없음 | Praktika, ELSA Speech Analyzer, Speak 튜터 레슨 | 있음 |
| 약한 소리 반복 연습 | 없음 (문장 단위 다시 말하기만) | 즉시 재시도 | 약점 소리 커리큘럼 |

앱별 근거:
- **ELSA Speak:** 소리·음절·단어 정확도, 강세·억양, 유창성을 채점하고 입 모양 애니메이션을 보여 줍니다. 자유 발화 분석(Speech Analyzer)도 있습니다.
- **Speak(스픽):** 공식 기술 페이지는 소리 단위 분석을 주장합니다. 하지만 2024년 엔지니어링 글에서는 단어 단위만 제공한다고 했고, 자세한 방식은 미확인입니다.
- **Praktika:** 대화 중 단어별 점수(발음·유창성·완성도·리듬)와 재시도를 제공합니다. 출처는 제3자 리뷰입니다.
- **Duolingo:** 스크립트 연습은 단어 인식 합격/불합격으로 판정합니다. Video Call의 발음 피드백 수준은 미확인입니다.
- **Google 번역 "Pronounce":** 2026-04 출시. 점수와 함께 불명확한 소리를 표시합니다.
- **Microsoft Azure Pronunciation Assessment:** 많은 앱이 쓰는 엔진입니다. 음소·단어·문장 정확도, 유창성, 완성도, 운율(en-US)을 채점하고 오류 유형(누락·삽입·오발음·끊김·단조로움)과 실제로 발음된 음소 후보(IPA)를 줍니다.

현재 방식의 알려진 약점: Qwen3-ASR은 문맥으로 단어를 보정하는 경향이 있습니다. 그래서 "다르게 인식된 부분"은 실제 발음 오류를 놓치기 쉽습니다(측정 전, PA-0에서 확인).

전체 출처는 [부록](#부록-출처)에 있습니다.

## 2. 지켜야 할 원칙 (PRD §8 유지)

- 사람 평가로 보정·검증하기 전에는 발음 점수를 노출하지 않습니다. 최고 수준의 공개 시스템도 사람 평가자와의 상관이 0.6~0.75 정도이고(Azure 약 0.70), 작은 점수 차이는 의미가 없습니다. 노출하더라도 숫자 대신 **등급(초록/노랑/빨강)**으로 보여 줍니다.
- **악센트 편향이 있습니다.** 공개 보정 데이터(speechocean762)는 중국어 모국어 화자뿐입니다. 한국인 학습자 음성으로 따로 검증하기 전에는 정확도를 주장하지 않습니다.
- 모든 판정에 근거(어느 단어, 어느 구간, 어떤 모델)를 붙입니다. 불확실하면 `needs_confirmation`으로 보류합니다.
- 전부 로컬·오프라인으로 처리합니다. 클라우드 발음 API(Azure 등)로 자동 전환하지 않습니다.
- 라이선스를 확인합니다. Parselmouth는 GPL-3.0이므로 배포할 경우 피하고, MIT인 pyworld를 우선 검토합니다. CTC-GOP 저장소는 라이선스가 명시되지 않았으므로 확인 전에는 쓰지 않습니다.

## 3. 트리아지

크기: S = 1~2일, M = 3~5일, L = 1~2주 (한 명 기준 추정). 우선순위: P0 = 다음에 바로, P1 = 그다음, P2 = 여유가 있을 때.

### 발음 분석 (PA)

| ID | 우선순위 | 작업 | 완료 기준 | 크기 | 의존 |
|---|---|---|---|---|---|
| PA-0 | P0 | **검증 기반.** 동의받은 한국인 학습자 녹음 평가셋(초기 목표: 20명 이상, 400발화, 스크립트·자유 발화 포함)과 사람 평가 라벨(음소·단어·문장, 평가자 2인 이상), speechocean762 벤치 하네스(`benchmarks/pronunciation/`) 구축. 현재 ASR diff가 실제 오발음을 얼마나 놓치는지도 측정 | 평가셋 매니페스트, 라벨 가이드, 평가자 간 일치도, 기준선 리포트 커밋. 동의 없는 음성 사용 0건 | L | — |
| PA-1 | P0 | **단어 타이밍과 비교 재생 (점수 없음).** `Qwen3-ForcedAligner-0.6B`(Apache-2.0, 단어 타이밍, MLX 포트 있음)를 녹음형 후처리에 추가. 결과 화면에서 단어를 누르면 "내 발음" 구간과 "모범 음성" 같은 단어를 나란히 재생하고, 단어별 길이·앞뒤 쉼을 표시 | 기존 PRD §4 선택 기능 범위 안에서 동작. 정렬 모델이 없으면 기능 비가용 표시(AT-18). 실시간 경로에 영향 없음. 정렬 오차를 PA-0 셋에서 측정 | M | — |
| PA-2 | P0 | **음소 인식 + GOP 프로토타입 (오프라인 평가만).** `facebook/wav2vec2-lv-60-espeak-cv-ft`(Apache-2.0, IPA 음소)로 CTC 기반 GOP 계산. 단어별 "X 대신 Y로 들렸을 가능성" 후보 생성. speechocean762와 PA-0 셋에서 사람 평가와의 상관을 측정 | 벤치 리포트(음소·단어 상관, 한국어 악센트 별도). UI 노출 없음. 채택 기준 제안(예: 음소 수준 상관 ≥ 0.55, 단어 수준 ≥ 0.5를 PA-0 셋에서 충족) | L | PA-0 |
| PA-3 | P1 | **점수 보정과 등급화.** GOPT 방식 헤드(BSD-3) 또는 단순 회귀로 음소→단어→문장 점수 산출. PA-0 라벨로 초록/노랑/빨강 임계값 조정. `pronunciation_status: "assessed_banded"`와 모델·보정 버전 기록 | 채택 기준 충족 시에만 기능 플래그 해제. 미충족이면 `assessment_unavailable` 유지. 모든 판정에 근거 구간 연결 | M | PA-2 |
| PA-4 | P1 | **발음 결과 UI.** 단어 색 표시(등급), 단어를 누르면 가능성 높은 오발음 소리·짧은 한국어 팁·내 음성과 모범 음성 비교. 약한 단어만 골라 다시 말하기. 약한 단어·소리를 복습(간격 반복)에 추가 | 키보드·스크린리더 지원. 색만으로 의미 전달 금지. 불확실 판정은 "이렇게 들렸어요. 맞나요?"로 보류 | M | PA-1, PA-3 |
| PA-5 | P1 | **운율(강세·억양) 참고 지표.** pyworld(MIT)로 피치 추출. 학습자 문장과 TTS 모범 문장의 억양 곡선·단어 길이 비교 그래프, 단조로움·끊김 표시 | 점수가 아닌 "참고 지표"로 표시. 한국어 학습자 셋에서 의미 있는지 검토 후 노출 | M | PA-1 |
| PA-6 | P2 | **자유 발화 발음 피드백.** 전사를 기준 텍스트로 삼아 평가하는 방식(Azure 문서 권장 방식과 같음)을 턴제 역할극·자유 답변에 적용. ASR 오류가 가짜 오발음이 되지 않도록 ASR 신뢰도·전사 확인과 연동 | 스크립트 연습 대비 오탐률 측정. 실시간 회화 중에는 끼어들지 않고 세션 요약에서만 제공 | M | PA-3 |
| PA-7 | P0 | **PRD·계약 갱신.** PRD §8.4 조건(보정 데이터, 채택 기준, 등급 표시), `pronunciation_status` 값, 증거 유형(`phone_gop`, `word_timing`), 모델 잠금 항목 추가 | 문서 리뷰 완료. 구현 전 합의 | S | — |
| PA-8 | P2 | **입 모양·조음 안내 콘텐츠.** 한국어 화자가 자주 틀리는 소리(r/l, f/p, v/b, th, 모음 길이 등)별 짧은 설명과 최소 대립쌍(minimal pair) 연습. 원어민·번역 검수 | 콘텐츠 검수 완료 표시. PA-4 팁과 연결 | M | PA-4 |

권장 순서: **PA-7 → PA-0 + PA-1 (병행) → PA-2 → PA-3 → PA-4 → PA-5 → PA-6 → PA-8.** PA-1만으로도 "내 발음 vs 모범 발음 비교"라는 체감 기능을 점수 없이 먼저 낼 수 있습니다.

### v0.1.0에서 넘어온 항목 (CO)

| ID | 우선순위 | 작업 | 완료 기준 | 크기 |
|---|---|---|---|---|
| CO-1 | P0 | 남은 브라우저 E2E 실행: 끼어들기(AT-02/03), 턴 종료(AT-05/06), API 거절(AT-11/12/20), 오프라인·로그 누출·프롬프트 주입(AT-15/16/21), 실제 장치 캡처 스모크 | 전 스펙 통과 또는 실패 근거와 함께 기록 | M |
| CO-2 | P0 | MLX TTS 기준 전체 스택 벤치마크(`./app benchmark`, 최소 30턴, PRD 권장 200턴): 응답 시작 p50/p95, 끼어들기 정지, 청크 간 공백, 녹음형 30초·120초 | `benchmarks/results/`에 리포트. 미달 목표는 미달로 표시 | M |
| CO-3 | P0 | 개발용 AI 목소리 2개를 사용 권리가 확인된 영어 음성으로 교체 | `content/voices/*/SOURCE.md`에 권리 근거 | S (외부 의존) |
| CO-4 | P1 | 시나리오·번역·모범 표현 원어민/번역 검수 | `content/README.md` 검수 상태 갱신 | M (외부 의존) |
| CO-5 | P1 | 세션 요약의 "다시 말하기" 연습 화면 실제 확인(개선점이 나오는 대화로 E2E 추가) | 스크린샷·E2E 통과 | S |
| CO-6 | P2 | 힌트 요청·응답 매칭: 늦게 온 1단계 번역이 새 요청의 목표 힌트와 섞일 수 있는 경쟁 조건 수정(요청 ID 부여) | 게이트웨이 테스트 추가 | S |
| CO-7 | P2 | AI 발화가 다음 답변 시작 시점에만 이력에 기록되는 문제 검토(목표·요약이 한 줄 늦을 수 있음) | 원인 분석과 수정 또는 의도 문서화 | S |
| CO-8 | P2 | NVIDIA/vLLM 경로 실제 실행 검증 | Linux + NVIDIA 장비에서 두 모드 통과 기록 | L (장비 의존) |

## 부록: 출처

2026-09-28 조사. 날짜는 해당 자료의 게시·갱신일입니다.

- ELSA 피드백(2025-10-24): https://blog.elsaspeak.com/en/advantage-of-elsa-feedback/ · Speech Analyzer: https://elsaspeak.com/en/speech-analyzer
- Speak(스픽) 기술 페이지: https://www.speak.com/ko/technology · ASR 글(2024-06-10): https://www.speak.com/blog/asr-levelup · 튜터 레슨: https://help.speak.com/en/articles/11966855-what-are-tutor-lessons
- Praktika: https://intercom.help/praktika-ai/en/articles/10707916-learning-process · 리뷰: https://languatalk.com/blog/praktika-review/
- Duolingo 2025 하이라이트(2025-12-10): https://blog.duolingo.com/product-highlights/ · 말하기 접근(2026-02): https://blog.duolingo.com/covering-all-the-bases-duolingos-approach-to-speaking-skills/
- Babbel(2023-11-21): https://www.babbel.com/press/en-us/releases/learn-with-your-own-voice-babbel-launches-two-new-speech-based-features-us
- Cake: https://apps.apple.com/kr/app/id1350420987 · 말해보카: https://apps.apple.com/kr/app/id1460766549
- Google 번역 발음 연습(2026-04-29): https://techcrunch.com/2026/04/29/google-translate-now-lets-you-practice-pronunciation/
- Azure Pronunciation Assessment(2026-07-03 갱신): https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-pronunciation-assessment
- wav2vec2 음소 모델: https://huggingface.co/facebook/wav2vec2-lv-60-espeak-cv-ft
- CTC 기반 GOP: https://arxiv.org/abs/2507.16838 · https://github.com/frank613/CTC-based-GOP
- GOPT: https://github.com/YuanGongND/gopt
- speechocean762: https://www.openslr.org/101/ · https://arxiv.org/abs/2104.01378
- Phi-4-MM 발음 채점: https://arxiv.org/html/2509.02915v1
- Montreal Forced Aligner: https://arxiv.org/abs/2606.18466 · https://github.com/MontrealCorpusTools/mfa-models
- Qwen3-ForcedAligner-0.6B: https://huggingface.co/Qwen/Qwen3-ForcedAligner-0.6B
- Parselmouth(GPL-3.0): https://github.com/YannickJadoul/Parselmouth · pyworld(MIT): https://github.com/JeremyCCHsu/Python-Wrapper-for-World-Vocoder
- 악센트 편향: https://arxiv.org/abs/2606.11639 · https://onlinelibrary.wiley.com/doi/10.1002/tesq.70130
