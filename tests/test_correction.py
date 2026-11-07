import pytest

from hypnos.correction import detect, similarity

CORRECTIONS_EN = [
    "no, I meant the staging branch",
    "No. Use pnpm instead",
    "that's wrong, it should be pnpm add",
    "I said pnpm, not npm",
    "don't push to main, make a branch",
    "use pnpm instead of npm",
    "actually, run the tests first",
    "why did you run npm?",
    "undo that please",
    "you forgot to run the tests",
    "not npm, use pnpm",
    "we always use conventional commits here",
    "nope",
    "use a conventional commit message, like feat(auth): ...",
    "only delete inside this project",
]

CORRECTIONS_ID = [
    "bukan, maksudnya pakai pnpm",
    "salah, harusnya pnpm add",
    "jangan push ke main",
    "bukan itu, branch yang baru",
    "pakai pnpm aja",
    "seharusnya jalankan test dulu",
    "kan aku bilang pakai pnpm",
    "gak, pakai yarn",
    "sudah saya bilang jangan pakai npm",
    "coba lagi dengan branch baru",
]

NOT_CORRECTIONS = [
    "add lodash to the project",
    "great, now write a test for the parser",
    "no problem, thanks!",
    "tidak apa-apa, lanjut",
    "can you explain how the router works?",
    "tambahkan endpoint baru untuk user",
    "please commit the changes",
    "nope, that's fine",
    "",
]


@pytest.mark.parametrize("text", CORRECTIONS_EN)
def test_english_corrections(text):
    verdict = detect(text)
    assert verdict.is_correction, text
    assert verdict.language == "en"


@pytest.mark.parametrize("text", CORRECTIONS_ID)
def test_indonesian_corrections(text):
    verdict = detect(text)
    assert verdict.is_correction, text
    assert verdict.language == "id"


@pytest.mark.parametrize("text", NOT_CORRECTIONS)
def test_not_corrections(text):
    assert not detect(text).is_correction, text


def test_strong_cues_are_more_confident_than_weak():
    assert detect("no, I meant pnpm").confidence > detect("maybe instead we could add docs").confidence


def test_multiple_cues_raise_confidence():
    assert detect("no, that's wrong, I meant pnpm").confidence > detect("no, pnpm").confidence


def test_reask_detection():
    previous = "add the lodash package to the web app"
    verdict = detect("add the lodash package to the web app please", previous)
    assert verdict.is_reask
    assert verdict.negative
    assert not detect("now write the README", previous).is_reask


def test_similarity_bounds():
    assert similarity("", "x") == 0.0
    assert similarity("pnpm add lodash", "pnpm add lodash") == 1.0
