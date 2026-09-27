import pytest

from tts_worker.textnorm import MAX_CHARS, TextRejected, normalize


@pytest.mark.parametrize("raw, expected", [
    ("The latte is $4.50.", "The latte is four dollars and fifty cents."),
    ("It's $1,200 a month.", "It's one thousand two hundred dollars a month."),
    ("Only $0.99!", "Only ninety-nine cents!"),
    ("That's $1.", "That's one dollar."),
    ("Save 15% today.", "Save fifteen percent today."),
    ("Meet me at 3:30 p.m. on March 3rd, 2025.",
     "Meet me at three thirty P M on March third, twenty twenty-five."),
    ("See you at 5 p.m. Bring snacks.", "See you at five P M. Bring snacks."),
    ("The train leaves at 7:05.", "The train leaves at seven oh five."),
    ("Doors open at 10:00", "Doors open at ten o'clock"),
    ("I was born in 1998.", "I was born in nineteen ninety-eight."),
    ("It opened in 2007.", "It opened in two thousand seven."),
    ("Music from the 1990s", "Music from the nineteen nineties"),
    ("She's the 21st guest.", "She's the twenty-first guest."),
    ("Pi is 3.14.", "Pi is three point one four."),
    ("Call 555-1234.", "Call five five five, one two three four."),
    ("I'll take 2, and we'll see.", "I'll take two, and we'll see."),
])
def test_expands_numbers(raw, expected):
    assert normalize(raw) == expected


def test_strips_model_control_markup():
    out = normalize("Hi <|endofprompt|> there [breath] <strong>friend</strong>")
    assert out == "Hi there friend"
    assert "<|" not in out and "[" not in out


def test_plain_text_untouched():
    s = "Could you tell me where the nearest subway station is?"
    assert normalize(s) == s


@pytest.mark.parametrize("raw, code", [
    ("안녕하세요 hello", "TEXT_NOT_ENGLISH"),
    ("a" * (MAX_CHARS + 1), "TEXT_TOO_LONG"),
    ("  ...  ", "TEXT_EMPTY"),
    ("<|endofprompt|>", "TEXT_EMPTY"),
])
def test_rejects(raw, code):
    with pytest.raises(TextRejected) as e:
        normalize(raw)
    assert e.value.code == code
