import os

import pytest

LLM_URL = os.environ.get("VR_LLM_URL", "http://127.0.0.1:8713")
LLM_API_KEY = os.environ.get("VR_LLM_API_KEY") or None

# Small inline scenario following content/scenarios/*.json (contracts/scenario.schema.json).
CAFE = {
    "scenario_id": "test_cafe",
    "version": "1.0.0",
    "title_ko": "카페 주문",
    "title_en": "Ordering at a cafe",
    "ai_role": "barista",
    "ai_role_ko": "바리스타",
    "user_role_ko": "손님",
    "setting_ko": "동네 카페 카운터",
    "default_voice_id": "dev_female_1",
    "facts": {
        "cafe_name": "Maple Bean Cafe",
        "menu": "Americano $3.50, Latte $4.50, Cappuccino $4.25, Blueberry muffin $2.75",
        "sizes": "Regular or large; large costs $0.75 more",
        "milk_options": "whole, oat, almond (oat and almond cost $0.50 more)",
        "payment": "card or cash",
    },
    "opening_line": {"text_id": "test_cafe.opening", "en": "Hi, welcome to Maple Bean Cafe! What can I get for you today?", "ko": "안녕하세요, 메이플 빈 카페입니다! 무엇을 드릴까요?"},
    "goals": [
        {"goal_id": "order_drink", "en": "Order a drink", "ko": "음료 주문하기"},
        {"goal_id": "change_option", "en": "Ask for or change an option (size or milk)", "ko": "옵션 요청·변경하기"},
        {"goal_id": "check_price", "en": "Ask how much it costs", "ko": "가격 확인하기"},
    ],
    "allowed_flow": [
        "Greet the customer and take the order",
        "Offer size and milk options",
        "Tell the total price and ask how they will pay",
    ],
    "difficulty": {
        "easy": {"guidance_en": "Speak slowly with very simple words.", "silence_ms": 1200},
        "normal": {"guidance_en": "Speak naturally like a friendly barista.", "silence_ms": 900},
        "hard": {"guidance_en": "Speak at natural speed with casual expressions.", "silence_ms": 700},
    },
    "hints": [
        {"hint_id": "test_cafe.h1", "goal_id": "order_drink", "ko": "마시고 싶은 음료를 주문해 보세요.", "keywords": ["I'd like", "latte", "please"], "example_en": "I'd like a latte, please."},
        {"hint_id": "test_cafe.h2", "goal_id": "change_option", "ko": "사이즈나 우유 종류를 말해 보세요.", "keywords": ["large", "oat milk"], "example_en": "Can I get a large with oat milk?"},
        {"hint_id": "test_cafe.h3", "goal_id": "check_price", "ko": "가격이 얼마인지 물어보세요.", "keywords": ["How much", "total"], "example_en": "How much is that in total?"},
    ],
    "model_expressions": [{"text_id": "test_cafe.m1", "goal_id": "order_drink", "en": "Could I get a latte, please?", "ko": "라테 한 잔 주시겠어요?"}],
    "exercises": {"reading": [], "shadowing": [], "free_answer": []},
}


@pytest.fixture
def scenario():
    return CAFE


@pytest.fixture
async def llm():
    from vr_feedback.llm import LlmClient

    client = LlmClient(LLM_URL, api_key=LLM_API_KEY)
    if not await client.health():
        await client.aclose()
        pytest.skip(f"llama-server not reachable at {LLM_URL}")
    yield client
    await client.aclose()
