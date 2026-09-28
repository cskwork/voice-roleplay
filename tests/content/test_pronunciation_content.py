"""Checks for content/pronunciation/guide.json against contracts/pronunciation_content.schema.json and content rules.

Run: uv run --no-project --with jsonschema --with pytest python -m pytest tests/content
"""

import copy
import json
import re
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = ROOT / "contracts" / "pronunciation_content.schema.json"
GUIDE_DIR = ROOT / "content" / "pronunciation"
GUIDE_PATH = GUIDE_DIR / "guide.json"

PINNED_MODEL = ("facebook/wav2vec2-lv-60-espeak-cv-ft", "ae45363bf3413b374fecd9dc8bc1df0e24c3b7f4")
EXPECTED_ENTRIES = {
    "r_l", "f_p", "v_b", "th_s", "dh_d", "z_dzh", "ih_ee", "uh_oo", "ae_e", "uh_ah", "s_sh",
    "schwa", "word_stress", "final_consonants", "clusters",
}
# Vowels a learner may add after a final consonant or inside a cluster (plus ɚ, which espeak writes for
# the inserted syllable in terrain / parade).
INSERTABLE_VOWELS = {"ɨ", "ɯ", "ə", "ɐ", "ᵻ", "ɪ", "i", "ʊ", "u", "ɚ"}
# Wording that would claim the app heard or judged the learner (PRD §8.2). Tips describe articulation only.
JUDGMENT_WORDS = ("들렸", "들었", "인식", "감지", "점수", "정확도", "틀렸", "%")

HANGUL = re.compile(r"[ᄀ-ᇿ㄰-㆏가-힣]")

SCHEMA = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
GUIDE = json.loads(GUIDE_PATH.read_text(encoding="utf-8"))
ENTRIES = GUIDE["entries"]
BY_ID = {e["entry_id"]: e for e in ENTRIES}
SYMBOLS = set(SCHEMA["$defs"]["espeak_symbol"]["enum"])


def text_fields(node, path=""):
    """Yield (path, key, value) for every string; list items inherit their parent key."""
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str):
                yield f"{path}.{key}", key, value
            else:
                for p, k, v in text_fields(value, f"{path}.{key}"):
                    yield p, (key if k is None else k), v
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                yield f"{path}[{i}]", None, item
            else:
                yield from text_fields(item, f"{path}[{i}]")


def validator():
    return Draft202012Validator(SCHEMA)


@pytest.fixture(params=ENTRIES, ids=[e["entry_id"] for e in ENTRIES])
def entry(request):
    return request.param


def test_schema_is_valid_2020_12():
    Draft202012Validator.check_schema(SCHEMA)


def test_guide_validates_against_schema():
    errors = sorted(validator().iter_errors(GUIDE), key=lambda e: list(e.path))
    assert not errors, "\n".join(f"{list(e.path)}: {e.message}" for e in errors)


def test_schema_rejects_bad_documents():
    v = validator()
    assert not v.is_valid({**GUIDE, "unexpected": 1})
    assert not v.is_valid({k: val for k, val in GUIDE.items() if k != "espeak_map"})

    bad_symbol = copy.deepcopy(GUIDE)
    bad_symbol["entries"][0]["minimal_pairs"][0]["a"]["ipa"] = ["ˈɹ", "aɪ", "t"]  # stress mark is not a phone token
    assert not v.is_valid(bad_symbol)

    too_few = copy.deepcopy(GUIDE)
    too_few["entries"][0]["minimal_pairs"] = too_few["entries"][0]["minimal_pairs"][:5]
    assert not v.is_valid(too_few)

    three_sentences = copy.deepcopy(GUIDE)
    s = three_sentences["entries"][0]["practice_sentences"]
    s.append({**s[0], "sentence_id": "r_l_s3"})
    assert not v.is_valid(three_sentences)


def test_expected_topics_present():
    assert set(BY_ID) == EXPECTED_ENTRIES
    assert len(BY_ID) == len(ENTRIES), "duplicate entry_id"


def test_phone_set_is_pinned():
    ps = GUIDE["phone_set"]
    assert (ps["model_repo"], ps["model_revision"]) == PINNED_MODEL


def test_ids_unique_and_prefixed():
    seen = set()
    for e in ENTRIES:
        ids = [p["pair_id"] for p in e["minimal_pairs"]] + [s["sentence_id"] for s in e["practice_sentences"]]
        for item_id in ids:
            assert item_id.startswith(e["entry_id"] + "_"), item_id
            assert item_id not in seen, item_id
            seen.add(item_id)


def test_language_of_fields():
    for path, key, value in text_fields(GUIDE):
        if key and (key == "ko" or key.endswith("_ko")):
            assert HANGUL.search(value), f"Korean field without Hangul: {path}"
        else:
            assert not HANGUL.search(value), f"English / IPA field contains Hangul: {path}"


