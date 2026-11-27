"""The batch preview report must be readable by a human, not a data dump."""

from __future__ import annotations

from test_plan import rec

from mining_fixer.batch import BatchSelection, LeftAlone, default_ops, select
from mining_fixer.plan import (
    OP_ADD_TAGS,
    OP_BOLD_CLOZE_WORD,
    OP_EXPAND_SENTENCE,
    OP_FILL,
    OP_REGENERATE_FURIGANA,
    OP_REPAIR_SENTENCE,
    OP_REPLACE_SOURCE,
    Plan,
    PlannedWrite,
)
from mining_fixer.report import (
    GLANCE_LIMIT,
    badge_explanation,
    change_lines,
    guard_reason,
    render_batch_summary,
)


def note(note_id, expression="診察", reading="しんさつ"):
    return rec(
        note_id,
        {
            "Expression": expression,
            "reading": reading,
            "Sentence": "fragment",
            "Sentence Audio": "[sound:a.webm]",
            "Picture": "<img>",
            "Source": "Show 1.mp4 (1m00s)",
            "Sentence (furigana)": "ruby",
        },
        tags=(),
    )


def writes(note_id):
    return (
        PlannedWrite(
            note_id=note_id,
            field="Sentence",
            op=OP_EXPAND_SENTENCE,
            old_value="fragment",
            new_value="full scene",
            source_note_id=9,
            default_checked=True,
        ),
        PlannedWrite(
            note_id=note_id,
            field="Picture",
            op=OP_FILL,
            old_value="",
            new_value="<img>",
            source_note_id=9,
            default_checked=True,
        ),
        PlannedWrite(
            note_id=note_id,
            field="tags",
            op=OP_ADD_TAGS,
            old_value="yomitan",
            new_value="asbplayer yomitan",
            default_checked=True,
        ),
    )


def test_the_preview_reads_like_a_sentence_not_a_dump():
    plan = Plan(
        writes=writes(1),
        reasons={2: "nothing-to-fill"},
    )
    records = {1: note(1), 2: note(2, "尾ける", "つける")}
    selection = select(plan, default_ops())
    text = render_batch_summary(plan, records, selection, {"scope": 2})

    assert "# Mining Fixer — preview (nothing was changed)" in text
    assert "will be fixed: 1 note(s)" in text
    assert "unchanged: 1 note(s)" in text
    # Human labels, not field names or op ids.
    assert "full scene sentence" in text
    assert "picture" in text
    assert "expand_sentence" not in text
    assert "OP_FILL" not in text


def test_left_alone_is_listed_with_its_reason_and_no_question():
    selection = BatchSelection(
        rows=(),
        left_alone=(LeftAlone(5, "sentence left alone — fragment not recoverable"),),
        untouched=1,
    )
    text = render_batch_summary(
        Plan(writes=()), {5: note(5, "尾ける", "つける")}, selection, {"scope": 2}
    )
    assert "| 尾ける | 5 | sentence left alone — fragment not recoverable |" in text
    assert "Nothing here needs a decision" in text


def test_bold_repairs_are_labelled_for_a_human():
    plan = Plan(
        writes=(
            PlannedWrite(
                note_id=7,
                field="Sentence",
                op=OP_BOLD_CLOZE_WORD,
                old_value="母は うつ病に。",
                new_value="母は <b>うつ病</b>に。",
                default_checked=True,
            ),
        )
    )
    text = render_batch_summary(
        plan, {7: note(7, "鬱病", "うつびょう")}, select(plan, default_ops()), {"scope": 1}
    )
    assert "bold on the tested word" in text
    assert "bold_cloze_word" not in text


def test_repair_rows_are_labelled_for_a_human():
    plan = Plan(
        writes=(
            PlannedWrite(
                note_id=7,
                field="Sentence",
                op=OP_REPAIR_SENTENCE,
                old_value="あしたの朝刊の見出しは<br>こうニャ！➡",
                new_value="あしたの朝刊の見出しは<br>こうニャ！",
                default_checked=True,
                badges=("strip-arrows",),
            ),
        )
    )
    text = render_batch_summary(
        plan,
        {7: note(7, "見出し", "みだし")},
        select(plan, default_ops({"batch_repair_sentence": True})),
        {"scope": 1},
    )
    assert "sentence noise repair" in text
    assert "repair_sentence" not in text


def test_furigana_rows_are_labelled_for_a_human():
    plan = Plan(
        writes=(
            PlannedWrite(
                note_id=7,
                field="Sentence (furigana)",
                op=OP_REGENERATE_FURIGANA,
                old_value="見出しはこうニャ!",
                new_value="見出[みだ]しはこうニャ!",
                default_checked=True,
                badges=("furigana-regenerated",),
            ),
        )
    )
    text = render_batch_summary(
        plan,
        {7: note(7, "見出し", "みだし")},
        select(plan, default_ops({"batch_regenerate_furigana": True})),
        {"scope": 1},
    )
    assert "furigana regenerated" in text
    assert "regenerate_furigana" not in text


