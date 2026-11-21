"""Tests for plan.py — safety invariants."""

from __future__ import annotations

import json
from pathlib import Path

from test_golden_scene import (
    CFG,
    CHILD,
    CHILD_ID,
    FOREIGN_BOLD_PLAIN_SENTENCE,
    PARENT,
    PARENT_ID,
    foreign_bold_child,
    foreign_bold_sibling,
)

from mining_fixer.matching import NoteRecord
from mining_fixer.normalize import MorphToken, furigana_base_matches, strip_renderable
from mining_fixer.plan import (
    FURIGANA_FIELD,
    OP_ADD_TAGS,
    OP_BOLD_CLOZE_WORD,
    OP_EXPAND_SENTENCE,
    OP_FILL,
    OP_REGENERATE_FURIGANA,
    OP_REPAIR_EXPRESSION,
    OP_REPAIR_SENTENCE,
    OP_REPLACE_SOURCE,
    OP_STRIP_EMPTY_GLOSSARY,
    OPS_TOUCHING_NONEMPTY,
    PLAN_VERSION,
    PlannedWrite,
    _bold_repair_for,
    _furigana_write_for,
    _retarget_bold,
    build_plan,
    scope_from_selection,
)


def rec(nid, fields, tags=()):
    return NoteRecord(note_id=nid, notetype_id=1, fields=fields, tags=frozenset(tags))


BASE = dict(CHILD.fields)


def test_plan_version_and_determinism():
    p1 = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    p2 = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    assert p1.version == PLAN_VERSION
    assert p1.writes == p2.writes
    ids = [(w.note_id, w.field, w.op) for w in p1.writes]
    assert ids == sorted(ids)


def test_never_overwrite_invariant():
    # A fill only ever writes into an empty field (QUALITY Q-D1).
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    for w in plan.writes:
        if w.op == OP_FILL:
            assert w.old_value == "", (w.field, w.new_value)


def test_replaces_only_approved_ops():
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    for w in plan.writes:
        if w.old_value:
            assert w.op in OPS_TOUCHING_NONEMPTY, w.field


def test_expansion_guard_when_fragment_not_recoverable():
    child = dict(CHILD.fields)
    child["cloze-prefix"] = ""  # fragment no longer reconstructible
    child["cloze-body"] = ""
    child["cloze-suffix"] = ""
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    plan = build_plan([PARENT, c], CFG, scope_note_ids=[CHILD.note_id])
    rows = [w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Sentence"]
    assert rows and rows[0].default_checked is False
    assert "fragment-not-recoverable" in rows[0].badges


def test_expansion_uses_the_fragment_when_anchor_not_unique():
    parent_f = dict(PARENT.fields)
    # 診察 occurs three times in the block, but the card's own fragment
    # 「先生は今 診察中ですので➡」 is unique — the highlight is unambiguous.
    parent_f["Sentence"] = "[TEL] 診察の先生は今 診察中ですので➡代わりに診察します。"
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    plan = build_plan([p, CHILD], CFG, scope_note_ids=[CHILD.note_id])
    rows = [w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Sentence"]
    assert rows and rows[0].default_checked
    assert "先生は今 <b>診察</b>中ですので➡" in rows[0].new_value


def test_expansion_is_checked_when_parent_bolds_a_different_word():
    # FOREIGN_BOLD: the tested word occurs twice in the parent block
    # (飲食店 / 無銭飲食) and the sibling bolds 無銭 — 「<b>無銭</b>飲食で…」.
    # Bold is markup, so the cloze fragment must still be found and the
    # expansion must ship checked with the highlight retargeted to 飲食.
    parent = foreign_bold_sibling()
    child = foreign_bold_child()
    plan = build_plan([parent, child], CFG, scope_note_ids=[child.note_id])
    rows = [
        w
        for w in plan.writes
        if w.note_id == child.note_id and w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE
    ]
    assert len(rows) == 1
    assert rows[0].default_checked
    assert "cloze-anchor-not-unique" not in rows[0].badges
    assert "<b>無銭</b>" not in rows[0].new_value
    assert rows[0].new_value == FOREIGN_BOLD_PLAIN_SENTENCE.replace(
        "無銭飲食で逮捕されました｡", "無銭<b>飲食</b>で逮捕されました｡"
    )


def test_skipped_expansion_is_recovered_on_the_next_run():
    # POST_PARTIAL_APPLY: audio, picture, ruby and Source came from the
    # sibling, but the Sentence stayed a fragment. A later run must finish
    # the job instead of reporting nothing-to-fill (QUALITY Q-A2).
    parent = foreign_bold_sibling()
    child = foreign_bold_child(post_partial_apply=True)
    plan = build_plan([parent, child], CFG, scope_note_ids=[child.note_id])
    rows = [
        w
        for w in plan.writes
        if w.note_id == child.note_id and w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE
    ]
    assert len(rows) == 1 and rows[0].default_checked
    expected = FOREIGN_BOLD_PLAIN_SENTENCE.replace(
        "無銭飲食で逮捕されました｡", "無銭<b>飲食</b>で逮捕されました｡"
    )
    assert rows[0].new_value == expected

    # Fixpoint: once the expansion is applied, the next run writes nothing.
    applied = dict(child.fields)
    applied["Sentence"] = expected
    child2 = rec(child.note_id, applied, tags=child.tags)
    plan2 = build_plan([parent, child2], CFG, scope_note_ids=[child.note_id])
    assert not [w for w in plan2.writes if w.note_id == child.note_id and w.field == "Sentence"]


def test_checked_only_apply_keeps_guarded_work_reachable():
    """Q-A11: a guarded row skipped by a run is still owed on the next one.

    Metamorphic property: apply only the checked writes, re-plan, and every
    previously guarded field must still be proposed. The 飲食 incident broke
    exactly this — the checked audio fill destroyed the guarded expansion's
    only trigger, so the next run reported nothing-to-fill.
    """
    fields = dict(CHILD.fields)
    fields["cloze-prefix"] = ""
    fields["cloze-body"] = ""
    fields["cloze-suffix"] = ""
    child = rec(CHILD.note_id, fields, tags=("yomitan",))
    first = build_plan([PARENT, child], CFG, scope_note_ids=[child.note_id])
    checked = [w for w in first.writes if w.note_id == child.note_id and w.default_checked]
    guarded = [w for w in first.writes if w.note_id == child.note_id and not w.default_checked]
    assert checked and guarded

    after = dict(child.fields)
    for write in checked:
        if write.field != "tags":
            after[write.field] = write.new_value
    child2 = rec(child.note_id, after, tags=child.tags)
    second = build_plan([PARENT, child2], CFG, scope_note_ids=[child.note_id])

    for write in guarded:
        if write.field == "tags":
            continue
        assert any(q.field == write.field for q in second.writes if q.note_id == child.note_id), (
            f"guarded {write.field} vanished after only checked writes were applied"
        )


def test_furigana_fill_paired_with_guarded_expansion():
    # The expansion is guarded, so the furigana fill must ship unchecked too:
    # full-scene ruby under a fragment sentence is a broken card.
    child = dict(CHILD.fields)
    child["cloze-prefix"] = ""
    child["cloze-body"] = ""
    child["cloze-suffix"] = ""
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    plan = build_plan([PARENT, c], CFG, scope_note_ids=[CHILD.note_id])
    fills = [
        w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Sentence (furigana)"
    ]
    assert fills and fills[0].op == OP_FILL
    assert fills[0].default_checked is False
    assert "paired-with-expansion" in fills[0].badges


def test_atomic_furigana_replace_when_both_have_it():
    # Child carries fragment ruby; the parent has scene ruby. The replace is
    # atomic with the Sentence expansion and carries its own badge.
    child = dict(CHILD.fields)
    child_furigana = "先生[せんせい]は今 診察[しんさつ]中ですので➡"
    child["Sentence (furigana)"] = child_furigana
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    plan = build_plan([PARENT, c], CFG, scope_note_ids=[CHILD.note_id])
    rows = {
        w.field: w for w in plan.writes if w.note_id == CHILD.note_id and w.op == OP_EXPAND_SENTENCE
    }
    assert set(rows) == {"Sentence", "Sentence (furigana)"}
    furi = rows["Sentence (furigana)"]
    assert furi.old_value == child_furigana
    assert furi.new_value == PARENT.get("Sentence (furigana)")
    assert furi.default_checked
    assert "replaces-fragment-furigana" in furi.badges


def test_stale_furigana_blocks_default_checked():
    # Parent lacks furigana: expanding would leave the child's fragment ruby
    # stale under the scene text -> expansion ships unchecked.
    parent_f = dict(PARENT.fields)
    parent_f["Sentence (furigana)"] = ""
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    child = dict(CHILD.fields)
    child["Sentence (furigana)"] = "先生[せんせい]は今 診察[しんさつ]中ですので➡"  # fragment ruby
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    plan = build_plan([p, c], CFG, scope_note_ids=[CHILD.note_id])
    rows = [w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Sentence"]
    assert rows and rows[0].default_checked is False
    assert "stale-fragment-furigana-risk" in rows[0].badges


def test_scene_furigana_filled_when_parent_has_it():
    # PARENT carries the full-scene ruby (its base text is the parent's
    # sentence); CHILD's ruby is empty, so it is filled with the parent's —
    # byte-identical to the user's manual fix (see test_golden_scene). The
    # ruby must describe the sentence the card ends up with; a value that
    # does not is never copied (test_a_foreign_ruby_is_never_copied...).
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD.note_id])
    fills = [
        w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Sentence (furigana)"
    ]
    assert fills and fills[0].op == OP_FILL and fills[0].default_checked
    assert fills[0].new_value == PARENT.get("Sentence (furigana)")


def test_furigana_generation_uses_the_sentence_that_will_be_written():
    # A guarded (unchecked) expansion never applies, so the generator must be
    # fed the *checked* Sentence write. Feeding it the first Sentence write in
    # the list (a guarded expansion added earlier) builds the ruby for a
    # sentence the card does not end up with, and the ruby then lands checked
    # on a different text (author's 高鳴る: a scene block's ruby on the card's
    # own sentence).
    seen: list[str] = []

    def gen(sentence: str) -> str:
        seen.append(sentence)
        return sentence

    writes = [
        PlannedWrite(
            note_id=CHILD.note_id,
            field="Sentence",
            op=OP_EXPAND_SENTENCE,
            old_value="",
            new_value="[TEL] 別の場面の 文です。",
            default_checked=False,
        ),
        PlannedWrite(
            note_id=CHILD.note_id,
            field="Sentence",
            op=OP_BOLD_CLOZE_WORD,
            old_value=CHILD.get("Sentence"),
            new_value=CHILD.get("Sentence"),
            default_checked=True,
        ),
    ]
    write, _reason, _detail = _furigana_write_for(CHILD, gen, writes)
    assert seen == [CHILD.get("Sentence")]
    assert write is not None and write.default_checked


def test_bold_moves_onto_a_half_width_katakana_spelling():
    # A subtitle dump can carry half-width katakana (ｽﾍﾟｸﾄﾗﾑ) while the mined
    # word and its furigana are full-width (スペクトラム): the bold must still
    # land on the card's own word, in the sentence's own characters (author's
    # 自閉症すぺくとらむ: the bold stayed on このたび from another card).
    child = dict(CHILD.fields)
    child["Expression"] = "自閉症すぺくとらむ"
    child["cloze-body"] = "自閉症スペクトラム"
    child["furigana"] = "自閉症[じへいしょう]すぺくとらむ"
    child["Sentence"] = "深也くんは自閉症ｽﾍﾟｸﾄﾗﾑでありながら<br><b>このたび</b> 受賞しました。"
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    write, _reason, _detail = _bold_repair_for(c)
    assert write is not None and write.default_checked
    assert "<b>自閉症ｽﾍﾟｸﾄﾗﾑ</b>" in write.new_value
    assert write.new_value.count("<b>") == 1
    assert strip_renderable(write.new_value) == strip_renderable(child["Sentence"])


def test_a_bold_write_keeps_the_raw_sentence_as_its_old_value():
    # The bold write also strips a tail arrow. Its old_value must be the field
    # as it stands at apply time (with the arrow), while new_value is the
    # cleaned + bolded text. Using the cleaned text as old_value made the
    # applier reject the row as "changed since preview", so the card was never
    # bolded and every run retried it (author: 11 cards on the newest 300).
    cfg = {**CFG, "sentence_repair_rules": ["strip-arrows"]}
    child = dict(CHILD.fields)
    child["Expression"] = "診察"
    child["cloze-body"] = "診察"
    child["furigana"] = "診察[しんさつ]"
    child["Sentence"] = "先生は今 診察中ですので➡"
    c = rec(CHILD.note_id, child, tags=("yomitan",))
    plan = build_plan([c], cfg, scope_note_ids=[c.note_id])
    rows = [w for w in plan.writes if w.note_id == c.note_id and w.op == OP_BOLD_CLOZE_WORD]
    assert rows
    write = rows[0]
    assert write.old_value == child["Sentence"]  # the field as it stands (➡ included)
    assert "➡" not in write.new_value  # the arrow is cleaned in the same write
    assert "<b>診察</b>" in write.new_value


def test_a_foreign_ruby_is_never_copied_onto_a_different_sentence():
    # A parent's ruby annotates the *parent's* line. When the child's own
    # sentence is a different block, the parent's ruby describes text the
    # child does not show — it must never ship checked (author's どなた,
    # 日取り: a scene fill copied a parent's ruby under another sentence).
    parent_f = dict(PARENT.fields)
    parent_f["Sentence (furigana)"] = (
        "先生[せんせい]は今 診察[しんさつ]中ですので➡代わりに伺[うかが]います"
    )
    assert not furigana_base_matches(parent_f["Sentence (furigana)"], PARENT.get("Sentence"))
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    plan = build_plan([p, CHILD], CFG, scope_note_ids=[CHILD.note_id])
    checked_rubies = [
        w
        for w in plan.writes
        if w.note_id == CHILD.note_id and w.field == "Sentence (furigana)" and w.default_checked
    ]
    assert not checked_rubies


def test_optout_tag_blocks_target():
    child = dict(CHILD.fields)
    c = rec(CHILD.note_id, child, tags=("yomitan", "no-autofill"))
    plan = build_plan([PARENT, c], CFG, scope_note_ids=[CHILD.note_id])
    assert plan.reasons.get(CHILD.note_id) == "opted-out"
    assert not plan.writes


def test_unfixable_reason_reported():
    # A card that is already complete (bold present, nothing empty) is an
    # honest miss with a reason and no writes.
    f = dict(CHILD.fields)
    f["Sentence"] = "先生は今 <b>診察</b>中ですので➡"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert plan.reasons.get(CHILD.note_id) in ("no-parent-that-fills", "nothing-to-fill")
    assert not plan.writes


def test_translation_fill_and_notes_optin():
    parent_f = dict(PARENT.fields)
    parent_f["Translation"] = "The doctor is examining a patient now."
    parent_f["Notes"] = "grammar: ~て form context"
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    plan = build_plan([p, CHILD], CFG, scope_note_ids=[CHILD.note_id])
    by_field = {w.field: w for w in plan.writes if w.note_id == CHILD.note_id}
    assert by_field["Translation"].default_checked
    assert by_field["Notes"].default_checked is False  # opt-in only


def test_retarget_bold_moves_bold_to_child_word():
    scene = "[TEL](ベル)<br><b>至急</b> 日野先生と話したいと｡<br>先生は今 診察中ですので➡"
    value, badge = _retarget_bold(scene, "至急", "診察")
    assert value == "[TEL](ベル)<br>至急 日野先生と話したいと｡<br>先生は今 <b>診察</b>中ですので➡"
    assert badge == "bold-retargeted-to-cloze-word"


def test_retarget_bold_keeps_value_when_bold_on_other_word():
    scene = "彼は<b>天才</b>だ。<br>先生は今 診察中ですので➡"
    value, badge = _retarget_bold(scene, "至急", "診察")
    assert value == scene  # untouched: guessing would be worse than keeping
    assert badge == "stale-parent-bold"


def test_retarget_bold_noop_when_child_already_bold():
    scene = "先生は今 <b>診察</b>中ですので➡"
    value, badge = _retarget_bold(scene, "", "診察")
    assert value == scene
    assert badge is None


def test_expansion_pairing_never_mixes_parents():
    # The whole scene write group carries one source note id; values must
    # all come from the same parent (never mix ruby from a second parent).
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    scene_rows = [
        w
        for w in plan.writes
        if w.note_id == CHILD_ID and w.group_id and w.group_id.startswith("scene-")
    ]
    assert scene_rows
    assert {w.source_note_id for w in scene_rows} == {PARENT_ID}


def test_add_tags_union_from_default_checked_sources():
    # The user's own example: 診察 (yomitan) fed by 至急 (asbplayer+yomitan)
    # must end up with both tags — and nothing removed.
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    rows = [w for w in plan.writes if w.note_id == CHILD_ID and w.op == OP_ADD_TAGS]
    assert len(rows) == 1
    row = rows[0]
    assert row.old_value == "yomitan"
    assert row.new_value == "asbplayer yomitan"
    assert row.default_checked
    assert "tags-union-never-removes" in row.badges
    added = set(row.new_value.split()) - set(row.old_value.split())
    assert added == {"asbplayer"}
    assert set(row.new_value.split()) >= set(row.old_value.split())


def test_add_tags_filters_control_tags():
    # Anki control tags and this tool's own tags never propagate.
    p = rec(
        PARENT.note_id,
        dict(PARENT.fields),
        tags=("asbplayer", "yomitan", "marked", "leech", "no-autofill", "auto-filled"),
    )
    plan = build_plan([p, CHILD], CFG, scope_note_ids=[CHILD.note_id])
    rows = [w for w in plan.writes if w.note_id == CHILD.note_id and w.op == OP_ADD_TAGS]
    assert rows and rows[0].new_value == "asbplayer yomitan"


def test_add_tags_skipped_when_target_already_has_all_tags():
    child = rec(CHILD.note_id, dict(CHILD.fields), tags=("asbplayer", "yomitan"))
    plan = build_plan([PARENT, child], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_ADD_TAGS]


def test_add_tags_needs_default_checked_writes():
    # Sources of UNCHECKED writes contribute nothing: a card the plan does
    # not act on has not earned tag authority. A parent outside the mining
    # session (far-from-source) demotes every row it proposes.
    p = rec(PARENT.note_id + 86_400_000, dict(PARENT.fields), tags=("asbplayer", "yomitan"))
    c = rec(CHILD.note_id, dict(CHILD.fields), tags=("yomitan",))
    plan = build_plan([p, c], CFG, scope_note_ids=[CHILD.note_id])
    rows = [w for w in plan.writes if w.note_id == CHILD.note_id]
    assert rows and all(not w.default_checked for w in rows)
    assert not [w for w in plan.writes if w.op == OP_ADD_TAGS]


def test_bold_repair_exact_tier():
    # The cloze body occurs exactly once, unbolded: wrap it.
    f = dict(CHILD.fields)
    f["Sentence"] = "先生は今 診察中ですので➡"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert bold[0].new_value == "先生は今 <b>診察</b>中ですので➡"
    assert "bold-added-to-cloze-word" in bold[0].badges
    assert bold[0].default_checked
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)