def test_tips_make_no_judgment_claims(entry):
    for field in ("why_ko", "tip_ko"):
        for word in JUDGMENT_WORDS:
            assert word not in entry[field], f"{entry['entry_id']}.{field} contains {word!r}"


def test_minimal_pairs_are_distinct_words(entry):
    words = set()
    for pair in entry["minimal_pairs"]:
        a, b = pair["a"], pair["b"]
        if entry["category"] in {"consonant", "vowel"} and "label_en" not in a:
            assert a["word"].lower() != b["word"].lower(), pair["pair_id"]
        key = (a["word"].lower(), b["word"].lower())
        assert key not in words, f"repeated pair {key}"
        words.add(key)


def test_segment_pairs_differ_in_one_target_phone(entry):
    if entry["category"] not in {"consonant", "vowel"}:
        return
    targets = set(entry["target_ipa"])
    assert targets, entry["entry_id"]
    for pair in entry["minimal_pairs"]:
        a, b = pair["a"]["ipa"], pair["b"]["ipa"]
        assert len(a) == len(b), pair["pair_id"]
        diff = [(x, y) for x, y in zip(a, b) if x != y]
        assert len(diff) == 1, f"{pair['pair_id']}: {diff}"
        changed = set(diff[0])
        if len(targets) == 2:
            assert changed == targets, f"{pair['pair_id']}: {changed} != {targets}"
        else:
            assert changed & targets, f"{pair['pair_id']}: {changed} misses {targets}"


def test_final_consonant_pairs_add_one_final_vowel():
    for pair in BY_ID["final_consonants"]["minimal_pairs"]:
        a, b = pair["a"]["ipa"], pair["b"]["ipa"]
        assert a[-1] not in INSERTABLE_VOWELS, pair["pair_id"]
        assert b[:-1] == a and b[-1] in INSERTABLE_VOWELS, pair["pair_id"]


def test_cluster_pairs_add_one_vowel_inside_the_cluster():
    for pair in BY_ID["clusters"]["minimal_pairs"]:
        a, b = pair["a"]["ipa"], pair["b"]["ipa"]
        assert a[0] not in INSERTABLE_VOWELS and a[1] not in INSERTABLE_VOWELS, f"{pair['pair_id']}: no cluster"
        assert b == a[:1] + [b[1]] + a[1:] and b[1] in INSERTABLE_VOWELS, pair["pair_id"]


@pytest.mark.parametrize("entry_id", ["schwa", "word_stress"])
def test_stress_pairs_are_noun_verb_of_one_word(entry_id):
    for pair in BY_ID[entry_id]["minimal_pairs"]:
        a, b = pair["a"], pair["b"]
        assert a["word"] == b["word"], pair["pair_id"]
        assert (a.get("label_en"), b.get("label_en")) == ("noun", "verb"), pair["pair_id"]
        assert a["stress_en"] != b["stress_en"], pair["pair_id"]
        for side in (a, b):
            assert side["stress_en"].replace("-", "").lower() == side["word"].lower(), pair["pair_id"]
            assert sum(part.isupper() for part in side["stress_en"].split("-")) == 1, pair["pair_id"]


def test_espeak_map_references_and_coverage():
    keys = [(m["espeak"], m["context"]) for m in GUIDE["espeak_map"]]
    assert len(keys) == len(set(keys)), "duplicate (espeak, context) in espeak_map"

    reached = set()
    for item in GUIDE["espeak_map"]:
        for entry_id in item["entry_ids"]:
            assert entry_id in BY_ID, item
            reached.add(entry_id)
        if item["context"] != "expected":
            assert item["espeak"] in INSERTABLE_VOWELS, item
            assert all(BY_ID[i]["category"] == "syllable" for i in item["entry_ids"]), item
    assert reached == set(BY_ID), f"entries not reachable from espeak_map: {set(BY_ID) - reached}"

    expected = {m["espeak"]: m["entry_ids"] for m in GUIDE["espeak_map"] if m["context"] == "expected"}
    for e in ENTRIES:
        for symbol in e["target_ipa"]:
            assert e["entry_id"] in expected.get(symbol, []), f"{symbol} does not map to {e['entry_id']}"


def test_symbol_list_has_no_stress_or_length_only_tokens():
    for symbol in SYMBOLS:
        assert "ˈ" not in symbol and "ˌ" not in symbol and symbol != "ː", symbol


def test_readme_marks_review_pending():
    readme = (GUIDE_DIR / "README.md").read_text(encoding="utf-8")
    if "pending" in GUIDE["review_status"].values():
        assert "원어민·음성학 검수 필요" in readme
    assert GUIDE["version"] in readme
