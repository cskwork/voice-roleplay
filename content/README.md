# Learning content

> **콘텐츠 검수 필요** — 이 폴더의 시나리오 문장, 한국어 번역, 모범 표현은 초안(v1.0.0)입니다.
> 출시 전 영어 원어민 검수와 한국어 번역 검수를 받아야 하며, 검수 전에는 "검수된 모범 표현"으로 표시하지 않습니다.

## Review status

| scenario_id | version | English review | Korean review |
|---|---|---|---|
| `cafe_order` | 1.0.0 | 필요 (pending) | 필요 (pending) |
| `hotel_checkin` | 1.0.0 | 필요 (pending) | 필요 (pending) |
| `directions` | 1.0.0 | 필요 (pending) | 필요 (pending) |
| `job_interview` | 1.0.0 | 필요 (pending) | 필요 (pending) |

All material is original and was not copied from any other app. When a scenario passes review, update its row here
and bump `version` if the text changed.

## Layout

- `scenarios/<scenario_id>.json` — one roleplay scenario per file, validated by `contracts/scenario.schema.json`.
- `voices/` — TTS reference voices (owned by the TTS worker, see `contracts/PROTOCOL.md` §4).

## Scenario format

| Field | Language | Used for |
|---|---|---|
| `scenario_id`, `version` | – | Identity; sessions record `scenario_version`. Use semver and bump on any text change. |
| `title_ko`, `title_en`, `ai_role_ko`, `user_role_ko`, `setting_ko` | KO / EN | Scenario list and session header. `setting_ko` tells the learner what they need to know (e.g. their reservation number). |
| `ai_role` | EN | Role given to the LLM. |
| `default_voice_id` | – | Voice under `content/voices/`. |
| `facts` | EN | Authoritative facts (prices, reservation, route, company). The LLM prompt includes them verbatim and must not contradict them. Prices are written `$D.CC`. |
| `opening_line` | EN + KO | First AI turn; pre-synthesized at startup. 1–2 sentences, at most one question. |
| `goals` | EN + KO | Exactly 3 goals tracked by `goal.update`. |
| `allowed_flow` | EN | What may happen in the conversation (LLM instructions). |
| `difficulty.{easy,normal,hard}` | EN | `guidance_en` for the LLM; `silence_ms` end-of-turn default (1200 / 900 / 700). |
| `hints` | KO + EN | One hint per entry, tied to a goal. Maps to `hint.request` levels: 1 = `ko`, 2 = `keywords`, 3 = `example_en`. |
| `model_expressions` | EN + KO | Model sentences for playback and the "다시 말하기" drill. |
| `exercises.reading` / `shadowing` | EN + KO | Sentence reading and shadowing practice. |
| `exercises.free_answer` | EN + KO | Question plus a sample answer. |

Rules checked by `tests/content/test_scenarios.py`:

- Every `text_id`, `hint_id`, and `exercise_id` is globally unique and starts with `<scenario_id>_`.
  `text_id` is what `GET /api/tts/cached` accepts, so never reuse or rename one without bumping `version`.
- Every hint and model expression points to an existing goal; every goal has at least one of each.
- English fields contain no Hangul; `ko` / `*_ko` fields contain Hangul.
- Spoken English lines (`opening_line`, model expressions, reading, shadowing) are at most 400 characters (TTS limit).
- Every `$` amount in `facts` is `$D.CC`.

Run the checks:

```sh
uv run --no-project --with jsonschema --with pytest python -m pytest tests/content
```