def test_bold_repair_surface_form():
    # 鬱病-shaped: cloze-* hold dictionary-entry text, the sentence spells
    # the leading kanji as kana (うつ病, exactly once) -> inferred tier.
    f = dict(CHILD.fields)
    f["Expression"] = "鬱病"
    f["reading"] = "うつびょう"
    f["cloze-prefix"] = "うつ‐びょう【"
    f["cloze-body"] = "鬱病"
    f["cloze-suffix"] = "】〔―ビヤウ〕"
    f["Sentence"] = "母は うつ病にかかってたんです｡"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert bold[0].new_value == "母は <b>うつ病</b>にかかってたんです｡"
    assert "bold-added-to-surface-form" in bold[0].badges
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)


def test_bold_repair_ambiguous_not_guessed():
    # Two occurrences of the tested word: never guess which to bold.
    f = dict(CHILD.fields)
    f["Sentence"] = "診察の先生は今 診察中ですので➡"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert any("occurs 2 times" in w for w in plan.warnings)


def test_bold_repair_trims_punctuation_on_repeated_word():
    # Real case from Issue #2 (note 1793112599834 とんでもない):
    # The subtitle caret '>' was captured inside the bold: <b>とんでもない></b>.
    # The word occurs twice in the sentence, so placing a new bold would be ambiguous,
    # but trimming the outer non-word delimiter preserves the existing occurrence
    # while restoring exact tag boundaries.
    f = dict(CHILD.fields)
    f["Expression"] = "とんでもない"
    f["reading"] = "とんでもない"
    f["cloze-body"] = "とんでもない"
    f["Sentence"] = "<b>とんでもない></b>大物か,とんでもないバカかのどちらかだな."
    c = rec(1_793_112_599_834, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[c.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert bold[0].new_value == "<b>とんでもない</b>>大物か,とんでもないバカかのどちらかだな."
    assert "bold-trimmed-punctuation" in bold[0].badges
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)

    # Second run is an exact no-op (Q-A2)
    f2 = dict(f)
    f2["Sentence"] = bold[0].new_value
    c2 = rec(c.note_id, f2, tags=("yomitan",))
    plan2 = build_plan([c2], CFG, scope_note_ids=[c2.note_id])
    assert not [w for w in plan2.writes if w.op == OP_BOLD_CLOZE_WORD]


def test_bold_repair_trims_escaped_html_entities():
    # Sibling issue case: <b>芸者&gt;</b> with escaped entity delimiter
    f = dict(CHILD.fields)
    f["Expression"] = "芸者"
    f["reading"] = "げいしゃ"
    f["cloze-body"] = "芸者"
    f["Sentence"] = "それは<b>芸者&gt;</b>というものだ。"
    c = rec(2_000_000, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[c.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert bold[0].new_value == "それは<b>芸者</b>&gt;というものだ。"
    assert "bold-trimmed-punctuation" in bold[0].badges
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)


def test_bold_repair_with_br_markup():
    # Regression: "<b" in sentence matches <br> — the build data is full of
    # <br>, which silently disabled every repair. Bold-tag detection must
    # ignore line-break markup.
    f = dict(CHILD.fields)
    f["Sentence"] = "母は うつ病に<br>かかってたんです｡"
    f["Expression"] = "鬱病"
    f["reading"] = "うつびょう"
    f["cloze-body"] = "鬱病"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert bold[0].new_value == "母は <b>うつ病</b>に<br>かかってたんです｡"


def test_bold_repair_skipped_when_expansion_queued():
    # The golden child's Sentence is queued for a checked scene expansion
    # that already carries the retargeted bold: no markup repair, no noise.
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert not any("no bold in Sentence" in w for w in plan.warnings)


# --- bold from expression (BULK_FIX.md dimension 6, slice shipped part) ------


def test_bold_repair_uses_the_expression_when_the_cloze_body_is_missing():
    # The mined fragment is junk or missing; the Expression itself occurs
    # once in the sentence — bold it (前人未到-shaped card).
    f = dict(CHILD.fields)
    f["Expression"] = "前人未到"
    f["reading"] = "ぜんじんみとう"
    f["furigana"] = ""
    f["cloze-prefix"] = ""
    f["cloze-body"] = ""
    f["cloze-suffix"] = ""
    f["Sentence"] = "ついに 前人未到の世界一周を<br>達成した。"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert "<b>前人未到</b>" in bold[0].new_value
    assert "bold-added-to-expression" in bold[0].badges
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)


def test_bold_repair_refuses_a_clause_like_expression():
    # The invalid card: the Expression field holds a whole clause that also
    # occurs verbatim in the sentence — bolding it would mask the sentence.
    f = dict(CHILD.fields)
    f["Expression"] = "漢字は読めるようになりたいかな"
    f["reading"] = ""
    f["furigana"] = ""
    f["cloze-prefix"] = ""
    f["cloze-body"] = ""
    f["cloze-suffix"] = ""
    f["Sentence"] = "漢字は読めるようになりたいかな<br>漢字読めないの?"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert any("not word-like enough" in w for w in plan.warnings)


def test_bold_repair_accepts_a_long_katakana_word_expression():
    # Long legitimate words (e.g. ベーコンチーズバーガー) are accepted and bolded.
    f = dict(CHILD.fields)
    f["Expression"] = "ベーコンチーズバーガー"
    f["reading"] = ""
    f["furigana"] = ""
    f["cloze-prefix"] = ""
    f["cloze-body"] = ""
    f["cloze-suffix"] = ""
    f["Sentence"] = "ベーコンチーズバーガーをください"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    writes = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(writes) == 1
    assert writes[0].new_value == "<b>ベーコンチーズバーガー</b>をください"


def test_bold_repair_skips_a_listening_tag_card():
    # The template's tag path: listening + usable audio hides the sentence.
    f = dict(CHILD.fields)
    f["Sentence"] = "先生は今 診察中ですので"
    f["Sentence Audio"] = " [sound:scene.webm] "
    c = rec(CHILD.note_id, f, tags=("yomitan", "listening"))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]