# --- the batch window's glance list (report.change_lines) -----------------


def test_the_change_glimpse_labels_each_note_and_samples_the_rewrites():
    # The glimpse must answer "is this change sound?" without opening the
    # report: human labels per note, and a short before → after only where
    # the text is actually rewritten.
    plan = Plan(writes=writes(1), reasons={2: "nothing-to-fill"})
    records = {1: note(1), 2: note(2, "尾ける", "つける")}
    lines = change_lines(select(plan, default_ops()), records)

    assert len(lines) == 1
    line = lines[0]
    assert line.startswith("診察 — ")
    assert "picture" in line
    assert "tags: asbplayer" in line
    assert "full scene sentence (fragment → full scene)" in line


def test_the_change_glimpse_stays_label_only_where_the_values_are_noise():
    # Ruby markup and markup-only bold are not readable in one line; their
    # labels carry the meaning, the values would only bury it.
    plan = Plan(
        writes=(
            PlannedWrite(
                note_id=7,
                field="Sentence",
                op=OP_BOLD_CLOZE_WORD,
                old_value="母は うつ病に。",
                new_value="母は <b>うつ病</b>に。",
                default_checked=True,
            ),
            PlannedWrite(
                note_id=7,
                field="Sentence (furigana)",
                op=OP_REGENERATE_FURIGANA,
                old_value="見出しはこうニャ!",
                new_value="見出[みだ]しはこうニャ!",
                default_checked=True,
                badges=("furigana-regenerated",),
            ),
        )
    )
    lines = change_lines(select(plan, default_ops()), {7: note(7, "見出し", "みだし")})

    assert lines == ["見出し — bold on the tested word · furigana regenerated"]


def test_the_change_glimpse_caps_itself_and_points_at_the_report():
    plan = Plan(
        writes=tuple(
            PlannedWrite(
                note_id=note_id,
                field="Picture",
                op=OP_FILL,
                old_value="",
                new_value="<img>",
                source_note_id=9,
                default_checked=True,
            )
            for note_id in range(1, 46)
        )
    )
    records = {note_id: note(note_id) for note_id in range(1, 46)}
    lines = change_lines(select(plan, default_ops()), records)

    assert len(lines) == GLANCE_LIMIT + 1
    assert lines[-1] == f"… and {45 - GLANCE_LIMIT} more note(s) — the report has the full list"


def test_the_change_glimpse_names_the_note_without_an_expression():
    plan = Plan(
        writes=(
            PlannedWrite(
                note_id=7,
                field="Picture",
                op=OP_FILL,
                old_value="",
                new_value="<img>",
                default_checked=True,
            ),
        )
    )
    lines = change_lines(select(plan, default_ops()), {})

    assert lines == ["note 7 — picture"]


# --- badge_explanation ---------------------------------------------------


def test_badge_explanation_returns_text_for_known_static_badges():
    assert badge_explanation("divergent-parents") == (
        "Scene siblings disagree on audio/sentence — requires manual review"
    )
    assert badge_explanation("bold-added-to-cloze-word") == (
        "Bold wrapped around the mined cloze body"
    )
    assert badge_explanation("furigana-regenerated") == (
        "Furigana regenerated by the reading engine"
    )


def test_badge_explanation_handles_dynamic_source_note():
    assert badge_explanation("source-note:1234567") == "Value sourced from note 1234567"


def test_badge_explanation_handles_dynamic_siblings_agree():
    assert badge_explanation("3-siblings-agree") == "3 sibling cards agree on this value"


def test_badge_explanation_handles_dynamic_added_gap():
    assert badge_explanation("added-gap:2.3h") == "Source is 2.3h away in time"


def test_badge_explanation_handles_dynamic_expanded_rule():
    result = badge_explanation("expanded-strip-arrows")
    assert "strip" in result.lower() and "arrow" in result.lower()


def test_badge_explanation_returns_badge_itself_for_unknown():
    assert badge_explanation("some-unknown-badge") == "some-unknown-badge"


