"""Golden acceptance test: the pipeline must reproduce the user's manual fix.

Ground truth (verified from the live collection, note for note):

- Broken card 診察 (note 1794599595386): fragment sentence, blob Source,
  no Sentence Audio / Picture / Sentence (furigana).
- Scene parent 至急 (note 1794599541163, 54 seconds earlier), enriched:
  scene audio, picture, timestamped Source, full-scene Sentence with its
  own word bolded (``<b>至急</b>``) and full-scene furigana.
- User's manual fix (診察_transformed, note 1794602006069): Sentence replaced
  by the scene text with the bold MOVED to the child's cloze word
  (``<b>診察</b>``), furigana/media/Source copied verbatim, word fields and
  cloze-* untouched, Expression unchanged.

The fixture (tests/fixtures/golden_scene.json) holds the real field values;
every asserted write must be byte-identical to the manual fix.
"""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path

from mining_fixer.matching import NoteRecord
from mining_fixer.plan import (
    OP_ADD_TAGS,
    OP_EXPAND_SENTENCE,
    OP_FILL,
    OP_REPLACE_SOURCE,
    build_plan,
)

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "golden_scene.json").read_text(encoding="utf-8")
)

PARENT_ID = FIXTURE["parent_note_id"]
CHILD_ID = FIXTURE["child_note_id"]  # 54_223 ms after the parent

AUDIO = "asbp_dr._rintaro_episode_6.mp_1219138_8cdhfa9x.webm"
PICTURE = "asbp_dr._rintaro_episode_6.mp_1219338_aramnlha.jpeg"
TIMESTAMPED_SOURCE = "Dr. Rintaro Episode 6.mp4 (20m19s)"

DEFS = (
    '<div style="text-align: left;" class="yomitan-glossary">'
    "(小学館例解学習国語 第十二版) しんさつ【診察】医者が病人の体を調べること。"
    '.yomitan-glossary [data-dictionary="小学館例解学習国語 第十二版"]</div>'
)

PARENT = NoteRecord(
    note_id=PARENT_ID,
    notetype_id=1,
    fields={
        "Expression": "至急",
        "Definition": DEFS,
        "Source": TIMESTAMPED_SOURCE,
        "Sentence": FIXTURE["parent_sentence"],
        "Sentence (furigana)": FIXTURE["parent_sentence_furigana"],
        "Sentence Audio": f" [sound:{AUDIO}] ",
        "Translation": "",
        "Picture": f' <img src="{PICTURE}"> ',
        "context": "",
        "Notes": "",
        "Word Audio": " [sound:yomitan_audio_7c0fef.ogg] ",
        "Pitch Accent": "しきゅう",
        "furigana": "至急[しきゅう]",
        "reading": "しきゅう",
        "cloze-prefix": "",
        "cloze-body": FIXTURE["parent_cloze_body"],
        "cloze-suffix": " 日野先生と話したいと｡",
        "Frequency": "9375",
        "Extended definition": "しきゅう.yomitan-glossary "
        '[data-dictionary="大辞泉"] gloss text here',
        "Kanji Notes": "",
    },
    tags=frozenset({"yomitan", "asbplayer"}),
)

CHILD = NoteRecord(
    note_id=CHILD_ID,
    notetype_id=1,
    fields={
        "Expression": "診察",
        "Definition": DEFS,
        "Source": FIXTURE["child_blob_source"],
        "Sentence": FIXTURE["child_sentence"],
        "Sentence (furigana)": "",
        "Sentence Audio": "",
        "Translation": "",
        "Picture": "",
        "context": "",
        "Notes": "",
        "Word Audio": " [sound:yomitan_audio_92dc4c.ogg] ",
        "Pitch Accent": "しんさつ",
        "furigana": "診察[しんさつ]",
        "reading": "しんさつ",
        "cloze-prefix": FIXTURE["child_cloze_prefix"],
        "cloze-body": FIXTURE["child_cloze_body"],
        "cloze-suffix": FIXTURE["child_cloze_suffix"],
        "Frequency": "9471",
        "Extended definition": "しんさつ.yomitan-glossary "
        '[data-dictionary="小学館例解学習国語"] gloss',
        "Kanji Notes": "",
    },
    tags=frozenset({"yomitan"}),
)

CFG = {
    "window_days": 3,
    "min_sentence_len": 6,
    "scene_fill_fields": ["Sentence Audio", "Picture", "Sentence (furigana)", "Translation"],
    "scene_replace_fields": ["Source", "Sentence"],
    "scene_optin_fields": ["Notes", "context", "Kanji Notes"],
    "word_fill_fields": [
        "Pitch Accent",
        "Frequency",
        "Word Audio",
        "furigana",
        "reading",
    ],
    "replaces_autocheck": True,
    "translation_autocheck": True,
    "notes_autocheck": False,
    "optout_tag": "no-autofill",
}


# Adversarial shapes from issue #6. The golden fixtures above are all
# pre-Apply and have a parent whose bold marks *its own* word. Both shapes
# below were absent, which is why a fragile fragment matcher looked green.
#  - FOREIGN_BOLD: the sibling bolds 無銭 inside the fragment the child
#    tests with 飲食, and the tested word occurs twice in the block.
#  - POST_PARTIAL_APPLY: the child after a run whose checked fills landed
#    (audio/picture/source) but whose guarded sentence expansion did not.
FOREIGN_BOLD_SIBLING_ID = 10_000_000
FOREIGN_BOLD_CHILD_ID = 10_000_060
FOREIGN_BOLD_AUDIO = " [sound:scene.webm] "
FOREIGN_BOLD_FULL_SENTENCE = (
    "(ドアが開く音)(警官) お待たせしました｡<br>"
    "おかあさんが 人形町の飲食店で<br><b>無銭</b>飲食で逮捕されました｡"
)
FOREIGN_BOLD_PLAIN_SENTENCE = re.sub(r"</?b>", "", FOREIGN_BOLD_FULL_SENTENCE)
FOREIGN_BOLD_FRAGMENT = "無銭<b>飲食</b>で逮捕されました｡"
FOREIGN_BOLD_FRAGMENT_RUBY = "無銭飲食[むせんいんしょく]で 逮捕[たいほ]されました｡"