def test_bold_repair_skips_the_no_gloss_listening_shape():
    # The template's shape path: no Definition/Extended definition/Frequency
    # and Sentence Audio present -> audio-only front, nothing to bold.
    f = dict(CHILD.fields)
    f["Sentence"] = "先生は今 診察中ですので"
    f["Definition"] = ""
    f["Extended definition"] = ""
    f["Frequency"] = ""
    f["Sentence Audio"] = " [sound:scene.webm] "
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]


def test_frequency_without_glosses_is_not_listening():
    # The template deliberately shows the normal sentence for those cards.
    f = dict(CHILD.fields)
    f["Sentence"] = "先生は今 診察中ですので"
    f["Definition"] = ""
    f["Extended definition"] = ""
    f["Sentence Audio"] = " [sound:scene.webm] "
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1


def test_bold_repair_uses_the_reading_when_the_sentence_is_all_kana():
    # The author's 予て card: expression 予て, reading かねて, sentence
    # 私は かねてより… — the kana spelling must be bolded.
    target = rec(
        1,
        {
            "Expression": "予て",
            "reading": "かねて",
            "furigana": "予[かね]て",
            "cloze-body": "",
            "Sentence": "私は かねてより<br>精神疾患と脳との関係について<br>研究してまいりました。",
        },
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert "<b>かねて</b>" in bold[0].new_value
    assert "bold-added-to-reading-form" in bold[0].badges
    assert strip_renderable(bold[0].new_value) == strip_renderable(bold[0].old_value)


def test_a_short_reading_is_never_bolded():
    # Two-kana readings match too much to bold without guessing.
    target = rec(
        1,
        {
            "Expression": "為る",
            "reading": "する",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "それをするつもりです。",
        },
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]


def test_an_ambiguous_reading_is_reported_not_bolded():
    target = rec(
        1,
        {
            "Expression": "予て",
            "reading": "かねて",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "かねてから かねてより",
        },
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert any("occurs 2 times" in warning for warning in plan.warnings)


def test_two_agreeing_parents_beat_a_divergent_one():
    # The author's 自閉症 ← 自負 case: two parents write byte-identical
    # payloads while a third differs. The agreement is corroborated, so the
    # rows are trusted (they used to ship unchecked and the card stayed
    # unfinished).
    child = scene(10_000_000, "先生は今 診察中ですので", **CHILD_SCENE)
    first = scene(
        10_000_000 + 60_000,
        "先生は今 診察中ですので 次の患者さんが",
        picture='<img src="a.jpeg">',
        audio=" [sound:a.webm] ",
    )
    twin = scene(
        10_000_000 + 61_000,
        "先生は今 診察中ですので 次の患者さんが",
        picture='<img src="a.jpeg">',
        audio=" [sound:a.webm] ",
    )
    other = scene(
        10_000_000 + 120_000,
        "先生は今 診察中ですので お待ちください",
        picture='<img src="b.jpeg">',
        audio=" [sound:b.webm] ",
    )
    plan = build_plan([child, first, twin, other], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows
    assert any(w.default_checked for w in rows), [w.badges for w in rows]
    assert any("2-siblings-agree" in w.badges for w in rows)
    assert any("corroborated-against-divergence" in w.badges for w in rows)
    assert not any("divergent-parents" in w.badges for w in rows)


def test_a_stray_angle_bracket_does_not_block_the_match():
    # Mining leaves <b>芸者&gt;</b>; the parent has 芸者. The matching key
    # ignores angle brackets, so the child still finds its scene (author's
    # 芸者/ハマり/ギャンブル依存症 cards).
    child = rec(
        1,
        {
            "Expression": "芸者",
            "reading": "げいしゃ",
            "furigana": "",
            "Sentence": "夢乃とかいう<b>芸者&gt;</b>じゃないのか?",
            "Sentence Audio": "",
            "Picture": "",
            "Translation": "",
            "Sentence (furigana)": "",
            "Source": "blob:https://asbplayer/xyz",
            "cloze-prefix": "夢乃とかいう",
            "cloze-body": "芸者",
            "cloze-suffix": "じゃないのか?",
        },
    )
    parent = rec(
        2,
        {
            "Expression": "占領",
            "reading": "せんりょう",
            "Sentence": "なぁ 今 お前の心の中占領してんのは誰だ?夢乃とかいう芸者じゃないのか?",
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Translation": "",
            "Sentence (furigana)": "",
            "Source": "Show.mp4 (1m00s)",
            "cloze-prefix": "",
            "cloze-body": "",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([child, parent], CFG, scope_note_ids=[1])
    rows = [w for w in plan.writes if w.note_id == 1 and w.op == OP_EXPAND_SENTENCE]
    assert rows, plan.reasons.get(1)
    assert "芸者" in rows[0].new_value


def test_a_bold_write_cleans_the_arrow_it_would_have_left():
    # 手を出す-shaped: the card needs a bold term AND carries tail arrows.
    # The bold write owns the field, so it must clean what it writes — the
    # arrow cleaner defers to it.
    target = rec(
        1,
        {
            "Expression": "手を出す",
            "reading": "てをだす",
            "furigana": "",
            "cloze-body": "手を出す",
            "Sentence": "女性に手を出すと➡<br>あっという間に症状は<br>悪化します。",
        },
    )
    plan = build_plan(
        [target],
        {**CFG, "sentence_repair_rules": ["strip-arrows"]},
        scope_note_ids=[1],
    )
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert "➡" not in bold[0].new_value
    assert "<b>手を出す</b>" in bold[0].new_value
    assert "strip-arrows" in bold[0].badges

    applied = dict(target.fields)
    applied["Sentence"] = bold[0].new_value
    again = build_plan([rec(1, applied)], CFG, scope_note_ids=[1])
    assert not [w for w in again.writes if w.note_id == 1 and w.field == "Sentence"]


def test_nested_scene_clips_are_trusted():
    # The author's 自閉症 case: two parents hold the same subtitle block, one
    # clipped shorter. That is a clip difference, not a different source, so
    # the tighter pick is trusted.
    child = scene(10_000_000, "先生は今 診察中ですので", **CHILD_SCENE)
    tight = scene(10_000_000 + 60_000, "先生は今 診察中ですので")
    wide = scene(
        10_000_000 + 61_000,
        "それでは 先生は今 診察中ですので お待ちください",
        picture='<img src="b.jpeg">',
        audio=" [sound:b.webm] ",
    )
    plan = build_plan([child, tight, wide], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows
    assert any(w.default_checked for w in rows), [w.badges for w in rows]
    badges = {badge for w in rows for badge in w.badges}
    assert badges & {"same-scene-tighter-clip", "corroborated-against-divergence"}
    assert "divergent-parents" not in badges


def test_bold_repair_uses_the_furigana_spellings_of_the_word():
    # 類い希's sentence spells it 類いまれ (kanji kept, rest in kana): the
    # note's own furigana gives that spelling.
    target = rec(
        1,
        {
            "Expression": "類い希",
            "reading": "たぐいまれ",
            "furigana": "類[たぐ]い 希[まれ]",
            "cloze-body": "類い希",
            "Sentence": "その立体造形に<br>類いまれなる才能を持ち<br>グランプリを受賞されました。",
        },
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert len(bold) == 1
    assert "<b>類いまれ</b>" in bold[0].new_value
    assert "bold-added-to-kana-mix" in bold[0].badges


def test_a_full_kana_sentence_keeps_the_reading_badge():
    # 予て -> かねて is the reading tier's case (no kanji in the matched span).
    target = rec(
        1,
        {
            "Expression": "予て",
            "reading": "かねて",
            "furigana": "予[かね]て",
            "cloze-body": "",
            "Sentence": "私は かねてより<br>研究してまいりました。",
        },
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    bold = [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]
    assert bold and "bold-added-to-reading-form" in bold[0].badges


def test_a_stale_fragment_ruby_is_regenerated_when_the_engine_is_available():
    # The scene parent carries no ruby, so completing the card would leave its
    # fragment ruby under the full scene text. When the furigana tier runs
    # with a working engine, it rebuilds the ruby from the completed sentence
    # in the same plan, so the completion ships *checked*
    # (`ruby-will-be-regenerated`) instead of being demoted.
    child = rec(
        1,
        {
            **_ZHENGDAO_CHILD,
            "Sentence (furigana)": "兄[あに](オレ)が 正道[せいどう]を 歩[あゆ]んだのなら",
        },
    )
    parent = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": "兄(オレ)が正道を歩んだのなら 弟は 後をついてくればいい",
            "Sentence Audio": " [sound:asbp_x.webm] ",
            "Picture": '<img src="asbp_x.jpeg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "Sentence (furigana)": "",
            "cloze-prefix": "",
            "cloze-body": "出来",
            "cloze-suffix": "",
        },
        tags=("asbplayer",),
    )

    def gen(sentence: str) -> str:
        return sentence  # base text == sentence, so the Q-D11 guard passes

    regen_cfg = {**CFG, "batch_regenerate_furigana": True}
    plan = build_plan([child, parent], regen_cfg, scope_note_ids=[1], furigana_generator=gen)
    expansion = next(w for w in plan.writes if w.note_id == 1 and w.op == OP_EXPAND_SENTENCE)
    assert expansion.default_checked
    assert "ruby-will-be-regenerated" in expansion.badges
    assert any(w.note_id == 1 and w.op == OP_REGENERATE_FURIGANA for w in plan.writes)


def test_a_completion_is_demoted_when_the_ruby_engine_returns_nothing():
    # The engine is present (so the completion ships expecting a rebuilt
    # ruby) but produces nothing: the card would keep its fragment ruby under
    # the full scene text. The post-check demotes the completion instead of
    # writing a broken pair.
    child = rec(
        1,
        {
            **_ZHENGDAO_CHILD,
            "Sentence (furigana)": "兄[あに](オレ)が 正道[せいどう]を 歩[あゆ]んだのなら",
        },
    )
    parent = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": "兄(オレ)が正道を歩んだのなら 弟は 後をついてくればいい",
            "Sentence Audio": " [sound:asbp_x.webm] ",
            "Picture": '<img src="asbp_x.jpeg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "Sentence (furigana)": "",
            "cloze-prefix": "",
            "cloze-body": "出来",
            "cloze-suffix": "",
        },
        tags=("asbplayer",),
    )
    regen_cfg = {**CFG, "batch_regenerate_furigana": True}
    plan = build_plan(
        [child, parent], regen_cfg, scope_note_ids=[1], furigana_generator=lambda _s: ""
    )
    expansion = next(w for w in plan.writes if w.note_id == 1 and w.op == OP_EXPAND_SENTENCE)
    assert not expansion.default_checked
    assert "stale-fragment-furigana-risk" in expansion.badges


def test_a_completed_sentence_is_never_expanded_again():
    # Convergence: a card that already carries its own Sentence Audio is a
    # scene unit in its own right — its audio matches its own sentence — so no
    # longer neighbouring block is forced onto it (and run two stays a no-op).
    child = rec(
        1,
        {
            "Expression": "校",
            "reading": "こう",
            "furigana": "校[こう]",
            "Sentence": "それでは わが<b>校</b>での研究に興味があれば",
            "Sentence Audio": " [sound:x.webm] ",
            "Picture": '<img src="x.jpg">',
            "Source": "Show.mp4 (10m00s)",
            "cloze-prefix": "わが",
            "cloze-body": "校",
            "cloze-suffix": "での研究に興味があれば",
        },
    )
    parent = scene(10_000_000 + 60_000, "それでは わが校での研究に興味があれば ぜひ ご紹介ください")
    plan = build_plan([child, parent], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.note_id == 1 and w.op == OP_EXPAND_SENTENCE]


def test_a_mismined_sentence_is_recovered_from_the_word_in_the_same_episode():
    # The author's 類い希 card: its Sentence is the wrong subtitle block, so it
    # matches no parent, yet the word (written 類いまれ) is in the scene the
    # same episode mined elsewhere. Recovery anchors on the word, uses the
    # tighter clip of two nested blocks, and bolds the word it found.
    target = rec(
        1,
        {
            "Expression": "類い希",
            "reading": "たぐいまれ",
            "furigana": "類[たぐ]い 希[まれ]",
            "Sentence": "関係のない文です。",
            "Sentence Audio": "",
            "Picture": "",
            "Sentence (furigana)": "",
            "Source": "Show.mp4 (15m01s)",
            "cloze-prefix": "たぐいまれ【類いまれ】[",
            "cloze-body": "類い希",
            "cloze-suffix": "] 形動だな",
        },
    )
    wide = rec(
        2,
        {
            "Expression": "診断",
            "reading": "しんだん",
            "Sentence": "今回 対象としたい患者は この方。<br>深也くんは自閉症でありながら"
            "<br>その立体造形に<br>類いまれなる才能を持ち<br><b>診断</b>されます。",
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Sentence (furigana)": "",
            "Source": "Show.mp4 (15m20s)",
            "cloze-prefix": "",
            "cloze-body": "診断",
            "cloze-suffix": "",
        },
    )
    tight = rec(
        3,
        {
            "Expression": "造形",
            "reading": "ぞうけい",
            "Sentence": "その立体造形に<br>類いまれなる才能を持ち",
            "Sentence Audio": " [sound:b.webm] ",
            "Picture": '<img src="b.jpg">',
            "Sentence (furigana)": "",
            "Source": "Show.mp4 (15m10s)",
            "cloze-prefix": "",
            "cloze-body": "造形",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([target, wide, tight], CFG, scope_note_ids=[1])
    rows = [w for w in plan.writes if w.note_id == 1]
    recovered = [w for w in rows if w.op == OP_EXPAND_SENTENCE and "recovered-sentence" in w.badges]
    assert recovered, plan.reasons.get(1)
    assert "<b>類いまれ</b>" in recovered[0].new_value
    assert recovered[0].default_checked is True
    assert "word-in-sentence" in recovered[0].badges


def test_word_recovery_accepts_a_blob_source_when_the_candidates_agree_on_one_episode():
    # The author's 此の度: no episode in its own Source, but every card that
    # contains このたび is from the same episode — so the scene is adopted.
    target = rec(
        1,
        {
            "Expression": "此の度",
            "reading": "このたび",
            "furigana": "此[こ]の 度[たび]",
            "Sentence": "この‐たび【×此の度】",
            "Sentence Audio": "",
            "Picture": "",
            "Source": "asbplayer blob:https://x",
            "cloze-prefix": "この‐たび【×",
            "cloze-body": "此の度",
            "cloze-suffix": "】",
        },
    )
    other = rec(
        2,
        {
            "Expression": "美術",
            "reading": "びじゅつ",
            "Sentence": "このたび こども美術展において",
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Source": "Show.mp4 (10m00s)",
            "cloze-prefix": "",
            "cloze-body": "美術",
            "cloze-suffix": "",
        },
    )
    third = rec(
        3,
        {
            "Expression": "受賞",
            "reading": "じゅしょう",
            "Sentence": "このたび こども美術展において グランプリを受賞",
            "Sentence Audio": " [sound:b.webm] ",
            "Picture": '<img src="b.jpg">',
            "Source": "Show.mp4 (10m05s)",
            "cloze-prefix": "",
            "cloze-body": "受賞",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([target, other, third], CFG, scope_note_ids=[1])
    recovered = [w for w in plan.writes if "recovered-sentence" in w.badges]
    assert recovered, plan.reasons.get(1)
    sentence = next(w for w in recovered if w.field == "Sentence")
    assert "<b>このたび</b>" in sentence.new_value
    assert any(w.field == "Picture" for w in recovered)


def test_word_recovery_refuses_candidates_from_different_episodes():
    # The same word in two different shows must not pick a scene: the
    # candidates disagree on the episode, so nothing is recovered.
    target = rec(
        1,
        {
            "Expression": "此の度",
            "reading": "このたび",
            "furigana": "此[こ]の 度[たび]",
            "Sentence": "この‐たび【×此の度】",
            "Sentence Audio": "",
            "Picture": "",
            "Source": "asbplayer blob:https://x",
            "cloze-prefix": "この‐たび【×",
            "cloze-body": "此の度",
            "cloze-suffix": "】",
        },
    )
    first = rec(
        2,
        {
            "Expression": "甲",
            "reading": "こう",
            "Sentence": "このたび こども美術展において",
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Source": "Show A.mp4 (10m00s)",
            "cloze-prefix": "",
            "cloze-body": "甲",
            "cloze-suffix": "",
        },
    )
    second = rec(
        3,
        {
            "Expression": "乙",
            "reading": "おつ",
            "Sentence": "このたび お招きいただきまして",
            "Sentence Audio": " [sound:b.webm] ",
            "Picture": '<img src="b.jpg">',
            "Source": "Show B.mp4 (10m00s)",
            "cloze-prefix": "",
            "cloze-body": "乙",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([target, first, second], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if "recovered-sentence" in w.badges]


def test_word_recovery_refuses_two_different_scenes():
    # Same episode, same word, genuinely different sentences: no recovery.
    target = rec(
        1,
        {
            "Expression": "類い希",
            "reading": "たぐいまれ",
            "furigana": "類[たぐ]い 希[まれ]",
            "Sentence": "関係のない文です。",
            "Sentence Audio": "",
            "Picture": "",
            "Source": "Show.mp4 (15m01s)",
            "cloze-prefix": "",
            "cloze-body": "類い希",
            "cloze-suffix": "",
        },
    )
    first = rec(
        2,
        {
            "Expression": "甲",
            "reading": "こう",
            "Sentence": "類いまれなる 才能があります。",
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Source": "Show.mp4 (15m20s)",
            "cloze-prefix": "",
            "cloze-body": "甲",
            "cloze-suffix": "",
        },
    )
    second = rec(
        3,
        {
            "Expression": "乙",
            "reading": "おつ",
            "Sentence": "まったく別の話題で 類いまれな 話をした。",
            "Sentence Audio": " [sound:b.webm] ",
            "Picture": '<img src="b.jpg">',
            "Source": "Show.mp4 (15m30s)",
            "cloze-prefix": "",
            "cloze-body": "乙",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([target, first, second], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if "recovered-sentence" in w.badges], (
        "different scenes must never be adopted"
    )


def test_a_short_fragment_still_finds_its_parent():
    # 親御-shaped: a five-character fragment. The length floor belongs to the
    # parent's scene sentence, not to the fragment.
    child = scene(
        10_000_000,
        "親御さんは",
        expr="親御",
        reading="おやご",
        body="親御",
        audio="",
        picture="",
        source="blob:https://asbplayer/xyz",
    )
    parent = scene(
        10_000_000 + 60_000,
        "とにかく 親御さんは<br>心配でたまらないようです。<br>もっと信じてあげてください。",
    )
    plan = build_plan([child, parent], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows, plan.reasons.get(child.note_id)


def test_scope_from_selection_preserves_order_and_dedupes():
    # The Browser selection is a list of note ids; its order is the user's.
    scope, unknown = scope_from_selection([3, 1, 3, 9], {1, 2, 3})
    assert scope == [3, 1]
    assert unknown == [9]


def test_scope_from_selection_reports_every_unknown_id():
    scope, unknown = scope_from_selection([9, 8], set())
    assert scope == []
    assert unknown == [9, 8]


def test_explicit_scope_limits_targets_but_not_sources():
    # What the Browser selection relies on: only the selected notes are
    # targets, while every record stays eligible as a parent/word source.
    plan = build_plan([PARENT, CHILD], CFG, scope_note_ids=[CHILD_ID])
    assert {w.note_id for w in plan.writes} == {CHILD_ID}
    assert PARENT_ID not in plan.reasons


# --- source confidence (wrong-source protection) ---------------------


def scene(
    nid,
    sentence,
    *,
    expr="至急",
    reading="しきゅう",
    body="至急",
    audio=" [sound:asbp.webm] ",
    picture='<img src="asbp.jpeg">',
    translation="",
    source="Show Episode 1.mp4 (1m00s)",
    furigana="",
):
    return rec(
        nid,
        {
            "Expression": expr,
            "reading": reading,
            "Sentence": sentence,
            "Sentence Audio": audio,
            "Picture": picture,
            "Translation": translation,
            "Source": source,
            "Sentence (furigana)": furigana,
            "cloze-prefix": "先生は今 ",
            "cloze-body": body,
            "cloze-suffix": "中ですので",
        },
        tags=("asbplayer",),
    )


CHILD_SCENE = dict(
    expr="診察",
    reading="しんさつ",
    body="診察",
    audio="",
    picture="",
    source="blob:https://asbplayer/xyz",
)


def test_divergent_scene_sources_are_never_auto_checked():
    # Two unrelated parents both contain the fragment but would write
    # different payloads: picking either silently risks an unrelated card.
    child = scene(10_000_000, "先生は今 診察中ですので", **CHILD_SCENE)
    first = scene(
        10_000_000 + 60_000,
        "先生は今 診察中ですので 次の患者さんが",
        picture='<img src="a.jpeg">',
        audio=" [sound:a.webm] ",
    )
    second = scene(
        10_000_000 + 120_000,
        "先生は今 診察中ですので お待ちください",
        picture='<img src="b.jpeg">',
        audio=" [sound:b.webm] ",
    )
    plan = build_plan([child, first, second], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows
    scene_rows = [w for w in rows if w.field != "Sentence"]
    assert not any(w.default_checked for w in scene_rows), [w.badges for w in scene_rows]
    assert any("divergent-parents" in w.badges for w in rows)
    assert any("scene sources disagree" in warning for warning in plan.warnings)


def test_a_far_scene_source_is_left_unchecked():
    # Within the ±3-day search window, but far outside a mining session: the
    # match is suggested, never trusted automatically.
    child = scene(0, "先生は今 診察中ですので", **CHILD_SCENE)
    far = scene(30 * 3_600_000, "先生は今 診察中ですので 次の患者さん")
    plan = build_plan([child, far], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows
    scene_rows = [w for w in rows if w.field != "Sentence"]
    assert not any(w.default_checked for w in scene_rows)
    assert any("far-from-source" in w.badges for w in rows)


def test_an_identical_sentence_is_trusted_even_when_far():
    # A byte-identical sentence verifies the source by itself.
    sentence = "先生は今 診察中ですので"
    child = scene(0, sentence, **CHILD_SCENE)
    twin = scene(30 * 3_600_000, sentence)
    plan = build_plan([child, twin], CFG, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id]
    assert rows
    assert all(w.default_checked for w in rows), [(w.field, w.badges) for w in rows]


# --- subtitle-noise repair (bulk-fix dimension 2, QUALITY Q-D10) ------------

REPAIR_CFG = {**CFG, "sentence_repair_rules": ["strip-arrows"]}
STRIP_DASH_CFG = {**CFG, "sentence_repair_rules": ["strip-dash"]}


def test_a_configured_repair_emits_a_checked_write_with_rule_badges():
    target = rec(
        1,
        {
            "Expression": "見出し",
            "reading": "みだし",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "あしたの朝刊の<b>見出し</b>は<br>こうニャ！➡",
        },
    )
    plan = build_plan([target], REPAIR_CFG, scope_note_ids=[1])
    repair_writes = [w for w in plan.writes if w.op == OP_REPAIR_SENTENCE]
    assert len(repair_writes) == 1
    write = repair_writes[0]
    assert write.op == OP_REPAIR_SENTENCE
    assert write.field == "Sentence"
    assert write.new_value == "あしたの朝刊の<b>見出し</b>は<br>こうニャ！"
    assert write.old_value == "あしたの朝刊の<b>見出し</b>は<br>こうニャ！➡"
    assert write.default_checked is True
    assert write.badges == ("strip-arrows",)


def test_repair_is_off_without_rules():
    target = rec(
        1,
        {"Expression": "", "reading": "", "furigana": "", "cloze-body": "", "Sentence": "前<br>後"},
    )
    plan = build_plan([target], CFG, scope_note_ids=[1])
    assert not any(w.op == OP_REPAIR_SENTENCE for w in plan.writes)


def test_repair_skips_a_note_whose_expansion_is_queued():
    # The fragment "見<br>出しはこうニャ！➡" would be repaired — but the scene
    # expansion already owns the field, so the noise repair stays out.
    parent = scene(9, "あしたの朝刊の見出しはこうニャ！")
    child = rec(
        1,
        {
            **scene(
                1,
                "見<br>出しはこうニャ！➡",
                expr="見出し",
                reading="みだし",
                body="見出し",
                audio="",
                picture="",
                source="asbplayer://blob",
            ).fields,
            "cloze-prefix": "",
            "cloze-body": "見出し",
            "cloze-suffix": "はこうニャ！",
        },
    )
    plan = build_plan([parent, child], REPAIR_CFG, scope_note_ids=[child.note_id])
    assert any(w.op == OP_EXPAND_SENTENCE and w.field == "Sentence" for w in plan.writes), [
        (w.op, w.field) for w in plan.writes
    ]
    assert not any(w.op == OP_REPAIR_SENTENCE for w in plan.writes)


def test_repair_skips_a_note_whose_bold_repair_is_queued():
    # Bold repair owns the field first; the noise repair must not fight it.
    # (The tail arrow would be repairable — that is the point of the test.)
    target = scene(
        1,
        "今のままじゃ<br>あいつらに顔向けできねえよ➡",
        expr="顔向け",
        reading="かおむけ",
        body="顔向け",
        audio="",
        picture="",
    )
    plan = build_plan([target], REPAIR_CFG, scope_note_ids=[target.note_id])
    sentence_writes = [w for w in plan.writes if w.field == "Sentence"]
    assert [w.op for w in sentence_writes] == [OP_BOLD_CLOZE_WORD]


def test_repair_is_dropped_when_the_tested_word_occurrence_would_change():
    # Under arrows-only rules the repair can change the tested word's
    # occurrence count only when that word is itself an arrow glyph
    # (synthetic by necessity — real arrows are never the mined word). The
    # guard stays as defense-in-depth for future rules (Q-D10).
    target = rec(
        1,
        {
            "Expression": "",
            "reading": "",
            "furigana": "",
            "Sentence": "見出し<b>➡</b>➡",
            "cloze-body": "➡",
        },
    )
    plan = build_plan([target], REPAIR_CFG, scope_note_ids=[1])
    assert not any(w.op == OP_REPAIR_SENTENCE for w in plan.writes)
    assert any("sentence repair skipped" in warning for warning in plan.warnings)


def test_unknown_repair_rules_warn_and_the_rest_still_runs():
    target = rec(
        1,
        {
            "Expression": "",
            "reading": "",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "前➡<br>後",
        },
    )
    cfg = {**CFG, "sentence_repair_rules": ["strip-arrows", "nonsense"]}
    plan = build_plan([target], cfg, scope_note_ids=[1])
    assert any("unknown sentence_repair_rules ignored: nonsense" in w for w in plan.warnings)
    assert [w.op for w in plan.writes if w.op == OP_REPAIR_SENTENCE] == [OP_REPAIR_SENTENCE]
    write = next(w for w in plan.writes if w.op == OP_REPAIR_SENTENCE)
    assert write.new_value == "前<br>後"


def test_a_stray_dash_is_stripped_from_the_sentence_field_only():
    # Owner report 2026-11-20: the Mining cards' Sentences carried the stray
    # horizontal bar (U+2015) — the extension strips it in the Sentence field
    # only: the ruby ("Sentence (furigana)") and the Definition are untouched
    # ("only inside the sentence field, not everywhere").
    sentence = "をつくった\u2015<br>チームを組んだ"
    target = rec(
        1,
        {
            "Expression": "",
            "reading": "",
            "furigana": "",
            "cloze-body": "",
            "Sentence": sentence,
            "Sentence (furigana)": "その 組み従った\u2015はち",
            "Definition": "組んだ\u2015 — the Definition keeps its bar",
        },
    )
    plan = build_plan([target], STRIP_DASH_CFG, scope_note_ids=[1])
    writes = [w for w in plan.writes if w.op == OP_REPAIR_SENTENCE]
    assert len(writes) == 1
    write = writes[0]
    assert write.field == "Sentence"
    assert write.default_checked is True
    assert write.badges == ("strip-dash",)
    assert write.old_value == sentence
    assert write.new_value == "をつくった<br>チームを組んだ"
    assert not any(w.field == FURIGANA_FIELD for w in plan.writes)
    assert not any(w.field == "Definition" for w in plan.writes)
    assert not any(w.op == OP_BOLD_CLOZE_WORD for w in plan.writes)


def test_strip_dash_is_on_by_default_in_the_shipped_config():
    # The extension handles the owner's report on its own: the shipped
    # config.json ships the rule (automatic mode — every dimension on).
    cfg = json.loads(
        (Path(__file__).resolve().parent.parent / "mining_fixer" / "config.json").read_text()
    )
    assert "strip-dash" in cfg["sentence_repair_rules"]


# --- furigana regeneration (bulk-fix dimension 3, QUALITY Q-D11) -------------

REGEN_CFG = {**CFG, "batch_regenerate_furigana": True}


def _regen_card(nid, sentence, furigana=""):
    return rec(
        nid,
        {
            "Expression": "",
            "reading": "",
            "furigana": "",
            "Sentence": sentence,
            "Sentence (furigana)": furigana,
        },
    )


def _generator(value, calls=None):
    def generate(sentence):
        if calls is not None:
            calls.append(sentence)
        return value

    return generate


def test_regeneration_emits_a_checked_write_with_badges():
    target = _regen_card(1, "見出しはこうニャ！", furigana="見出しはこうニャ！")
    plan = build_plan(
        [target],
        REGEN_CFG,
        scope_note_ids=[1],
        furigana_generator=_generator("見出[みだ]しはこうニャ！"),
    )
    writes = [w for w in plan.writes if w.op == OP_REGENERATE_FURIGANA]
    assert len(writes) == 1
    write = writes[0]
    assert write.field == FURIGANA_FIELD
    assert write.old_value == "見出しはこうニャ！"
    assert write.new_value == "見出[みだ]しはこうニャ！"
    assert write.default_checked is True
    assert write.badges == ("furigana-regenerated",)


def test_regeneration_is_off_by_default():
    target = _regen_card(1, "見出しはこうニャ！", furigana="見出しはこうニャ！")
    plan = build_plan(
        [target],
        CFG,
        scope_note_ids=[1],
        furigana_generator=_generator("見出[みだ]しはこうニャ！"),
    )
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)
    assert not any("furigana" in warning for warning in plan.warnings)


def test_regeneration_is_a_noop_when_the_ruby_already_matches():
    target = _regen_card(1, "見出しはこうニャ！", furigana="見出[みだ]しはこうニャ！")
    plan = build_plan(
        [target],
        REGEN_CFG,
        scope_note_ids=[1],
        furigana_generator=_generator("見出[みだ]しはこうニャ！"),
    )
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)
    assert not any("furigana" in warning for warning in plan.warnings)


def test_regeneration_warns_once_when_no_generator_is_available():
    plan = build_plan(
        [_regen_card(1, "見出しはこうニャ！"), _regen_card(2, "見出しはこうニャ！")],
        REGEN_CFG,
        scope_note_ids=[1, 2],
    )
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)
    assert sum("no furigana generator" in w for w in plan.warnings) == 1


def test_regeneration_runs_on_the_sentence_the_plan_leaves_behind():
    # The repair removes the tail arrow; the ruby must describe THAT text
    # (BULK_FIX.md: sentence repair precedes furigana generation).
    calls: list[str] = []
    target = _regen_card(1, "見出しはこうニャ！➡", furigana="見出しはこうニャ！")
    cfg = {**REGEN_CFG, "sentence_repair_rules": ["strip-arrows"]}
    plan = build_plan(
        [target],
        cfg,
        scope_note_ids=[1],
        furigana_generator=_generator("見出[みだ]しはこうニャ！", calls=calls),
    )
    assert calls == ["見出しはこうニャ！"]
    assert any(w.op == OP_REPAIR_SENTENCE for w in plan.writes)
    write = next(w for w in plan.writes if w.op == OP_REGENERATE_FURIGANA)
    assert write.badges == ("furigana-regenerated", "after-sentence-write")


def test_regeneration_defers_to_a_queued_scene_fill_of_the_same_field():
    # The scene fill already owns Sentence (furigana) for this child.
    parent = scene(
        9,
        "あしたの朝刊の見出しはこうニャ！",
        furigana="あしたの朝刊の見出し[みだし]はこうニャ！",
    )
    child = rec(
        1,
        {
            **scene(
                1,
                "見<br>出しはこうニャ！➡",
                expr="見出し",
                reading="みだし",
                body="見出し",
                audio="",
                picture="",
                source="asbplayer://blob",
            ).fields,
            "cloze-prefix": "",
            "cloze-body": "見出し",
            "cloze-suffix": "はこうニャ！",
        },
    )
    plan = build_plan(
        [parent, child],
        REGEN_CFG,
        scope_note_ids=[child.note_id],
        furigana_generator=_generator("見出[みだ]しはこうニャ！"),
    )
    assert any(w.op == OP_FILL and w.field == FURIGANA_FIELD for w in plan.writes)
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)


def test_regeneration_is_dropped_when_the_engine_mangles_the_text():
    target = _regen_card(1, "見出しはこうニャ！", furigana="old")
    plan = build_plan(
        [target],
        REGEN_CFG,
        scope_note_ids=[1],
        furigana_generator=_generator("全然違うテキスト"),
    )
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)
    assert any("furigana regeneration skipped" in warning for warning in plan.warnings)


def test_regeneration_is_dropped_when_the_engine_raises():
    def boom(_sentence):
        raise RuntimeError("mecab is not running")

    target = _regen_card(1, "見出しはこうニャ！", furigana="old")
    plan = build_plan([target], REGEN_CFG, scope_note_ids=[1], furigana_generator=boom)
    assert not any(w.op == OP_REGENERATE_FURIGANA for w in plan.writes)
    assert any("reading engine raised" in warning for warning in plan.warnings)


# --- empty-definition cleanup (QUALITY Q-D12, owner ask 2026-11-16) ----------

JUNK_SHELL = '<div style="text-align: left;" class="yomitan-glossary"><ol></ol></div>'


def test_expansion_cleans_the_copied_sentence_in_the_same_write():
    # One run, one fix: the parent's dump may carry a tail arrow, so the
    # expansion cleans what it writes. A second run must find nothing.
    cfg = {**CFG, "sentence_repair_rules": ["strip-arrows"]}
    child = scene(10_000_000, "先生は今 診察中ですので", **CHILD_SCENE)
    parent = scene(10_000_000 + 60_000, "それでは 先生は今 診察中ですので お待ちください➡")
    plan = build_plan([child, parent], cfg, scope_note_ids=[child.note_id])
    rows = [w for w in plan.writes if w.note_id == child.note_id and w.op == OP_EXPAND_SENTENCE]
    assert rows
    assert "➡" not in rows[0].new_value
    assert "expanded-strip-arrows" in rows[0].badges

    applied = dict(child.fields)
    applied["Sentence"] = rows[0].new_value
    again = build_plan(
        [rec(child.note_id, applied, tags=("asbplayer",)), parent],
        cfg,
        scope_note_ids=[child.note_id],
    )
    assert not [
        w for w in again.writes if w.note_id == child.note_id and w.op == OP_EXPAND_SENTENCE
    ]


def test_a_shell_definition_is_cleared_never_filled_from_sibling():
    # Definitions are unique to each card (FIELD_NEVER_COPY): the add-on never
    # copies definitions from sibling cards. An empty glossary shell is
    # cleared (OP_STRIP_EMPTY_GLOSSARY), never filled from a sibling.
    shell = '<div class="yomitan-glossary"><ol></ol></div>'
    good = '<div class="yomitan-glossary"><ol><li>いっぱく【一白】しろい着物</li></ol></div>'
    target = rec(
        1,
        {
            **scene(1, "先生は今 診察中ですので", body="一白").fields,
            "Definition": shell,
            "cloze-body": "一白",
        },
    )
    sibling = rec(
        2,
        {
            **scene(2, "先生は今 診察中ですので", body="一白").fields,
            "Definition": good,
            "cloze-body": "一白",
        },
        tags=("yomitan",),
    )
    cfg = {**CFG, "definition_fields": ["Definition", "Extended definition"]}
    plan = build_plan([target, sibling], cfg, scope_note_ids=[1])
    fills = [
        w for w in plan.writes if w.note_id == 1 and w.op == OP_FILL and w.field == "Definition"
    ]
    assert fills == []  # Definition is NEVER copied from siblings
    clears = [
        w
        for w in plan.writes
        if w.note_id == 1 and w.op == OP_STRIP_EMPTY_GLOSSARY and w.field == "Definition"
    ]
    assert len(clears) == 1
    assert clears[0].old_value == shell
    assert clears[0].new_value == ""
    assert "cleared-empty-glossary" in clears[0].badges

    # Idempotence: the card the plan leaves behind has nothing left to fix.
    applied = dict(target.fields)
    applied["Definition"] = good
    second = build_plan([rec(1, applied), sibling], cfg, scope_note_ids=[1])
    assert not [w for w in second.writes if w.note_id == 1 and w.field == "Definition"]


def test_a_shell_with_nothing_to_fill_is_cleared():
    shell = '<div class="yomitan-glossary"><ol></ol></div>'
    f = dict(CHILD.fields)
    f["Definition"] = shell
    f["Sentence"] = "先生は今 診察中ですので"
    plan = build_plan([rec(1, f)], CFG, scope_note_ids=[1])
    clears = [w for w in plan.writes if w.op == OP_STRIP_EMPTY_GLOSSARY and w.field == "Definition"]
    assert len(clears) == 1
    assert clears[0].new_value == ""


def test_an_empty_definition_shell_is_cleared():
    f = dict(CHILD.fields)
    f["Definition"] = JUNK_SHELL
    f["Sentence"] = "先生は今 診察中ですので"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    clears = [w for w in plan.writes if w.op == OP_STRIP_EMPTY_GLOSSARY]
    assert len(clears) == 1
    write = clears[0]
    assert write.field == "Definition"
    assert write.old_value == JUNK_SHELL
    assert write.new_value == ""
    assert write.default_checked is True
    assert write.badges == ("cleared-empty-glossary",)


def test_a_real_definition_is_never_cleared():
    f = dict(CHILD.fields)
    f["Definition"] = '<div class="yomitan-glossary"><ol><li>しんさつ</li></ol></div>'
    f["Sentence"] = "先生は今 診察中ですので"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    assert not [w for w in plan.writes if w.op == OP_STRIP_EMPTY_GLOSSARY]


def test_both_definition_fields_are_cleaned_when_both_are_shells():
    f = dict(CHILD.fields)
    f["Definition"] = JUNK_SHELL
    f["Extended definition"] = '<div class="yomitan-glossary"><ol></ol></div>'
    f["Sentence"] = "先生は今 診察中ですので"
    c = rec(CHILD.note_id, f, tags=("yomitan",))
    plan = build_plan([c], CFG, scope_note_ids=[CHILD.note_id])
    fields = sorted(w.field for w in plan.writes if w.op == OP_STRIP_EMPTY_GLOSSARY)
    assert fields == ["Definition", "Extended definition"]


# --- dimension picker: scene propagation off (BULK_FIX.md B7) -----------------


def test_the_scene_dimension_off_leaves_scene_fills_out_of_the_plan():
    # The picker can turn propagation off without unlocking anything else:
    # no scene-origin row is planned, so no fill can slip through selection.
    cfg = {**CFG, "batch_scene_propagation": False}
    plan = build_plan([PARENT, CHILD], cfg, scope_note_ids=[CHILD_ID])
    assert not any(w.op == OP_FILL for w in plan.writes)
    assert not any(w.op in (OP_EXPAND_SENTENCE, OP_REPLACE_SOURCE) for w in plan.writes)
    # The lossless core still works (bold repair is not a dimension).
    assert any(w.op == OP_BOLD_CLOZE_WORD for w in plan.writes) or not plan.writes


# --- Expression spelling repair (bulk-fix dimension 4, Q-D12) -----------------

# The real ひと握り card (note 1736973220751): the mining kept the subtitle's
# surface form while the note's own furigana records the dictionary form the
# Yomitan lookup resolved to. Before T15 the planner emitted nothing for it
# ("already complete") — the observation that made the gap visible.
HITONIGIRI = rec(
    1736973220751,
    {
        **BASE,
        "Expression": "ひと握り",
        "reading": "ひとにぎり",
        "furigana": "一握[ひとにぎ]り",
        "Sentence": "選別者の中でも&nbsp;ほんの<b>ひと握り</b>の<br>常識を超えた怪物たち",
        "Sentence (furigana)": "選別[せんべつ]者[しゃ]の 中[なか]でもほんのひと 握[にぎ]りの",
        "Sentence Audio": "[sound:Judas_Kami_no_Tou_-_S0_828541_4f19jx02.mp3]",
        "Word Audio": "[sound:yomitan_audio.mp3]",
        "Picture": '<img src="Judas_Kami_no_Tou_-_S0_828640_CMfYsMCA.webp">',
        "Source": "[Judas] Kami no Tou - S01E02.mkv (13m48s)",
    },
)
EXPR_REPAIR_CFG = {**CFG, "batch_repair_expression": True}


def expression_rows(plan):
    return [w for w in plan.writes if w.field == "Expression"]


def test_expression_repair_rewrites_the_surface_form_to_the_furigana_base():
    plan = build_plan([HITONIGIRI], EXPR_REPAIR_CFG, scope_note_ids=[HITONIGIRI.note_id])
    rows = expression_rows(plan)
    assert len(rows) == 1
    write = rows[0]
    assert write.op == OP_REPAIR_EXPRESSION
    assert write.old_value == "ひと握り"
    assert write.new_value == "一握り"
    assert write.default_checked
    assert "expression-repaired-from-furigana" in write.badges


def test_expression_repair_can_be_switched_off():
    # The escape hatch: `batch_repair_expression: false` removes the tier
    # without reporting an anomaly — the card simply stays untouched.
    cfg = {**CFG, "batch_repair_expression": False}
    plan = build_plan([HITONIGIRI], cfg, scope_note_ids=[HITONIGIRI.note_id])
    assert expression_rows(plan) == []
    # Not reported as a new anomaly either — the card simply stays untouched.
    assert plan.reasons.get(HITONIGIRI.note_id) == "no-parent-that-fills"


def test_expression_repair_is_inert_when_the_furigana_matches_the_expression():
    # A correctly mined card: furigana `ひと握[にぎ]り` describes ひと握り.
    target = rec(
        2,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "ひとにぎり",
            "furigana": "ひと握[にぎ]り",
            "Sentence": "ほんの<b>ひと握り</b>の怪物たち",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[2])
    assert expression_rows(plan) == []
    assert plan.warnings == []


def test_expression_repair_is_silent_without_a_furigana_field():
    # No oracle on the note → nothing to derive; kana-only expressions
    # without furigana stay future work (the offline dictionary seam).
    target = rec(
        3,
        {**BASE, "Expression": "いきなり", "reading": "いきなり", "furigana": ""},
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[3])
    assert expression_rows(plan) == []
    assert plan.warnings == []


def test_expression_repair_reports_a_furigana_that_reads_differently():
    # The furigana describes another word than the reading field claims:
    # a mis-mine signal, reported — never rewritten. The card is fully
    # enriched, so the expression reason is the plan's only reason (the
    # scene dimension stays quiet); on a scene-gapped card the same miss
    # surfaces as a run warning instead.
    target = rec(
        4,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "にぎる",
            "furigana": "一握[ひとにぎ]り",
            "Sentence": "ほんの<b>ひと握り</b>の怪物たち",
            "Sentence (furigana)": "ほんのひと 握[にぎ]りの怪物たち",
            "Sentence Audio": "[sound:a.mp3]",
            "Picture": '<img src="p.webp">',
            "Translation": "a handful of monsters",
            "Source": "show (13m48s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[4])
    assert expression_rows(plan) == []
    assert plan.reasons == {4: "expression-repair-reading-mismatch"}
    assert any("expression repair skipped" in warning for warning in plan.warnings)


def test_expression_repair_refuses_a_kanji_less_base():
    # A pure-kana base would be a different word, never a spelling (Q-D12).
    # `ひとに[ぎり]` reads ひとにぎり like the reading field, but its base
    # ひとに carries no kanji to rewrite to.
    target = rec(
        5,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "ひとにぎり",
            "furigana": "ひとに[ぎり]",
            "Sentence": "ほんの<b>ひと握り</b>の怪物たち",
            "Sentence (furigana)": "ほんのひと 握[にぎ]りの怪物たち",
            "Sentence Audio": "[sound:a.mp3]",
            "Picture": '<img src="p.webp">',
            "Translation": "a handful of monsters",
            "Source": "show (13m48s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[5])
    assert expression_rows(plan) == []
    assert plan.reasons == {5: "expression-repair-no-kanji-form"}


def test_expression_repair_refuses_a_clause_like_base():
    # The Q-D3 word-like gate applies to the base too: an over-long or
    # clause-like base is refused.
    target = rec(
        6,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "ひとにぎり",
            "furigana": "漢字能力検定試験実施企画連合協議会総本部全国大会予選[ひとにぎり]",
            "Sentence": "ほんの<b>ひと握り</b>の怪物たち",
            "Sentence (furigana)": "ほんのひと 握[にぎ]りの怪物たち",
            "Sentence Audio": "[sound:a.mp3]",
            "Picture": '<img src="p.webp">',
            "Translation": "a handful of monsters",
            "Source": "show (13m48s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[6])
    assert expression_rows(plan) == []
    assert plan.reasons == {6: "expression-not-word-like"}


def test_expression_repair_completes_a_kana_only_expression_with_kanji():
    # The same class as ひと握り with no kanji mined at all: the dictionary
    # form recorded in the furigana supplies every kanji.
    target = rec(
        20,
        {
            **BASE,
            "Expression": "とっておき",
            "reading": "とっておき",
            "furigana": "取[と]って 置[お]き",
            "Sentence": "ほんの<b>とっておき</b>の話",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[20])
    rows = expression_rows(plan)
    assert len(rows) == 1
    assert rows[0].old_value == "とっておき"
    assert rows[0].new_value == "取って置き"


def test_expression_repair_completes_a_mixed_expression_with_kanji():
    # A kanji + kana spelling that gains one more kanji (子供のころ → 子供の頃).
    target = rec(
        21,
        {
            **BASE,
            "Expression": "子供のころ",
            "reading": "こどものころ",
            "furigana": "子供[こども]の 頃[ころ]",
            "Sentence": "ほんの<b>子供のころ</b>の話",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[21])
    rows = expression_rows(plan)
    assert len(rows) == 1
    assert rows[0].new_value == "子供の頃"


def test_expression_repair_normalizes_an_inflected_dictionary_form():
    # 決まってる → 決まる and 倍にして → 倍にする: the Expression is updated to
    # the uninflected dictionary form recorded in furigana when the reading
    # matches and the kanji stem is preserved.
    target = rec(
        22,
        {
            **BASE,
            "Expression": "決まってる",
            "reading": "きまる",
            "furigana": "決[き]まる",
            "Sentence": "運命は<b>決まってる</b>",
            "Sentence (furigana)": "運命[うんめい]は決[き]まってる",
            "Sentence Audio": "[sound:b.mp3]",
            "Picture": '<img src="q.webp">',
            "Translation": "it is decided",
            "Source": "show (1m02s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[22])
    rows = expression_rows(plan)
    assert len(rows) == 1
    assert rows[0].old_value == "決まってる"
    assert rows[0].new_value == "決まる"
    assert "expression-repaired-from-furigana" in rows[0].badges
    assert "expression-normalized-to-lemma" in rows[0].badges

    # Real card 1739215432868: 倍にして → 倍にする
    target_bainishite = rec(
        1739215432868,
        {
            **BASE,
            "Expression": "倍にして",
            "reading": "ばいにする",
            "furigana": "倍[ばい]にする",
            "Sentence": (
                "北村先生 僕 いつごろ歩けるようになるんですか？"
                "じゃあ 今日からリハビリの量を<b>倍にして</b>ください｡"
            ),
        },
    )
    plan_bai = build_plan([target_bainishite], EXPR_REPAIR_CFG, scope_note_ids=[1739215432868])
    rows_bai = expression_rows(plan_bai)
    assert len(rows_bai) == 1
    assert rows_bai[0].old_value == "倍にして"
    assert rows_bai[0].new_value == "倍にする"
    assert "expression-repaired-from-furigana" in rows_bai[0].badges
    assert "expression-normalized-to-lemma" in rows_bai[0].badges


def test_expression_repair_refuses_a_kana_preference_spelling():
    # The dictionary writes 気づき; the mined 気付き is a valid spelling, so
    # dropping kanji is not a completion — reported, never rewritten.
    target = rec(
        23,
        {
            **BASE,
            "Expression": "気付き",
            "reading": "きづき",
            "furigana": "気[き]づき",
            "Sentence": "小さな<b>気付き</b>を大切に",
            "Sentence (furigana)": "小[ちい]さな気[き]づきを大[たい]切[せつ]に",
            "Sentence Audio": "[sound:c.mp3]",
            "Picture": '<img src="r.webp">',
            "Translation": "a small realization",
            "Source": "show (1m03s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[23])
    assert expression_rows(plan) == []
    assert plan.reasons == {23: "expression-repair-not-a-kanji-completion"}


def test_expression_repair_refuses_a_different_kanji_homophone():
    # 美味い and 上手い both read うまい but mean different things; the
    # furigana alone cannot decide, so the card is reported (rule 5).
    target = rec(
        24,
        {
            **BASE,
            "Expression": "美味い",
            "reading": "うまい",
            "furigana": "上手[うま]い",
            "Sentence": "この店の料理は<b>美味い</b>",
            "Sentence (furigana)": "この店[みせ]の料理[りょうり]は美[う]味[ま]い",
            "Sentence Audio": "[sound:d.mp3]",
            "Picture": '<img src="s.webp">',
            "Translation": "this restaurant's food is delicious",
            "Source": "show (1m04s)",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[24])
    assert expression_rows(plan) == []
    assert plan.reasons == {24: "expression-repair-not-a-kanji-completion"}


def test_expression_repair_ignores_html_ruby_furigana():
    # An HTML-ruby furigana is an unusable oracle (its <rt> text would
    # corrupt the base), so the card is treated like a furigana-less one:
    # silent, not a reason.
    target = rec(
        25,
        {
            **BASE,
            "Expression": "温める",
            "reading": "あたためる",
            "furigana": "<ruby>温<rt>あたた</rt></ruby>める",
            "Sentence": "キンタマを<b>温めたら</b>ダメ",
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[25])
    assert expression_rows(plan) == []
    assert plan.warnings == []


def test_expression_repair_is_deferred_while_word_writes_are_queued():
    # F2 risk 4: a word-level fill (e.g. Frequency) is sourced from the old
    # spelling's word group; the rewrite must not land in the same run. The
    # next pass, with the word field filled, picks the rewrite up.
    sibling = rec(
        7,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "ひとにぎり",
            "furigana": "一握[ひとにぎ]り",
            "Frequency": "14200",
        },
    )
    target = rec(
        8,
        {
            **BASE,
            "Expression": "ひと握り",
            "reading": "ひとにぎり",
            "furigana": "一握[ひとにぎ]り",
            "Sentence": "ほんの<b>ひと握り</b>の怪物たち",
            "Frequency": "",
        },
    )
    plan = build_plan([sibling, target], EXPR_REPAIR_CFG, scope_note_ids=[8])
    assert any(w.field == "Frequency" for w in plan.writes)  # the word tier ran
    assert expression_rows(plan) == []


def test_expression_repair_runs_alongside_non_word_writes():
    # Bold markup and scene media do not depend on the word group: the
    # rewrite is not deferred for them.
    target = rec(
        9,
        {
            **HITONIGIRI.fields,
            "Sentence": "ほんのひと握りの怪物たち",  # no bold → bold repair too
        },
    )
    plan = build_plan([target], EXPR_REPAIR_CFG, scope_note_ids=[9])
    assert expression_rows(plan)
    assert any(w.op == OP_BOLD_CLOZE_WORD for w in plan.writes)


_ZHENGDAO_CHILD = {
    "Expression": "正道",
    "reading": "せいどう",
    "furigana": "正[せい]道[どう]",
    "Sentence": "兄(オレ)が正道を歩んだのなら",
    "Sentence Audio": "",
    "Picture": "",
    # The real 正道 card's Source has no mining timestamp, so the moment-group
    # path does not apply and the scene matcher runs (as it does live).
    "Source": "[Judas] JUJUTSU KAISEN - S03E02",
    "cloze-prefix": "",
    "cloze-body": "正道",
    "cloze-suffix": "",
}


def test_a_dictionary_word_recording_is_not_scene_audio():
    # A parent whose Sentence Audio is a dictionary pack's word recording is a
    # word card, not a scene: its recording is never copied onto a child's
    # scene fields.
    child = rec(1, dict(_ZHENGDAO_CHILD))
    word_card = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": "兄(オレ)が正道を歩んだのなら 弟は 後をついてくればいい",
            "Sentence Audio": " [sound:yomitan_audio_abc.ogg] ",
            "Picture": '<img src="dictionary_pack.jpg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "cloze-prefix": "",
            "cloze-body": "出来",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([child, word_card], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.note_id == 1 and w.field == "Sentence Audio"]


def test_a_jidoujisho_clip_is_scene_audio_and_completes_the_card():
    # jidoujisho-* is Jidoujisho's video-player clip export — the scene's own
    # audio/picture, not a dictionary recording (verified on the configured
    # deck: all 683 such files appear only in Sentence Audio, never in Word
    # Audio). The 正道 card is completed from it: the two-line word fragment
    # is replaced by the parent's whole nine-line scene (the word bolded), and
    # the clip and picture come along in the same run.
    child = rec(1, dict(_ZHENGDAO_CHILD))
    scene_parent = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": (
                "デキがよかろうと<br>悪かろうと<br>兄は 弟の手本なんだ<br>兄(オレ)が"
                "<br>道を誤ったのなら<br>弟は その道を<br>避ければいい"
                "<br>兄(オレ)が正道を歩んだのなら<br>弟は 後をついてくればいい"
            ),
            "Sentence Audio": " [sound:jidoujisho-20260208T104227_6236629731955619868.mp3] ",
            "Picture": '<img src="jidoujisho-20260208T104227_653.jpg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "cloze-prefix": "",
            "cloze-body": "出来",
            "cloze-suffix": "",
        },
        tags=("asbplayer",),
    )
    plan = build_plan([child, scene_parent], CFG, scope_note_ids=[1])
    rows = {(w.field, w.op): w for w in plan.writes if w.note_id == 1}
    expansion = rows[("Sentence", OP_EXPAND_SENTENCE)]
    assert expansion.default_checked
    assert "<b>正道</b>" in expansion.new_value
    assert "デキがよかろうと" in expansion.new_value
    assert "fragment-kept-in-scene" in expansion.badges
    audio = rows[("Sentence Audio", OP_FILL)]
    assert audio.default_checked and "jidoujisho" in audio.new_value
    assert rows[("Picture", OP_FILL)].default_checked


def test_recovery_is_refused_when_the_word_is_present_in_katakana():
    # 自閉症すぺくとらむ: the Expression is kana, the sentence has katakana —
    # the word IS present, so the sentence is not a mis-mine and must not be
    # replaced by another scene that merely mentions the phrase.
    target = rec(
        1,
        {
            "Expression": "自閉症すぺくとらむ",
            "reading": "じへいしょうすぺくとらむ",
            "furigana": "自[じ]閉[へい]症[しょう]すぺくとらむ",
            "Sentence": (
                "深也くんは<br>自閉症スペクトラムでありながら"
                "<br>その立体造形に類いまれなる才能を持ち"
            ),
            "Sentence Audio": "",
            "Picture": "",
            "Source": "Show.mp4 (15m20s)",
            "cloze-prefix": "",
            "cloze-body": "自閉症スペクトラム",
            "cloze-suffix": "でありながら",
        },
    )
    other = rec(
        2,
        {
            "Expression": "親御",
            "reading": "おやご",
            "Sentence": "自閉症スペクトラムの子供を持つ 親御さんは つい自分を責めてしまいます",
            "Sentence Audio": " [sound:asbp_x.webm] ",
            "Picture": '<img src="asbp_x.jpg">',
            "Source": "Show.mp4 (12m00s)",
            "cloze-prefix": "",
            "cloze-body": "親御",
            "cloze-suffix": "",
        },
    )
    third = rec(
        3,
        {
            "Expression": "授乳",
            "reading": "じゅにゅう",
            "Sentence": (
                "自閉症スペクトラムの子供を持つ 親御さんは つい自分を責めてしまいます 授乳中"
            ),
            "Sentence Audio": " [sound:asbp_y.webm] ",
            "Picture": '<img src="asbp_y.jpg">',
            "Source": "Show.mp4 (12m10s)",
            "cloze-prefix": "",
            "cloze-body": "授乳",
            "cloze-suffix": "",
        },
    )
    plan = build_plan([target, other, third], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if "recovered-sentence" in w.badges]


def test_card_with_existing_audio_is_never_overwritten_by_moment_group():
    # A card that already carries its own Sentence Audio is a scene unit in its
    # own right (QUALITY Q-D16) — neither its sentence nor its audio may ever
    # be overwritten by a neighbouring card mined at the same moment.
    target = rec(
        1,
        {
            "Expression": "自負",
            "reading": "じふ",
            "Sentence": "本日は皆さんに研究に関してお願いがあります。自負しております。",
            "Sentence Audio": " [sound:asbp_dr._rintaro_episode_7__1_901356_x830iyzy.webm] ",
            "Source": "Dr. Rintaro Episode 7 (1).mp4 (15m01s)",
            "cloze-body": "自負",
        },
    )
    mate = rec(
        2,
        {
            "Expression": "別の言葉",
            "Sentence": "全く別の文です。",
            "Sentence Audio": " [sound:asbp_show_other.webm] ",
            "Source": "Dr. Rintaro Episode 7 (1).mp4 (15m01s)",
            "cloze-body": "別の言葉",
        },
    )
    plan = build_plan([target, mate], CFG, scope_note_ids=[1])
    assert not [
        w for w in plan.writes if w.note_id == 1 and w.field in ("Sentence", "Sentence Audio")
    ]


def test_sentence_audio_is_strictly_fill_only_never_overwritten():
    # QUALITY Q-D1: Fills only touch empty fields. An existing, non-empty
    # Sentence Audio must never be overwritten even if neighbouring cards agree
    # on a different clip.
    target = rec(
        1,
        {
            "Expression": "単語",
            "Sentence": "共通の文です。",
            "Sentence Audio": " [sound:clip_a.webm] ",
            "Source": "Video.mp4 (10m00s)",
            "cloze-body": "単語",
        },
    )
    mates = [
        rec(
            i,
            {
                "Expression": f"単語{i}",
                "Sentence": "共通の文です。",
                "Sentence Audio": " [sound:clip_b.webm] ",
                "Source": "Video.mp4 (10m00s)",
                "cloze-body": f"単語{i}",
            },
        )
        for i in (2, 3, 4)
    ]
    plan = build_plan([target, *mates], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.note_id == 1 and w.field == "Sentence Audio"]


def test_word_recovery_never_overwrites_card_that_owns_sentence_audio():
    # A card with its own Sentence Audio and an inflected spelling (読め) is a
    # complete scene unit even when Translation is empty (Q-D16); same-episode
    # cards mentioning 読む in another scene must never overwrite its Sentence.
    target = rec(
        1,
        {
            "Expression": "読む",
            "reading": "よむ",
            "furigana": "読[よ]む",
            "Sentence": "この本を<b>読め</b>と言われた。",
            "Sentence Audio": " [sound:scene_15m01s.webm] ",
            "Picture": '<img src="scene_15m01s.jpg">',
            "Translation": "",
            "Source": "Show.mp4 (15m01s)",
            "cloze-body": "",
        },
    )
    other1 = rec(
        2,
        {
            "Expression": "趣味",
            "Sentence": "毎日新聞を読むのが私の<b>趣味</b>です。",
            "Sentence Audio": " [sound:scene_20m00s.webm] ",
            "Picture": '<img src="scene_20m00s.jpg">',
            "Source": "Show.mp4 (20m00s)",
            "cloze-body": "趣味",
        },
    )
    other2 = rec(
        3,
        {
            "Expression": "新聞",
            "Sentence": "毎日<b>新聞</b>を読むのが私の趣味です。",
            "Sentence Audio": " [sound:scene_20m00s.webm] ",
            "Picture": '<img src="scene_20m00s.jpg">',
            "Source": "Show.mp4 (20m00s)",
            "cloze-body": "新聞",
        },
    )
    plan = build_plan([target, other1, other2], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.note_id == 1 and w.field == "Sentence"]


def test_word_recovery_skips_junk_sources_and_cleans_recovered_sentence():
    # Q-D8 + Q-A2: recovered scene sentences strip configured noise in the same
    # write, and placeholder/separator values in scene fill fields are ignored.
    target = rec(
        1,
        {
            "Expression": "類い希",
            "reading": "たぐいまれ",
            "furigana": "類[たぐ]い 希[まれ]",
            "Sentence": "関係のない文です。",
            "Sentence Audio": "",
            "Picture": "",
            "Translation": "",
            "Sentence (furigana)": "",
            "Source": "Show.mp4 (15m01s)",
            "cloze-body": "類い希",
        },
    )
    shared_ruby = (
        "その 立体[りったい] 造形[ぞうけい]に<br> 類[たぐ]いまれなる 才能[さいのう]を 持[も]ち➡"
    )
    s1 = rec(
        2,
        {
            "Expression": "造形",
            "Sentence": "その立体<b>造形</b>に<br>類いまれなる才能を持ち➡",
            "Sentence (furigana)": shared_ruby,
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Translation": "<hr>",
            "Source": "Show.mp4 (15m20s)",
            "cloze-body": "造形",
        },
    )
    s2 = rec(
        3,
        {
            "Expression": "才能",
            "Sentence": "その立体造形に<br>類いまれなる<b>才能</b>を持ち➡",
            "Sentence (furigana)": shared_ruby,
            "Sentence Audio": " [sound:a.webm] ",
            "Picture": '<img src="a.jpg">',
            "Translation": "<hr>",
            "Source": "Show.mp4 (15m20s)",
            "cloze-body": "才能",
        },
    )
    cfg = {**CFG, "sentence_repair_rules": ["strip-arrows"]}
    plan = build_plan([target, s1, s2], cfg, scope_note_ids=[1])
    by_field = {w.field: w for w in plan.writes if w.note_id == 1}
    assert "➡" not in by_field["Sentence"].new_value
    assert "expanded-strip-arrows" in by_field["Sentence"].badges
    assert "Sentence (furigana)" in by_field
    assert "➡" not in by_field["Sentence (furigana)"].new_value
    assert "Translation" not in by_field  # <hr> is junk, never copied


def test_existing_bold_normalization_strips_noise_and_respects_guards():
    # Moving a foreign bold must also strip tail arrows in one pass, and must
    # never place a new bold via a 2-kana reading or a clause-like Expression.
    cfg = {**CFG, "sentence_repair_rules": ["strip-arrows"]}
    card = rec(
        1,
        {
            "Expression": "診察",
            "reading": "しんさつ",
            "furigana": "診察[しんさつ]",
            "cloze-body": "診察",
            "Sentence": "<b>至急</b> 先生は今 診察中ですので➡",
        },
    )
    plan = build_plan([card], cfg, scope_note_ids=[1])
    rows = [w for w in plan.writes if w.field == "Sentence"]
    assert len(rows) == 1
    assert rows[0].new_value == "至急 先生は今 <b>診察</b>中ですので"
    assert "strip-arrows" in rows[0].badges

    short_reading = rec(
        2,
        {
            "Expression": "為る",
            "reading": "する",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "<b>それ</b>をするつもりです。",
        },
    )
    assert not build_plan([short_reading], cfg, scope_note_ids=[2]).writes

    clause_expr = rec(
        3,
        {
            "Expression": "漢字は読めるようになりたいかな",
            "reading": "",
            "furigana": "",
            "cloze-body": "",
            "Sentence": "<b>別の語</b> 漢字は読めるようになりたいかな",
        },
    )
    assert not build_plan([clause_expr], cfg, scope_note_ids=[3]).writes


def test_existing_complex_bold_markup_is_not_double_wrapped():
    # Nested HTML inside <b> or <strong> tags is left untouched rather than
    # wrapping a second <b> that would fail post-write verification.
    card = rec(
        1,
        {
            "Expression": "診察",
            "reading": "しんさつ",
            "cloze-body": "診察",
            "Sentence": "<b>至急<br>連絡</b> 先生は今 診察中ですので",
        },
    )
    plan = build_plan([card], CFG, scope_note_ids=[1])
    assert not [w for w in plan.writes if w.op == OP_BOLD_CLOZE_WORD]


def test_demoted_completion_keeps_guard_visible_and_cleans_fragment_ruby_arrows():
    # If furigana regeneration fails on a completion that needed a rebuilt ruby,
    # the completion is demoted with `stale-fragment-furigana-risk` kept visible
    # on Sentence (never silently replaced), while subtitle arrows in the kept
    # fragment ruby are still cleaned.
    child = rec(
        1,
        {
            **_ZHENGDAO_CHILD,
            "Sentence": "兄(オレ)が<b>正道</b>を歩んだのなら",
            "Sentence (furigana)": "兄[あに](オレ)が 正道[せいどう]を 歩[あゆ]んだのなら➡",
        },
    )
    parent = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": "兄(オレ)が正道を歩んだのなら 弟は 後をついてくればいい",
            "Sentence Audio": " [sound:asbp_x.webm] ",
            "Picture": '<img src="asbp_x.jpeg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "Sentence (furigana)": "",
            "cloze-body": "出来",
        },
    )
    cfg = {
        **CFG,
        "batch_regenerate_furigana": True,
        "sentence_repair_rules": ["strip-arrows"],
    }
    plan = build_plan([child, parent], cfg, scope_note_ids=[1], furigana_generator=lambda _s: "")
    expansion = next(w for w in plan.writes if w.note_id == 1 and w.op == OP_EXPAND_SENTENCE)
    assert not expansion.default_checked
    assert "stale-fragment-furigana-risk" in expansion.badges
    checked = {(w.field, w.op): w for w in plan.writes if w.note_id == 1 and w.default_checked}
    assert ("Sentence (furigana)", OP_REPAIR_SENTENCE) in checked
    assert "➡" not in checked[("Sentence (furigana)", OP_REPAIR_SENTENCE)].new_value


def test_fragment_ruby_arrow_does_not_block_furigana_regeneration_on_expansion():
    # When a card expands to the full scene and its old fragment ruby carries a
    # tail arrow, ruby noise-repair must not claim FURIGANA_FIELD with the old
    # fragment and block furigana regeneration for the expanded sentence.
    child = rec(
        1,
        {
            **_ZHENGDAO_CHILD,
            "Sentence (furigana)": "兄[あに](オレ)が 正道[せいどう]を 歩[あゆ]んだのなら➡",
        },
    )
    parent = rec(
        2,
        {
            "Expression": "出来",
            "reading": "でき",
            "Sentence": "兄(オレ)が正道を歩んだのなら 弟は 後をついてくればいい",
            "Sentence Audio": " [sound:asbp_x.webm] ",
            "Picture": '<img src="asbp_x.jpeg">',
            "Source": "[Judas] JUJUTSU KAISEN - S03E02",
            "Sentence (furigana)": "",
            "cloze-body": "出来",
        },
    )
    cfg = {
        **CFG,
        "batch_regenerate_furigana": True,
        "sentence_repair_rules": ["strip-arrows"],
    }
    plan = build_plan([child, parent], cfg, scope_note_ids=[1], furigana_generator=lambda s: s)
    checked = {(w.field, w.op): w for w in plan.writes if w.note_id == 1 and w.default_checked}
    assert ("Sentence", OP_EXPAND_SENTENCE) in checked
    assert ("Sentence (furigana)", OP_REGENERATE_FURIGANA) in checked


def test_missing_image_alongside_valid_sound_still_drops_fill():
    parent_f = dict(PARENT.fields)
    parent_f["Picture"] = '[sound:ok.webm] <img src="missing.jpeg">'
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    plan = build_plan(
        [p, CHILD],
        CFG,
        scope_note_ids=[CHILD.note_id],
        media_exists=lambda name: name != "missing.jpeg",
    )
    assert not [w for w in plan.writes if w.note_id == CHILD.note_id and w.field == "Picture"]


def test_config_field_and_autocheck_overrides_are_respected():
    parent_f = dict(PARENT.fields)
    parent_f["Translation"] = "The doctor is in an examination."
    parent_f["Notes"] = "polite context"
    p = rec(PARENT.note_id, parent_f, tags=("asbplayer", "yomitan"))
    cfg = {
        **CFG,
        "translation_autocheck": False,
        "notes_autocheck": True,
        "never_copy_fields": ["Picture"],
        "scene_replace_fields": ["Sentence"],  # Source replacement switched off
    }
    plan = build_plan([p, CHILD], cfg, scope_note_ids=[CHILD.note_id])
    by_field = {w.field: w for w in plan.writes if w.note_id == CHILD.note_id}
    assert "Picture" not in by_field
    assert "Source" not in by_field
    assert by_field["Translation"].default_checked is False
    assert by_field["Notes"].default_checked is True


def test_bold_repair_recognizes_inflected_span_with_morph_tokenizer():
    # Issue #1: 淀む (reading よどむ) with existing bold around よどん
    target = rec(
        1793400721969,
        {
            "Expression": "淀む",
            "reading": "よどむ",
            "Sentence": "それもひどく<b>よどん</b>だ水脈をかきまぜてしまうようなことだった。",
        },
    )

    def mock_tok(text: str):
        return [
            MorphToken("それ", "名詞", "それ", "ソレ"),
            MorphToken("も", "助詞", "も", "モ"),
            MorphToken("ひどく", "形容詞", "ひどい", "ヒドク"),
            MorphToken("よどん", "動詞", "よどむ", "ヨドン"),
            MorphToken("だ", "助動詞", "だ", "ダ"),
        ]

    plan = build_plan([target], CFG, scope_note_ids=[target.note_id], morph_tokenizer=mock_tok)
    # Recognized as already bolded; no write generated, no missing bold reported
    assert not [w for w in plan.writes if w.note_id == target.note_id and w.field == "Sentence"]
    assert not any("no bold in Sentence" in w for w in plan.warnings)


def test_bold_repair_places_inflected_bold_with_morph_tokenizer():
    # 読む (reading よむ) in "本を読め。" -> "本を<b>読め</b>。"
    target = rec(
        1,
        {
            "Expression": "読む",
            "reading": "よむ",
            "Sentence": "本を読め。",
        },
    )

    def mock_tok(text: str):
        return [
            MorphToken("本", "名詞", "本", "ホン"),
            MorphToken("を", "助詞", "を", "ヲ"),
            MorphToken("読め", "動詞", "読む", "ヨメ"),
            MorphToken("。", "記号", "。", "。"),
        ]

    plan = build_plan([target], CFG, scope_note_ids=[1], morph_tokenizer=mock_tok)
    writes = [w for w in plan.writes if w.note_id == 1 and w.field == "Sentence"]
    assert len(writes) == 1
    assert writes[0].op == OP_BOLD_CLOZE_WORD
    assert writes[0].new_value == "本を<b>読め</b>。"
    assert "bold-added-to-inflected-form" in writes[0].badges

    # Verify idempotence on second pass
    target_pass2 = rec(1, {**target.fields, "Sentence": writes[0].new_value})
    plan2 = build_plan([target_pass2], CFG, scope_note_ids=[1], morph_tokenizer=mock_tok)
    assert not [w for w in plan2.writes if w.note_id == 1 and w.field == "Sentence"]


def test_bold_repair_refuses_ambiguous_multiple_inflections():
    # 行く in "早く行って、行っておいで。" has 2 occurrences of 行って
    target = rec(
        1,
        {
            "Expression": "行く",
            "reading": "いく",
            "Sentence": "早く行って、行っておいで。",
        },
    )

    def mock_tok(text: str):
        return [
            MorphToken("早く", "副詞", "早く", "ハヤク"),
            MorphToken("行って", "動詞", "行く", "イッテ"),
            MorphToken("、", "記号", "、", "、"),
            MorphToken("行っ", "動詞", "行く", "イッ"),
            MorphToken("て", "助詞", "て", "テ"),
            MorphToken("おいで", "動詞", "おいで", "オイデ"),
            MorphToken("。", "記号", "。", "。"),
        ]

    plan = build_plan([target], CFG, scope_note_ids=[1], morph_tokenizer=mock_tok)
    assert not [w for w in plan.writes if w.note_id == 1 and w.field == "Sentence"]
    assert plan.reasons.get(1) == "cloze-word-ambiguous"