def test_all_known_badges_have_explanations():
    """Every badge that the planner produces must have a non-trivial explanation."""
    known_static_badges = [
        # Bold repair
        "bold-added-to-cloze-word",
        "bold-added-to-expression",
        "bold-added-to-surface-form",
        "bold-added-to-reading-form",
        "bold-added-to-kana-mix",
        "bold-added-to-inflected-form",
        "bold-normalized-to-one",
        "bold-trimmed-punctuation",
        "bold-retargeted-to-cloze-word",
        "bold-retargeted-to-expression",
        "stale-parent-bold",
        "parent-bold-removed",
        # Scene expansion
        "expands-sentence",
        "expansion-guard",
        "fragment-kept-in-cloze",
        "fragment-kept-in-scene",
        "fragment-not-recoverable",
        "cloze-anchor-not-unique",
        "paired-with-expansion",
        # Scene source confidence
        "divergent-parents",
        "far-from-source",
        "corroborated-against-divergence",
        "same-scene-tighter-clip",
        "interior-span",
        "scene-block-parent",
        # Furigana
        "replaces-fragment-furigana",
        "ruby-will-be-regenerated",
        "stale-fragment-furigana-risk",
        "scene-furigana",
        "furigana-regenerated",
        "after-sentence-write",
        "ruby",
        # Sentence recovery
        "recovered-sentence",
        "word-in-sentence",
        # Expression repair
        "expression-repaired-from-furigana",
        "expression-normalized-to-lemma",
        # Glossary
        "cleared-empty-glossary",
        # Tags
        "tags-union-never-removes",
        # Source
        "replaces-blob-url",
        "fills-empty-source",
        # Word-level provenance
        "dictionary-version-may-differ",
        "yomitan-gloss",
        "single-source-user-authored",
        "reading-derived-from-furigana",
        # Repair rules
        "strip-arrows",
        "flatten-div-blocks",
        "join-speaker-lines",
        "strip-dash",
        "strip-nbsp",
        "strip-inline-readings",
        "ruby-reading-overridden",
        # Scope propagation and clean-run tagging (B12, issue #20)
        "scene-neighbour",
        "card-finished-clean",
    ]
    for badge in known_static_badges:
        explanation = badge_explanation(badge)
        assert explanation != badge, f"Badge {badge!r} has no explanation"


# --- guard_reason --------------------------------------------------------


def test_guard_reason_checked_by_default():
    w = PlannedWrite(
        note_id=1,
        field="Picture",
        op=OP_FILL,
        old_value="",
        new_value="<img>",
        default_checked=True,
    )
    assert guard_reason(w) == "Checked by default"


def test_guard_reason_for_guard_badges():
    cases = [
        ("divergent-parents", "Scene siblings disagree on audio/sentence — requires manual review"),
        ("far-from-source", "Scene parent is far away in time — may be a different scene"),
        (
            "fragment-not-recoverable",
            "The card's original fragment cannot be recovered from the expansion",
        ),
        (
            "cloze-anchor-not-unique",
            "The tested word appears more than once in the expanded sentence",
        ),
        ("stale-fragment-furigana-risk", "Expansion would leave an outdated ruby annotation"),
        (
            "paired-with-expansion",
            "This fill is linked to a guarded expansion — both deferred together",
        ),
        ("expansion-guard", "Expansion is guarded — review before applying"),
    ]
    for badge, expected_text in cases:
        w = PlannedWrite(
            note_id=1,
            field="Sentence",
            op=OP_EXPAND_SENTENCE,
            old_value="frag",
            new_value="scene",
            default_checked=False,
            badges=(badge,),
        )
        reason = guard_reason(w)
        assert reason == f"Unchecked by default: {expected_text}"


def test_guard_reason_for_config_disabled_fields():
    w_notes = PlannedWrite(
        note_id=1,
        field="Notes",
        op=OP_FILL,
        old_value="",
        new_value="text",
        default_checked=False,
    )
    assert (
        guard_reason(w_notes)
        == "Unchecked by default: Notes auto-check is disabled in configuration"
    )

    w_trans = PlannedWrite(
        note_id=1,
        field="Translation",
        op=OP_FILL,
        old_value="",
        new_value="text",
        default_checked=False,
    )
    assert (
        guard_reason(w_trans)
        == "Unchecked by default: Translation auto-check is disabled in configuration"
    )

    w_source = PlannedWrite(
        note_id=1,
        field="Source",
        op=OP_REPLACE_SOURCE,
        old_value="blob",
        new_value="real",
        default_checked=False,
    )
    assert (
        guard_reason(w_source)
        == "Unchecked by default: Source replacement is disabled in configuration"
    )

    w_expand = PlannedWrite(
        note_id=1,
        field="Sentence",
        op=OP_EXPAND_SENTENCE,
        old_value="frag",
        new_value="scene",
        default_checked=False,
    )
    assert (
        guard_reason(w_expand)
        == "Unchecked by default: Sentence expansion is disabled in configuration"
    )


def test_guard_reason_prefers_specific_guard_over_expansion_guard():
    w = PlannedWrite(
        note_id=1,
        field="Sentence",
        op=OP_EXPAND_SENTENCE,
        old_value="frag",
        new_value="scene",
        default_checked=False,
        badges=("expansion-guard", "fragment-not-recoverable"),
    )
    assert guard_reason(w) == (
        "Unchecked by default: The card's original fragment cannot be recovered from the expansion"
    )


def test_guard_reason_fallback():
    w = PlannedWrite(
        note_id=1,
        field="CustomField",
        op=OP_FILL,
        old_value="",
        new_value="val",
        default_checked=False,
    )
    assert guard_reason(w) == "Unchecked by default: guarded change — review before applying"