def foreign_bold_sibling():
    return NoteRecord(
        note_id=FOREIGN_BOLD_SIBLING_ID,
        notetype_id=1,
        fields={
            "Expression": "無銭飲食",
            "reading": "むせんいんしょく",
            "Sentence": FOREIGN_BOLD_FULL_SENTENCE,
            "Sentence Audio": FOREIGN_BOLD_AUDIO,
            "Picture": '<img src="scene.jpeg">',
            "Source": "Show.mp4 (6m53s)",
            "Sentence (furigana)": FOREIGN_BOLD_PLAIN_SENTENCE,
            "cloze-body": "無銭飲食",
        },
        tags=frozenset({"asbplayer"}),
    )


def foreign_bold_child(*, post_partial_apply: bool = False):
    return NoteRecord(
        note_id=FOREIGN_BOLD_CHILD_ID,
        notetype_id=1,
        fields={
            "Expression": "飲食",
            "reading": "いんしょく",
            "Sentence": FOREIGN_BOLD_FRAGMENT,
            "Sentence Audio": FOREIGN_BOLD_AUDIO if post_partial_apply else "",
            "Picture": '<img src="scene.jpeg">' if post_partial_apply else "",
            "Source": "Show.mp4 (6m53s)" if post_partial_apply else "blob:https://asbplayer/xyz",
            "Sentence (furigana)": (FOREIGN_BOLD_FRAGMENT_RUBY if post_partial_apply else ""),
            "cloze-prefix": "無銭",
            "cloze-body": "飲食",
            "cloze-suffix": "で逮捕されました｡",
        },
        tags=frozenset(
            {"asbplayer", "auto-filled", "yomitan"} if post_partial_apply else {"yomitan"}
        ),
    )


def writes_for(plan, note_id):
    return {(w.field, w.op): w for w in plan.writes if w.note_id == note_id}


def test_golden_scene_completion():
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    writes = writes_for(plan, CHILD_ID)

    audio = writes[("Sentence Audio", OP_FILL)]
    assert audio.new_value == f" [sound:{AUDIO}] "
    assert audio.source_note_id == PARENT_ID
    assert audio.default_checked

    picture = writes[("Picture", OP_FILL)]
    assert picture.new_value == f' <img src="{PICTURE}"> '
    assert picture.default_checked

    source = writes[("Source", OP_REPLACE_SOURCE)]
    assert source.old_value == FIXTURE["child_blob_source"]
    assert source.new_value == TIMESTAMPED_SOURCE
    assert source.default_checked
    assert "replaces-blob-url" in source.badges

    sentence = writes[("Sentence", OP_EXPAND_SENTENCE)]
    # BYTE-identical to the user's manual fix, including the bold moved
    # from the parent's word (至急) to the child's cloze word (診察).
    assert sentence.new_value == FIXTURE["manual_sentence"]
    assert sentence.default_checked
    assert "fragment-kept-in-cloze" in sentence.badges
    assert "bold-retargeted-to-cloze-word" in sentence.badges

    furigana = writes[("Sentence (furigana)", OP_FILL)]
    assert furigana.new_value == FIXTURE["manual_sentence_furigana"]
    assert furigana.default_checked

    # The user's tag rule: audit notes carry the tags of the cards they
    # use as sources (診察 must end with asbplayer + yomitan), never less,
    # never extra bookkeeping tags.
    tags = writes[("tags", OP_ADD_TAGS)]
    assert tags.old_value == "yomitan"
    assert tags.new_value == "asbplayer yomitan"
    assert tags.default_checked
    assert "tags-union-never-removes" in tags.badges


def test_golden_fields_never_touched():
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    touched = {(w.field, w.op) for w in plan.writes if w.note_id == CHILD_ID}
    for field_name in (
        "Expression",
        "Definition",
        "Extended definition",
        "Pitch Accent",
        "Frequency",
        "Word Audio",
        "furigana",
        "reading",
        "cloze-prefix",
        "cloze-body",
        "cloze-suffix",
        "Kanji Notes",
    ):
        assert not any(f == field_name for f, _ in touched), field_name
    # The fixture parent HAS scene furigana, so the fill row exists (asserted
    # byte-identical above). When the parent lacks it, no row at all: the
    # user's furigana workflow owns that field.
    bare = replace(PARENT, fields={**PARENT.fields, "Sentence (furigana)": ""})
    plan2 = build_plan([bare, CHILD], CFG, scope_note_ids=[CHILD_ID])
    assert ("Sentence (furigana)", OP_FILL) not in writes_for(plan2, CHILD_ID)


def test_no_fill_when_media_missing():
    def missing(filename):
        return False

    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID], media_exists=missing)
    writes = writes_for(plan, CHILD_ID)
    # audio/picture parents are filtered out entirely -> no scene writes
    assert ("Sentence Audio", OP_FILL) not in writes
    assert ("Picture", OP_FILL) not in writes
