"""Plan construction: the single source of truth for preview and apply.

Pure module: no anki/aqt imports.

A :class:`Plan` is a deterministic, versioned list of :class:`PlannedWrite`
plus per-candidate reasons for every note the add-on could not help. The
preview dialog and the applier consume the same plan, so what is shown is
exactly what is written.
"""

from __future__ import annotations

import bisect
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace

from .fieldconfig import (
    FIELD_NEVER_COPY,
    eligible_source,
    image_files,
    is_blob_source,
    is_empty,
    sound_files,
)
from .matching import (
    DAY_MS,
    DEFAULT_SCENE_FILL_FIELDS,
    DEFAULT_SCENE_OPTIN_FIELDS,
    DEFAULT_SCENE_REPLACE_FIELDS,
    MAX_WINDOW_MS,
    NoteRecord,
    SceneMatch,
    SceneParentIndex,
    agreement_groups,
    best_word_source,
    find_scene_parents,
    is_word_media,
    kana_spelling_candidates,
    rank_scene_parents,
    word_groups,
)
from .normalize import (
    REPAIR_RULES,
    RUBY_EXCLUDED_RULES,
    MorphToken,
    furigana_base,
    furigana_base_matches,
    is_empty_glossary_shell,
    kana_fold,
    norm_reading,
    norm_sentence_match,
    reading_from_furigana,
    repair_sentence,
    strip_renderable,
    word_key,
)

__all__ = [
    "OPS_TOUCHING_NONEMPTY",
    "PLAN_VERSION",
    "Plan",
    "PlannedWrite",
    "build_plan",
    "scope_from_selection",
]

PLAN_VERSION = 7

OP_FILL = "fill"
OP_REPLACE_SOURCE = "replace_source"
OP_EXPAND_SENTENCE = "expand_sentence"
# Markup-only repair: wrap the tested word in <b> (rendered text unchanged).
OP_BOLD_CLOZE_WORD = "bold_cloze_word"
# Subtitle-noise repair: line-tail continuation arrows (bulk-fix dimension
# 2; see QUALITY Q-D10 and normalize.repair_sentence).
OP_REPAIR_SENTENCE = "repair_sentence"
# Furigana regeneration (bulk-fix dimension 3): rebuild `Sentence
# (furigana)` from the note's own post-write Sentence through the installed
# Japanese Support add-on's reading engine (never a home-grown generator).
OP_REGENERATE_FURIGANA = "regenerate_furigana"
# Empty-glossary cleanup (QUALITY Q-D12, owner ask 2026-11-16): a definition
# field holding only empty container markup (the Youtan `<ol></ol>` shell)
# is cleared — the rendered text is empty either way.
OP_STRIP_EMPTY_GLOSSARY = "strip_empty_glossary"
# The ruby layer this op rebuilds (the deck's own field name convention).
FURIGANA_FIELD = "Sentence (furigana)"
# Expression spelling repair (bulk-fix dimension 4, self-oracled variant):
# rewrite a surface-form Expression to the dictionary form the note's own
# `furigana` field records (ひと握り under `一握[ひとにぎ]り` → 一握り).
OP_REPAIR_EXPRESSION = "repair_expression"
# Tag-set union from contributing sources; never removes a tag.
OP_ADD_TAGS = "add_tags"
OPS_TOUCHING_NONEMPTY = frozenset(
    {
        OP_REPLACE_SOURCE,
        OP_EXPAND_SENTENCE,
        OP_BOLD_CLOZE_WORD,
        OP_REPAIR_SENTENCE,
        OP_REGENERATE_FURIGANA,
        OP_REPAIR_EXPRESSION,
        OP_STRIP_EMPTY_GLOSSARY,
        OP_ADD_TAGS,
    }
)


@dataclass(frozen=True)
class PlannedWrite:
    """One field change. ``old_value`` empty <=> OP_FILL.

    Eight ops may write a non-empty value, each guarded: the two approved
    replaces (blob Source — the blob URL is recorded in the audit log;
    fragment Sentence — the fragment survives in the scene text and in
    cloze-* on sentence-mined cards), the markup-only bold repair
    (``strip_renderable`` of both values must be equal), the subtitle-noise
    repair (only the configured noise classes change, QUALITY Q-D10), the
    furigana regeneration (the ruby layer must carry exactly the sentence's
    characters, QUALITY Q-D11), the Expression spelling repair (the new value
    is the note's own furigana base, QUALITY Q-D12), the empty-glossary
    cleanup (clears only a definition shell that renders as nothing,
    QUALITY Q-D13), and the tags union (``new_value`` is a superset of
    ``old_value``). All carry badges.
    """

    note_id: int
    field: str
    op: str
    old_value: str
    new_value: str
    source_note_id: int | None = None
    default_checked: bool = False
    badges: tuple[str, ...] = ()
    window_ms: int | None = None
    group_id: str | None = None  # rows sharing a group are applied together


@dataclass
class Plan:
    version: int = PLAN_VERSION
    writes: tuple[PlannedWrite, ...] = ()
    reasons: dict[int, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def checked(self) -> tuple[PlannedWrite, ...]:
        return tuple(w for w in self.writes if w.default_checked)


def _fragment_recoverable(candidate: NoteRecord, cand_norm: str) -> bool:
    """True when cloze-* alone reconstructs the card's own Sentence.

    Only a badge label now: a completion keeps the fragment whether or not it
    is in cloze-* — the scene matcher guarantees the card's sentence is
    *contained* in the parent's, so replacing the Sentence with the parent's
    scene text never loses it (``fragment-kept-in-scene``). This predicate
    distinguishes the sentence-mined shape (cloze-* *is* the fragment,
    ``fragment-kept-in-cloze``) from the word-mined one.
    """
    parts = [
        strip_renderable(candidate.get("cloze-prefix")),
        strip_renderable(candidate.get("cloze-body")),
        strip_renderable(candidate.get("cloze-suffix")),
    ]
    if not any(parts):
        return False
    joined = norm_sentence_match("".join(parts))
    return bool(joined) and joined == cand_norm


def _shared_audio_parent(
    candidate: NoteRecord, audio_parents: dict[str, list[NoteRecord]]
) -> NoteRecord | None:
    """The sibling whose scene audio this card already carries.

    A card whose audio was filled from a sibling but whose Sentence was left
    as a fragment is the "skipped expansion" shape (the 飲食 card). Its audio
    file is byte-identical to that sibling's while its sentence is still a
    strict fragment of the sibling's full block. Such a card must stay
    expandable even though it now owns audio — otherwise a single skipped
    guarded row makes the card unfixable forever (QUALITY Q-A2).
    """
    own_audio = sound_files(candidate.get("Sentence Audio"))
    cand_norm = norm_sentence_match(candidate.get("Sentence"))
    if not own_audio or not cand_norm:
        return None
    seen: set[int] = set()
    for filename in own_audio:
        for parent in audio_parents.get(filename, ()):
            if parent.note_id == candidate.note_id or parent.note_id in seen:
                continue
            seen.add(parent.note_id)
            parent_norm = norm_sentence_match(parent.get("Sentence"))
            if parent_norm and parent_norm != cand_norm and cand_norm in parent_norm:
                return parent
    return None


def _unique_anchor(parent_value: str, cloze_body: str) -> bool:
    """The cloze body (the tested word) must occur exactly once in the new
    Sentence; otherwise the template's highlight would be ambiguous or lost."""
    if not cloze_body.strip():
        return False
    body = strip_renderable(cloze_body).strip()
    if not body:
        return False
    parent_plain = strip_renderable(parent_value)
    return parent_plain.count(body) == 1


_BOLD_RE = re.compile(r"<b>([^<]{1,80})</b>")


def _retarget_bold(parent_value: str, parent_body: str, child_body: str) -> tuple[str, str | None]:
    """Move the scene's bold markup from the parent's word to the child's.

    The deck convention bolds the mined word inside ``Sentence`` (e.g.
    ``先生は今 <b>診察</b>中ですので➡``). Copying a parent scene verbatim
    would leave the PARENT's word bolded on the child's card; the user's
    manual fix un-bolds the parent's word and bolds the child's cloze word
    instead. Both tags are formatting: no text is added or removed, and
    every change is logged with the old value.

    Returns ``(new_value, badge)``; badge is ``None`` when nothing changed,
    ``"stale-parent-bold"`` when the copy would keep the parent's word
    bolded, and the precise retarget badge otherwise.
    """
    parent_bold = _BOLD_RE.search(parent_value)
    value = parent_value
    unwrapped = False
    if parent_bold:
        pbody = parent_body.strip()
        if pbody and strip_renderable(parent_bold.group(1)) == strip_renderable(pbody).strip():
            value = parent_value.replace(parent_bold.group(0), parent_bold.group(1), 1)
            unwrapped = True
        elif (
            child_body.strip()
            and strip_renderable(parent_bold.group(1)) == strip_renderable(child_body).strip()
        ):
            # Bold already marks the child's word: keep the value as is.
            value = parent_value
        else:
            # Bold marks a different word: leave the parent value untouched
            # rather than guess which markup the card should carry.
            return parent_value, "stale-parent-bold"
    wrapped = False
    cbody = child_body.strip()
    if cbody and f"<b>{cbody}</b>" not in value and value.count(cbody) == 1:
        value = value.replace(cbody, f"<b>{cbody}</b>", 1)
        wrapped = True
    if value == parent_value:
        return parent_value, None
    if unwrapped and wrapped:
        return value, "bold-retargeted-to-cloze-word"
    return value, "parent-bold-removed" if unwrapped else "bold-added-to-cloze-word"


_KANJI_HINT_RE = re.compile(r"[㐀-\u4dbf\u4e00-\u9fff]")


def _mixed_form_candidates(expression: str, reading: str) -> list[str]:
    """Mixed-kana spellings of the headword: its leading kanji read as kana.

    Mined sentences often render the headword's leading kanji in kana
    (鬱病 with reading うつびょう appears as うつ病). Per-kanji readings are
    unknown offline, so every (reading-prefix, headword-suffix) pair is a
    candidate; the caller's sentence-occurrence oracle plus the uniqueness
    requirement filter the wrong ones out. Candidates must contain kanji:
    pure-kana strings would match unrelated common words.
    """
    hw = expression.strip()
    rd = kana_fold(reading.strip())
    if len(hw) < 2 or len(rd) < 2 or not _KANJI_HINT_RE.search(hw):
        return []
    out: list[str] = []
    for k in range(1, len(rd)):
        for m in range(1, len(hw)):
            cand = rd[:k] + hw[m:]
            if cand == hw or not _KANJI_HINT_RE.search(cand) or cand in out:
                continue
            out.append(cand)
    return out


def _wrap_first_occurrence(sentence: str, word: str) -> str | None:
    """``word`` wrapped in ``<b>`` at its first occurrence, or None if unsafe.

    Unsafe: the occurrence sits inside an HTML tag (e.g. an attribute
    value). The caller additionally verifies the wrapping is markup-only.
    """
    start = sentence.find(word)
    if start < 0:
        return None
    if sentence.rfind("<", 0, start) > sentence.rfind(">", 0, start):
        return None  # the occurrence lives inside a tag
    return f"{sentence[:start]}<b>{word}</b>{sentence[start + len(word) :]}"


_BOLD_TAG_RE = re.compile(r"<(?:b(?:\s|>)|strong\b)", re.IGNORECASE)
_OPEN_BOLD_RE = re.compile(r"<(?:b|strong)\b[^>]*>", re.IGNORECASE)


def _has_complex_bold_markup(text: str) -> bool:
    """True when ``text`` contains bold markup outside simple ``<b>…</b>`` spans.

    ``_BOLD_RE`` only matches ``<b>([^<]{1,80})</b>``; if the sentence carries
    ``<strong>``, attributed ``<b …>``, or HTML nested inside ``<b>`` (such as
    ``<b>foo<br>bar</b>``), rewriting bold spans could wrap a second ``<b>``
    instead of replacing the existing one. Under rule 5 (never guess), leave
    non-simple bold markup alone. Pure.
    """
    return len(_OPEN_BOLD_RE.findall(text)) != len(_BOLD_RE.findall(text))


#: Conservative word-vs-clause gate for expression-derived bold repair
#: (QUALITY Q-D3). The honest oracle is MeCab tokenization of the Expression
#: (BULK_FIX.md dimension 6); until then a clause-like Expression is
#: reported, never bolded — a false skip loses nothing, a wrong bold hides
#: An Expression must look like a word, not a clause: whitespace or sentence
#: punctuation reject it immediately; length is capped to protect
#: the sentence's structure while allowing legitimate long compound words.
MAX_EXPRESSION_CHARS = 25
_CLAUSE_PUNCT_RE = re.compile(r"[。、，．,.!?！？…]")
_KATAKANA_WORD_RE = re.compile(r"^[ァ-ヴー・]+$")
_CLAUSE_ENDINGS_RE = re.compile(
    r"(?:たいかな|ようになりたい|かもしれない|だろう|でしょうか|ですね|ですよ)$"
)


def _expression_is_word_like(
    expression: str,
    morph_tokenizer: Callable[[str], Sequence[MorphToken]] | None = None,
) -> bool:
    """Could this Expression be a single mined word or compound?

    Rejects whitespace, sentence punctuation, obvious clause endings, and
    anything longer than ``MAX_EXPRESSION_CHARS``. Legitimate long words
    (such as katakana compounds like ベーコンチーズバーガー) are accepted.
    """
    text = (expression or "").strip()
    if not text:
        return False
    if any(ch.isspace() for ch in text):
        return False
    if _CLAUSE_PUNCT_RE.search(text):
        return False
    if _CLAUSE_ENDINGS_RE.search(text):
        return False
    if morph_tokenizer is not None:
        try:
            tokens = morph_tokenizer(text)
            if tokens:
                has_clause_particles = any(
                    tok.pos in ("助詞", "助動詞")
                    and tok.surface in ("は", "が", "を", "たい", "かな", "だろう")
                    for tok in tokens
                )
                if has_clause_particles:
                    return False
                if all(tok.pos in ("名詞", "接頭詞", "接尾辞", "記号") for tok in tokens):
                    return len(text) <= MAX_EXPRESSION_CHARS
        except Exception:
            pass
    if _KATAKANA_WORD_RE.match(text):
        return len(text) <= MAX_EXPRESSION_CHARS
    return len(text) <= MAX_EXPRESSION_CHARS


def _is_listening(target: NoteRecord) -> bool:
    """The template's audio-only front — both paths (BULK_FIX.md).

    Verified against the card template 2026-11-16: the front removes the
    sentence views when (a) a ``listening`` tag token comes with usable
    audio (``Sentence Audio`` or ``Word Audio``), or (b) ``Definition``,
    ``Extended definition`` and ``Frequency`` are all empty while
    ``Sentence Audio`` is present (the legacy audio-only shape). Frequency
    without glosses is deliberately NOT listening — the template shows the
    normal sentence there.
    """
    tag = "listening" in {t.lower() for t in target.tags}
    sentence_audio = target.get("Sentence Audio")
    word_audio = target.get("Word Audio")
    if tag and not (is_empty(sentence_audio) and is_empty(word_audio)):
        return True
    def_val = target.get("Definition")
    ext_val = target.get("Extended definition")
    def_empty = is_empty(def_val) or is_empty_glossary_shell(def_val)
    ext_empty = is_empty(ext_val) or is_empty_glossary_shell(ext_val)
    return (
        def_empty
        and ext_empty
        and is_empty(target.get("Frequency"))
        and not is_empty(sentence_audio)
    )


#: Half-width katakana base characters (a subtitle dump frequently carries
#: them where the mined word / furigana is full-width).
_HALFWIDTH_KATAKANA = frozenset(
    chr(code) for code in range(0xFF61, 0xFFA0) if code not in (0xFF9E, 0xFF9F)
)
_HALFWIDTH_MARKS = "\uff9e\uff9f"


def _fold(text: str) -> str:
    """NFKC-fold for comparison (half-width katakana ↔ full-width).

    A subtitle dump can carry half-width katakana (``ｽﾍﾟｸﾄﾗﾑ``) while the
    mined word and its furigana are full-width (``スペクトラム``), so an exact
    substring search misses the tested word (author's 自閉症すぺくとらむ).
    Folding is used only to *compare*, never to rewrite: the sentence keeps
    its own characters. Pure.
    """
    return unicodedata.normalize("NFKC", text or "")


def _width_span_map(sentence: str) -> tuple[str, list[tuple[int, int]]]:
    """The NFKC-folded ``sentence`` plus, per folded character, its source span.

    A half-width katakana base and its voiced mark (``ﾎ`` + ``ﾟ``) fold to a
    single character, so the map is built a source cluster at a time: each
    folded character points at the original ``[start, end)`` it came from.
    Pure.
    """
    folded: list[str] = []
    spans: list[tuple[int, int]] = []
    index = 0
    length = len(sentence)
    while index < length:
        end = index + 1
        if (
            sentence[index] in _HALFWIDTH_KATAKANA
            and end < length
            and sentence[end] in _HALFWIDTH_MARKS
        ):
            end += 1
        piece = unicodedata.normalize("NFKC", sentence[index:end]) or sentence[index:end]
        for _ in piece:
            spans.append((index, end))
        folded.append(piece)
        index = end
    return "".join(folded), spans


def _word_span(sentence: str, word: str) -> tuple[int, int] | None:
    """The unique location of ``word`` in ``sentence`` under width folding.

    Returns the original ``[start, end)`` span, or ``None`` when the word does
    not occur or occurs more than once — ambiguity is never resolved by
    guessing. The span points at the sentence's *own* characters, so a bold
    inserted there keeps the rendered text byte-identical. Pure.
    """
    if not word:
        return None
    folded, spans = _width_span_map(sentence)
    target = _fold(word)
    if not target:
        return None
    first = folded.find(target)
    if first < 0 or folded.find(target, first + 1) >= 0:
        return None
    return spans[first][0], spans[first + len(target) - 1][1]


_BOLD_DELIM_CHARS = r"><\"'(),.!?:;~_\\/\-。、！？：；…―〜「」『』（）【】［］・➡⇒→\s"
_HTML_ENTITIES_LEAD_RE = re.compile(rf"^(?:&gt;|&lt;|&quot;|&amp;|&apos;|[{_BOLD_DELIM_CHARS}])+")
_HTML_ENTITIES_TRAIL_RE = re.compile(rf"(?:&gt;|&lt;|&quot;|&amp;|&apos;|[{_BOLD_DELIM_CHARS}])+$")


def _trim_bold_punctuation(sentence: str, wanted: set[str]) -> tuple[str, bool]:
    """Trim non-word punctuation and escaped entities out of <b>...</b> spans.

    When a subtitle dump or mining tool wraps the tested word plus a trailing or
    leading delimiter (e.g. ``<b>とんでもない></b>`` or ``<b>芸者&gt;</b>``),
    move those delimiters outside the bold tag so the bold wraps exactly the
    word. Preserves the exact occurrence the existing bold sits on even when
    the word occurs multiple times in the sentence. Markup-only:
    ``strip_renderable(new) == strip_renderable(old)``. Pure.
    """
    if not _BOLD_TAG_RE.search(sentence):
        return sentence, False
    changed = False

    def repl(match: re.Match[str]) -> str:
        nonlocal changed
        inner = match.group(1)
        if _fold(strip_renderable(inner).strip()) in wanted:
            return match.group(0)

        lead_match = _HTML_ENTITIES_LEAD_RE.match(inner)
        lead = lead_match.group(0) if lead_match else ""
        rest = inner[len(lead) :]
        trail_match = _HTML_ENTITIES_TRAIL_RE.search(rest)
        trail = trail_match.group(0) if trail_match else ""
        core = rest[: len(rest) - len(trail)] if trail else rest

        if core and _fold(strip_renderable(core).strip()) in wanted:
            changed = True
            return f"{lead}<b>{core}</b>{trail}"
        return match.group(0)

    trimmed = _BOLD_RE.sub(repl, sentence)
    return trimmed, changed


def _shares_kanji(a: str, b: str) -> bool:
    """True when two strings share at least one kanji character."""
    a_kanji = set(_KANJI_HINT_RE.findall(a or ""))
    b_kanji = set(_KANJI_HINT_RE.findall(b or ""))
    return bool(a_kanji and (a_kanji & b_kanji))


def _is_morph_match(
    span_text: str,
    expr: str,
    reading: str,
    morph_tokenizer: Callable[[str], Sequence[MorphToken]],
    furigana: str | None = None,
) -> bool:
    """True when morphological analysis identifies ``span_text`` as the card's word.

    Recognizes inflected forms (淀む -> よどん) and alternate-kanji spellings
    (日差し -> 日射し) without guessing. Pure.
    """
    if not span_text or not expr:
        return False
    try:
        tokens = morph_tokenizer(span_text)
        if tokens and tokens[0].pos in ("動詞", "形容詞", "名詞", "副詞", "連体詞"):
            tok = tokens[0]
            if len(tokens) > 1 and not all(
                t.pos == "助動詞"
                or (t.pos == "動詞" and t.lemma in ("れる", "られる", "せる", "させる", "得る"))
                for t in tokens[1:]
            ):
                return False
            target_reading = kana_fold(norm_reading(reading or ""))
            if len(target_reading) < 2 and furigana:
                target_reading = kana_fold(reading_from_furigana(furigana) or "")
            tok_lemma_norm = kana_fold(norm_reading(tok.lemma))
            expr_norm = kana_fold(norm_reading(expr))
            if tok.lemma == expr or tok_lemma_norm == expr_norm:
                return True
            if target_reading and len(target_reading) >= 2 and tok_lemma_norm == target_reading:
                return True
            if (
                target_reading
                and len(target_reading) >= 2
                and kana_fold(norm_reading(tok.reading)) == target_reading
                and _shares_kanji(tok.surface, expr)
            ):
                return True
    except Exception:
        pass
    return False


def _bold_normalized_sentence(
    sentence: str, candidates: list[str], body: str = ""
) -> tuple[str, str] | None:
    """Markup-only fix for a sentence whose bold is missing, foreign or doubled.

    The deck convention is exactly one bold item — the tested word. Cases
    seen on the author's cards: two bold items (校 and ぜひ), a block with no
    bold at all (the same-moment restore), and a bold on another word. The
    word is located under width folding (:func:`_word_span` — a subtitle dump
    may write half-width katakana, 自閉症すぺくとらむ), but the inserted bold
    wraps the sentence's own characters, so the rendered text is unchanged.
    Returns ``(new_value, chosen_word)``.
    """
    ordered = [c for c in (strip_renderable(cand).strip() for cand in candidates) if c]
    wanted = {_fold(c) for c in ordered}
    body_word = strip_renderable(body).strip()

    def locate(text: str) -> tuple[str, tuple[int, int]] | None:
        for candidate in ([body_word] if body_word else []) + ordered:
            span = _word_span(text, candidate)
            if span is not None:
                return candidate, span
        return None

    spans = _BOLD_RE.findall(sentence)
    if not spans:
        found = locate(sentence)
        if found is None:
            return None
        _word, (start, end) = found
        bolded = sentence[:start] + f"<b>{sentence[start:end]}</b>" + sentence[end:]
        return bolded, sentence[start:end]
    matching = [span for span in spans if _fold(strip_renderable(span).strip()) in wanted]
    if len(spans) > 1:
        if len(matching) != 1:
            return None
        keep = matching[0]
        seen = False

        def only_kept(match: re.Match) -> str:
            nonlocal seen
            if not seen and match.group(1) == keep:
                seen = True
                return match.group(0)
            return match.group(1)

        new_value = _BOLD_RE.sub(only_kept, sentence)
        return (new_value, keep) if new_value != sentence else None
    if matching:
        return None  # already one bold, on the tested word
    without_bold = _BOLD_RE.sub(lambda match: match.group(1), sentence)
    found = locate(without_bold)
    if found is None:
        return None
    _word, (start, end) = found
    new_value = without_bold[:start] + f"<b>{without_bold[start:end]}</b>" + without_bold[end:]
    return (new_value, without_bold[start:end]) if new_value != sentence else None


def _bold_repair_for(
    target: NoteRecord,
    rules: tuple[str, ...] = (),
    morph_tokenizer: Callable[[str], Sequence[MorphToken]] | None = None,
    batch_bold_inflected: bool = True,
) -> tuple[PlannedWrite | None, str | None, str | None]:
    """Locate the tested word in a bold-less ``Sentence``; markup-only fix.

    The deck convention bolds the mined word inside ``Sentence``; the front
    template keys on it (accent highlight — and its cloze-fallback rebuilds
    the front from cloze-* exactly when no bold term exists). Cards whose
    cloze-* hold dictionary-entry text (mining bug) have no bold; the word
    may still occur as a different surface form. Listening cards (audio-only
    front) are skipped: nothing displays the sentence.

    Tier 1: the cloze body occurs exactly once. Tier 2: the Expression
    itself occurs verbatim exactly once (the mined word is missing from
    cloze-*). Tier 3: a mixed-kana spelling derived from (Expression,
    reading) occurs exactly once (鬱病 -> うつ病). Tiers 2–3 require a
    word-like Expression (:func:`_expression_is_word_like`): a clause-like
    value such as 漢字は読めるようになりたいかな is reported
    (``expression-looks-like-a-clause``), never bolded. Ambiguity — more
    than one occurrence — is never resolved by guessing.

    ``rules`` (the configured repair rules) are applied to the sentence
    *before* the word is located: the bold write replaces the field, so it
    must leave no subtitle arrows behind — otherwise the arrow cleaner, which
    defers to a queued bold write, would be skipped and the arrows would
    survive (author bug report: 手を出す-shaped cards).

    Returns ``(write, reason, detail)``: the write is markup-only
    (``strip_renderable`` equality is verified before it is returned);
    ``reason``/``detail`` explain a miss for the report.
    """
    sentence = target.get("Sentence")
    # The apply-time check compares against the field as it stands *now* (it
    # still carries the subtitle arrows); the word is located in the cleaned
    # text, so new_value strips them and old_value must not.
    raw_sentence = sentence
    # <br> must not read as a bold tag: match <b>/<b attrs>/<strong only.
    if is_empty(sentence):
        return None, None, None
    if _BOLD_TAG_RE.search(sentence):
        # Bold present: normally left alone — unless it is foreign or
        # duplicated, in which case normalise the markup only (rendered text
        # unchanged) so exactly the tested word carries the bold.
        if _is_listening(target) or _has_complex_bold_markup(sentence):
            return None, None, None
        expr_raw = strip_renderable(target.get("Expression")).strip()
        word_like_expr = _expression_is_word_like(expr_raw)
        reading = kana_fold(norm_reading(target.get("reading") or ""))
        if len(reading) < 3:
            furi_reading = kana_fold(reading_from_furigana(target.get("furigana")) or "")
            if len(furi_reading) > len(reading):
                reading = furi_reading
        recognized = [strip_renderable(target.get("cloze-body")).strip()]
        if word_like_expr:
            recognized.append(expr_raw)
            recognized += kana_spelling_candidates(
                target.get("Expression") or "", target.get("furigana") or ""
            )
            recognized += _mixed_form_candidates(
                target.get("Expression") or "", target.get("reading") or ""
            )
            if reading:
                recognized.append(reading)
        span_list = _BOLD_RE.findall(sentence)
        wanted = {_fold(strip_renderable(cand).strip()) for cand in recognized if cand}
        if morph_tokenizer is not None:
            for span in span_list:
                clean_span = strip_renderable(span).strip()
                if _is_morph_match(
                    clean_span,
                    expr_raw,
                    target.get("reading") or "",
                    morph_tokenizer,
                    target.get("furigana"),
                ):
                    wanted.add(_fold(clean_span))
        if len(span_list) == 1 and _fold(strip_renderable(span_list[0]).strip()) in wanted:
            # Already exactly one bold, and it wraps the tested word: done.
            return None, None, None
        trimmed_sentence, bold_trimmed = _trim_bold_punctuation(sentence, wanted)
        trimmed_spans = _BOLD_RE.findall(trimmed_sentence)
        if (
            bold_trimmed
            and len(trimmed_spans) == 1
            and _fold(strip_renderable(trimmed_spans[0]).strip()) in wanted
        ):
            work_sentence = trimmed_sentence
            noise_rules: tuple[str, ...] = ()
            if rules:
                cleaned, noise_rules = repair_sentence(trimmed_sentence, rules)
                if noise_rules:
                    work_sentence = cleaned
            if strip_renderable(work_sentence) == strip_renderable(raw_sentence):
                write = PlannedWrite(
                    note_id=target.note_id,
                    field="Sentence",
                    op=OP_BOLD_CLOZE_WORD,
                    old_value=raw_sentence,
                    new_value=work_sentence,
                    default_checked=True,
                    badges=("bold-trimmed-punctuation", *noise_rules),
                )
                return write, None, None

        placeable = _placeable_bold_candidates(target)
        work_sentence = trimmed_sentence if bold_trimmed else sentence
        noise_rules: tuple[str, ...] = ()
        if rules:
            cleaned, noise_rules = repair_sentence(work_sentence, rules)
            if noise_rules:
                work_sentence = cleaned
        normalised = _bold_normalized_sentence(
            work_sentence,
            placeable,
            strip_renderable(target.get("cloze-body")),
        )
        if normalised is None:
            return None, None, None
        new_value, _chosen = normalised
        if strip_renderable(new_value) != strip_renderable(work_sentence):
            return None, None, None
        badges = ["bold-normalized-to-one"]
        if bold_trimmed:
            badges.append("bold-trimmed-punctuation")
        badges.extend(noise_rules)
        write = PlannedWrite(
            note_id=target.note_id,
            field="Sentence",
            op=OP_BOLD_CLOZE_WORD,
            old_value=raw_sentence,
            new_value=new_value,
            default_checked=True,
            badges=tuple(badges),
        )
        return write, None, None
    if _is_listening(target):
        return None, None, None
    noise_rules: tuple[str, ...] = ()
    if rules:
        cleaned, noise_rules = repair_sentence(sentence, rules)
        if noise_rules:
            sentence = cleaned
    body = strip_renderable(target.get("cloze-body")).strip()
    expr = strip_renderable(target.get("Expression")).strip()

    # Tier 1: the mined word (cloze body) occurs exactly once.
    if body:
        count = sentence.count(body)
        if count > 1:
            return None, "cloze-word-ambiguous", f"cloze word '{body}' occurs {count} times"
        if count == 1:
            new_value = _wrap_first_occurrence(sentence, body)
            if new_value is not None and strip_renderable(new_value) == strip_renderable(sentence):
                write = PlannedWrite(
                    note_id=target.note_id,
                    field="Sentence",
                    op=OP_BOLD_CLOZE_WORD,
                    old_value=raw_sentence,
                    new_value=new_value,
                    default_checked=True,
                    badges=("bold-added-to-cloze-word", *noise_rules),
                )
                return write, None, None
            return (
                None,
                "cloze-word-not-in-sentence",
                f"cloze word '{body}' has no anchor outside HTML markup",
            )

    # Tiers 2–3 speak for the Expression and must never bold a clause.
    if not expr:
        return None, None, None
    if not _expression_is_word_like(expr):
        if sentence.count(expr) == 1:
            return (
                None,
                "expression-not-word-like",
                f"expression '{expr}' is not word-like enough to bold"
                " (a clause or over-long) — left alone",
            )
        return None, None, None

    # Tier 2: the Expression itself occurs verbatim exactly once.
    verbatim = sentence.count(expr)
    if verbatim > 1:
        return None, "cloze-word-ambiguous", f"expression '{expr}' occurs {verbatim} times"
    if verbatim == 1:
        new_value = _wrap_first_occurrence(sentence, expr)
        if new_value is not None and strip_renderable(new_value) == strip_renderable(sentence):
            write = PlannedWrite(
                note_id=target.note_id,
                field="Sentence",
                op=OP_BOLD_CLOZE_WORD,
                old_value=raw_sentence,
                new_value=new_value,
                default_checked=True,
                badges=("bold-added-to-expression", *noise_rules),
            )
            return write, None, None

    # Tier 3: reading-derived mixed-kana surface forms.
    hits = [
        (cand, sentence.count(cand))
        for cand in _mixed_form_candidates(target.get("Expression"), target.get("reading"))
        if cand in sentence
    ]
    single = [cand for cand, n in hits if n == 1]
    multi = [(cand, n) for cand, n in hits if n > 1]
    if len(single) == 1 and not multi:
        new_value = _wrap_first_occurrence(sentence, single[0])
        if new_value is not None and strip_renderable(new_value) == strip_renderable(sentence):
            write = PlannedWrite(
                note_id=target.note_id,
                field="Sentence",
                op=OP_BOLD_CLOZE_WORD,
                old_value=raw_sentence,
                new_value=new_value,
                default_checked=True,
                badges=("bold-added-to-surface-form", *noise_rules),
            )
            return write, None, None
    if hits:
        listed = ", ".join(f"'{cand}' ×{n}" for cand, n in sorted(hits))
        return None, "cloze-word-ambiguous", f"surface-form candidates: {listed}"

    # Tier 4: the word written entirely in kana — the note's own reading is
    # the oracle (予て, reading かねて, appears as かねて in the sentence;
    # the author's own card). Only readings of three kana or more are used:
    # shorter readings (して, から) match too much to bold without a guess.
    # Ambiguity is reported, never resolved.
    reading = kana_fold(norm_reading(target.get("reading") or ""))
    if len(reading) < 3:
        reading = kana_fold(reading_from_furigana(target.get("furigana")) or "")
    if len(reading) >= 3:
        count = sentence.count(reading)
        if count > 1:
            return None, "cloze-word-ambiguous", f"reading '{reading}' occurs {count} times"
        if count == 1:
            new_value = _wrap_first_occurrence(sentence, reading)
            if new_value is not None and strip_renderable(new_value) == strip_renderable(sentence):
                write = PlannedWrite(
                    note_id=target.note_id,
                    field="Sentence",
                    op=OP_BOLD_CLOZE_WORD,
                    old_value=raw_sentence,
                    new_value=new_value,
                    default_checked=True,
                    badges=("bold-added-to-reading-form", *noise_rules),
                )
                return write, None, None

    # Tier 3b: the note's own furigana, spelled with some kanji written in
    # kana (類い希 -> 類いまれ — the author's card). Every per-kanji mix is a
    # candidate; exactly one occurrence is required, as everywhere else.
    for candidate in kana_spelling_candidates(
        target.get("Expression") or "", target.get("furigana") or ""
    ):
        if candidate == expr or len(candidate) < 2:
            continue
        count = sentence.count(candidate)
        if count == 1:
            new_value = _wrap_first_occurrence(sentence, candidate)
            if new_value is not None and strip_renderable(new_value) == strip_renderable(sentence):
                write = PlannedWrite(
                    note_id=target.note_id,
                    field="Sentence",
                    op=OP_BOLD_CLOZE_WORD,
                    old_value=raw_sentence,
                    new_value=new_value,
                    default_checked=True,
                    badges=("bold-added-to-kana-mix", *noise_rules),
                )
                return write, None, None
        elif count > 1:
            return (
                None,
                "cloze-word-ambiguous",
                f"spelling '{candidate}' occurs {count} times",
            )

    # Tier 5: inflected / alternate-kanji matching via morphological analysis (B10, Issue #1).
    if (
        batch_bold_inflected
        and morph_tokenizer is not None
        and expr
        and _expression_is_word_like(expr)
    ):
        try:
            clean_sent = strip_renderable(sentence)
            tokens = morph_tokenizer(clean_sent)
            target_reading = kana_fold(norm_reading(target.get("reading") or ""))
            if len(target_reading) < 2:
                target_reading = kana_fold(reading_from_furigana(target.get("furigana")) or "")
            expr_norm = kana_fold(norm_reading(expr))
            content_matches: list[tuple[MorphToken, str]] = []
            for idx, tok in enumerate(tokens):
                if tok.pos not in ("動詞", "形容詞", "名詞", "副詞", "連体詞"):
                    continue
                tok_lemma_norm = kana_fold(norm_reading(tok.lemma))
                matches = (
                    tok.lemma == expr
                    or tok_lemma_norm == expr_norm
                    or (
                        bool(target_reading)
                        and len(target_reading) >= 2
                        and tok_lemma_norm == target_reading
                    )
                    or (
                        bool(target_reading)
                        and len(target_reading) >= 2
                        and kana_fold(norm_reading(tok.reading)) == target_reading
                        and _shares_kanji(tok.surface, expr)
                    )
                )
                if matches:
                    full_surface = tok.surface
                    next_idx = idx + 1
                    while next_idx < len(tokens):
                        ntok = tokens[next_idx]
                        if ntok.pos == "助動詞" or (
                            ntok.pos == "動詞"
                            and ntok.lemma in ("れる", "られる", "せる", "させる", "得る")
                        ):
                            full_surface += ntok.surface
                            next_idx += 1
                        else:
                            break
                    content_matches.append((tok, full_surface))

            if len(content_matches) > 1:
                return (
                    None,
                    "cloze-word-ambiguous",
                    f"multiple inflected tokens for '{expr}' in sentence",
                )
            if len(content_matches) == 1:
                chosen_surface = content_matches[0][1]
                count = sentence.count(chosen_surface)
                if count == 1:
                    new_value = _wrap_first_occurrence(sentence, chosen_surface)
                    if new_value is not None and strip_renderable(new_value) == strip_renderable(
                        sentence
                    ):
                        write = PlannedWrite(
                            note_id=target.note_id,
                            field="Sentence",
                            op=OP_BOLD_CLOZE_WORD,
                            old_value=raw_sentence,
                            new_value=new_value,
                            default_checked=True,
                            badges=("bold-added-to-inflected-form", *noise_rules),
                        )
                        return write, None, None
                elif count > 1:
                    return (
                        None,
                        "cloze-word-ambiguous",
                        f"inflected form '{chosen_surface}' occurs {count} times",
                    )
        except Exception:
            pass

    # Expression absent (inflected or a mis-mine): silent when nothing was
    # mined — the expected, documented class-C outcome (BULK_FIX slice 8) —
    # and a reported anomaly when the cloze body was present but junk.
    if not body:
        return None, None, None
    return (
        None,
        "cloze-word-not-in-sentence",
        f"cloze word '{body}' and its reading-derived forms never occur in it",
    )


def _sentence_repair_write(
    target: NoteRecord, rules: tuple[str, ...]
) -> tuple[PlannedWrite | None, str | None, str | None]:
    """Rewrite subtitle noise in the target's own ``Sentence``.

    Bulk-fix dimension 2: line-tail continuation arrows are the noise;
    bold markup, sound tags, images, brackets and every spoken character
    pass through untouched (:func:`normalize.repair_sentence`).

    Guards (QUALITY Q-D10):
    - the tested word (``cloze-body``) must occur exactly as often after the
      repair as before — the repair must not create or destroy an anchor;
    - the caller must not already queue another ``Sentence`` write for this
      note — expansion and bold own the field first.

    Returns ``(write, reason, detail)``.
    """
    sentence = target.get("Sentence")
    if is_empty(sentence):
        return None, None, None
    new_value, applied = repair_sentence(sentence, rules)
    if new_value == sentence:
        return None, None, None
    body = strip_renderable(target.get("cloze-body")).strip()
    if body:
        old_plain = strip_renderable(sentence)
        if old_plain.count(body) != strip_renderable(new_value).count(body):
            return (
                None,
                "repair-anchor-risk",
                f"the repair would change how often '{body}' occurs in Sentence",
            )
    return (
        PlannedWrite(
            note_id=target.note_id,
            field="Sentence",
            op=OP_REPAIR_SENTENCE,
            old_value=sentence,
            new_value=new_value,
            default_checked=True,
            badges=applied,
        ),
        None,
        None,
    )


def _ruby_repair_rules(rules: tuple[str, ...]) -> tuple[str, ...]:
    """The rules that may rewrite the ruby ("Sentence (furigana)").

    Every call site that repairs the ruby passes this instead of ``rules``:
    ``strip-dash`` is Sentence-field-only — the owner asked that the stray
    bars be removed "only inside the sentence field, not everywhere", so the
    ruby keeps them (it is derived from the sentence and must describe the
    text the card ends up with — Q-D15).
    """
    return tuple(rule for rule in rules if rule not in RUBY_EXCLUDED_RULES)


def _ruby_repair_write(
    target: NoteRecord, rules: tuple[str, ...]
) -> tuple[PlannedWrite | None, str | None, str | None]:
    """Strip subtitle noise from the target's own ``Sentence (furigana)``.

    Owner bug report 2026-11-16: the arrows also sit in the ruby field — the
    card shows them even after the Sentence is cleaned. Same rule set, same
    determinism; the ruby's annotations pass through untouched (the arrow
    rule only removes line-tail arrows). The ``strip-dash`` rule is excluded
    — it is Sentence-field-only (``RUBY_EXCLUDED_RULES``).

    Guards: the field must be non-empty and actually change; a queued
    furigana regeneration or scene fill of the field wins (it replaces the
    whole value). Returns ``(write, reason, detail)``.
    """
    value = target.get(FURIGANA_FIELD)
    if is_empty(value):
        return None, None, None
    new_value, applied = repair_sentence(value, _ruby_repair_rules(rules))
    if new_value == value:
        return None, None, None
    return (
        PlannedWrite(
            note_id=target.note_id,
            field=FURIGANA_FIELD,
            op=OP_REPAIR_SENTENCE,
            old_value=value,
            new_value=new_value,
            default_checked=True,
            badges=(*applied, "ruby"),
        ),
        None,
        None,
    )


def _furigana_write_for(
    target: NoteRecord,
    generator,
    target_writes: list[PlannedWrite],
) -> tuple[PlannedWrite | None, str | None, str | None]:
    """Rebuild the target's ``Sentence (furigana)`` from its own Sentence.

    Bulk-fix dimension 3: the ruby layer must describe the sentence the
    plan will leave behind, so the generator input is the *post-write*
    Sentence — a queued repair/expansion/bold write feeds its new value
    (the repair→furigana ordering rule of BULK_FIX.md, enforced in one
    plan). The value itself comes from the installed Japanese Support
    add-on's reading engine; this planner never generates ruby itself.

    Guards (QUALITY Q-D11):
    - the caller must not already queue another ``Sentence (furigana)``
      write for this note — a scene fill owns the field first;
    - the engine's output must be non-empty (an engine failure is a
      reason, never a guessed write);
    - the generated ruby's base text must carry exactly the sentence's
      characters (:func:`normalize.furigana_base_matches`).

    Returns ``(write, reason, detail)``.
    """
    sentence = target.get("Sentence")
    if is_empty(sentence):
        return None, None, None
    # The generator input must be the sentence that will actually be written,
    # so a *checked* Sentence write (repair/expansion/bold) feeds its new
    # value. A guarded (unchecked) expansion never applies: using it would
    # generate a ruby for a sentence the card does not end up with, and the
    # ruby would then be applied checked onto a different text (author's
    # 高鳴る: the scene block's ruby landed on the card's own sentence).
    pending = next(
        (w for w in target_writes if w.field == "Sentence" and w.default_checked),
        None,
    )
    if pending is not None:
        sentence = pending.new_value
    try:
        generated = generator(sentence)
    except Exception as exc:  # the engine subprocess is outside our control
        return None, "furigana-generation-failed", f"reading engine raised: {exc}"
    if not (generated or "").strip():
        return (
            None,
            "furigana-generation-failed",
            "the reading engine produced nothing for this sentence",
        )
    current = target.get(FURIGANA_FIELD) or ""
    # A ruby that no longer describes its sentence (the mining junk
    # この‐たび【…】, or a block copied earlier) is stale: regenerate it in
    # place rather than leaving the mismatch (author report on 此の度).
    # Guard (owner report 2026-11-21): never overwrite user-authored ruby context
    # (speaker tags like （炭治郎）, hints, or asides) on existing unedited sentences.
    if bool(current.strip()) and pending is None:
        base = furigana_base(current)
        ruby_speaker = bool(re.search(r"[（(][^()（）\n]{1,16}[)）]", base))
        sent_speaker = bool(re.search(r"[（(][^()（）\n]{1,16}[)）]", sentence))
        norm_sent = norm_sentence_match(sentence)
        norm_base = norm_sentence_match(base)
        if (ruby_speaker and not sent_speaker) or (
            len(norm_base) > len(norm_sent) and norm_sent in norm_base
        ):
            return None, None, None

    stale = bool(current.strip()) and not furigana_base_matches(current, sentence)
    if generated == current and not stale:
        return None, None, None
    if not furigana_base_matches(generated, sentence):
        return (
            None,
            "furigana-base-mismatch",
            "generated ruby does not carry the sentence's characters",
        )
    badges = ["furigana-regenerated"]
    if any(w.field == "Sentence" for w in target_writes):
        badges.append("after-sentence-write")
    return (
        PlannedWrite(
            note_id=target.note_id,
            field=FURIGANA_FIELD,
            op=OP_REGENERATE_FURIGANA,
            old_value=current,
            new_value=generated,
            default_checked=True,
            badges=tuple(badges),
        ),
        None,
        None,
    )


def _completes_or_normalizes_expression(expression: str, base: str) -> tuple[bool, str]:
    """Check if base is a valid kanji completion or dictionary lemma normalization.

    Returns (is_valid, kind) where kind is "completion" or "normalization".
    - "completion": base adds kanji that expression wrote in kana (ひと握り → 一握り).
    - "normalization": base is the uninflected dictionary headword sharing the same
      kanji stem in identical order (倍にして → 倍にする, 決まってる → 決まる).
    Refuses kanji-dropping kana preference (気付き → 気づき), different-kanji
    substitutions (美味い → 上手い), and reversed kanji order (花火 → 火花).
    """
    expr_kanji = _KANJI_HINT_RE.findall(expression)
    base_kanji = _KANJI_HINT_RE.findall(base)
    set_expr = set(expr_kanji)
    set_base = set(base_kanji)

    # 1. Kanji completion: base strictly extends expression's kanji set
    if set_expr < set_base:
        return True, "completion"

    # 2. Inflection normalization: same kanji set in the same order, sharing the initial stem
    if (
        set_expr == set_base
        and expr_kanji == base_kanji
        and expression
        and base
        and expression[0] == base[0]
    ):
        return True, "normalization"

    return False, ""


def _expression_repair_for(
    target: NoteRecord,
) -> tuple[PlannedWrite | None, str | None, str | None]:
    """Rewrite a surface-form ``Expression`` to its recorded dictionary form.

    Bulk-fix dimension 4 (FUTURE_FEATURES §F2): the mining keeps the
    subtitle's mixed-kana spelling (ひと握り) while the card's own
    ``furigana`` field records the dictionary form the Yomitan lookup
    resolved to (``一握[ひとにぎ]り``). The furigana base IS the oracle —
    no dictionary seam and no hand-written kana→kanji table.

    Also handles inflected surface forms (倍にして → 倍にする, 決まってる → 決まる):
    normalizes the Expression to the uninflected dictionary lemma recorded in
    furigana when the reading matches and the kanji stem is preserved.

    Guards (QUALITY Q-D12) — every failure is a reported reason, never a
    guess:

    - the furigana must be usable and ``reading_from_furigana(furigana)``
      must equal the note's normalized ``reading`` field: the furigana must
      describe *this* word, not another entry;
    - the base must differ from the Expression (the inert, common case),
      contain kanji, be word-like (the Q-D3 gate) and be a valid kanji
      completion or inflection normalization (``_completes_or_normalizes_expression``):
      it may add missing kanji or normalize inflection endings, never drop
      or substitute kanji — that is what refuses kana-preference (気付き → 気づき)
      and homophone (美味い → 上手い) classes;
    - the caller queues it only when no word-level write is queued for the
      note — those values come from the old spelling's word group and must
      not land in the same run as the rewrite (F2 risk 4).

    Returns ``(write, reason, detail)``; a missing or empty furigana is the
    silent no-op, not an anomaly.
    """
    expression = strip_renderable(target.get("Expression")).strip()
    furigana = target.get("furigana")
    base = furigana_base(furigana)
    if not expression or not base:
        return None, None, None
    if base == expression:
        return None, None, None  # the furigana already describes the mined spelling
    derived = reading_from_furigana(furigana)
    reading = norm_reading(target.get("reading"))
    if derived is None or not reading or derived != reading:
        return (
            None,
            "expression-repair-reading-mismatch",
            f"furigana base '{base}' does not read as the note's reading '{target.get('reading')}'",
        )
    if not _KANJI_HINT_RE.search(base):
        return (
            None,
            "expression-repair-no-kanji-form",
            f"furigana base '{base}' has no kanji spelling to rewrite to",
        )
    if not _expression_is_word_like(base):
        return (
            None,
            "expression-not-word-like",
            f"furigana base '{base}' is not word-like enough to become the Expression",
        )
    valid, kind = _completes_or_normalizes_expression(expression, base)
    if not valid:
        return (
            None,
            "expression-repair-not-a-kanji-completion",
            f"furigana base '{base}' does not complete or normalize Expression '{expression}'",
        )
    badges = (
        ("expression-repaired-from-furigana", "expression-normalized-to-lemma")
        if kind == "normalization"
        else ("expression-repaired-from-furigana",)
    )
    return (
        PlannedWrite(
            note_id=target.note_id,
            field="Expression",
            op=OP_REPAIR_EXPRESSION,
            old_value=target.get("Expression"),
            new_value=base,
            default_checked=True,
            badges=badges,
        ),
        None,
        None,
    )


def _empty_glossary_writes(
    target: NoteRecord, cfg: dict, filled_fields: set[str]
) -> list[PlannedWrite]:
    """Clear definition fields holding nothing but empty container markup.

    Owner ask 2026-11-16: `Definition` / `Extended definition` show shells
    like ``<div class="yomitan-glossary"><ol></ol></div>`` when the mined
    dictionary entry had no body. When a sibling value exists the *fill*
    handles the field in the same run (it writes through the shell); this op
    is the fallback for a shell with nothing to fill it with.

    The guard is pure and absolute
    (:func:`normalize.is_empty_glossary_shell`): any media or visible text
    means no write, so a real definition is never touched.
    """
    fields = tuple(cfg.get("definition_fields", ("Definition", "Extended definition")))
    out: list[PlannedWrite] = []
    for name in fields:
        if name in filled_fields:
            continue
        value = target.get(name)
        if not is_empty_glossary_shell(value):
            continue
        out.append(
            PlannedWrite(
                note_id=target.note_id,
                field=name,
                op=OP_STRIP_EMPTY_GLOSSARY,
                old_value=value,
                new_value="",
                default_checked=True,
                badges=("cleared-empty-glossary",),
            )
        )
    return out


def _episode_key(source: str) -> str | None:
    """The episode a timestamped ``Source`` names, or None.

    ``Dr. Rintaro Episode 7 (1).mp4 (15m20s)`` -> ``Dr. Rintaro Episode
    7 (1).mp4``. A blob link names no episode (the video is opaque), so
    word-anchored recovery refuses those cards rather than guess.
    """
    text = (source or "").strip()
    if not text or "asbplayer" in text.lower():
        return None
    match = re.search(r"^(.*?\.(?:mp4|mkv|webm|srt|ass|m4a|mp3|wav|flac))\s*\(", text, re.I)
    if match:
        return match.group(1).strip()
    return None


def _cached_norm_sentence_match(record: NoteRecord, cache: dict[int, str] | None) -> str:
    if cache is None:
        return norm_sentence_match(record.get("Sentence"))
    cached = cache.get(record.note_id)
    if cached is None:
        cached = norm_sentence_match(record.get("Sentence"))
        cache[record.note_id] = cached
    return cached


def _word_recovered_scene_writes(
    target: NoteRecord,
    episodes: dict[str, list[NoteRecord]],
    cfg: dict,
    cache: dict[int, str] | None = None,
) -> tuple[list[PlannedWrite], str | None]:
    """Recover a scene by the *word* when the card's own sentence is wrong.

    The author's 類い希 card carries the wrong subtitle block and never
    matches a parent, yet its word (written 類いまれ) is plainly in the scene
    the same episode mined elsewhere. Recovery therefore anchors on the word,
    not on the card's sentence:

    - only when the card is still missing its ``Sentence Audio`` (QUALITY
      Q-D16: a card that already owns its audio is a scene unit and is never
      overwritten by another scene);
    - every spelling of the word from its own furigana is tried
      (:func:`matching.kana_spelling_candidates`), plus the cloze body and
      the reading;
    - each candidate must occur **exactly once** in a same-episode,
      audio-bearing card's sentence;
    - at least **two** such cards must agree on the sentence byte-identically
      or by nested clipping — one card alone is never an automatic write.

    Returns ``(writes, reason)``; writes are checked and carry
    ``recovered-sentence``.
    """
    replace_fields = tuple(cfg.get("scene_replace_fields", DEFAULT_SCENE_REPLACE_FIELDS))
    never_copy = FIELD_NEVER_COPY | frozenset(cfg.get("never_copy_fields", ()))
    if (
        "Sentence" not in replace_fields
        or "Sentence" in never_copy
        or not is_empty(target.get("Sentence Audio"))
    ):
        return [], "no-parent-that-fills"

    episode = _episode_key(target.get("Source"))
    if episode is not None:
        pools = episodes.get(episode, [])
    else:
        # The target names no episode (a bare blob link). The candidates must
        # still agree on ONE episode themselves — the same scene was mined
        # into several cards of a single video — or the recovery is refused
        # (author's 此の度: the 美術/受賞 block, all from Episode 7).
        pools = [record for group in episodes.values() for record in group]
    candidates: list[str] = []
    for cand in kana_spelling_candidates(
        target.get("Expression") or "", target.get("furigana") or ""
    ):
        if cand and cand not in candidates:
            candidates.append(cand)
    body = strip_renderable(target.get("cloze-body")).strip()
    if body and body not in candidates:
        candidates.append(body)
    reading = kana_fold(norm_reading(target.get("reading") or ""))
    if len(reading) >= 2 and reading not in candidates:
        candidates.append(reading)

    # Recovery is only for a *mis-mine*: the card's own sentence must not
    # already contain the word (or must be a dictionary-entry line, the
    # author's 此の度 — この‐たび【×此の度】). Otherwise a completed card
    # would be re-expanded on every run, and "run twice = no-op" breaks.
    sentence = target.get("Sentence") or ""
    looks_like_dictionary = bool(re.search(r"[【】]|‐|〔|⦅", sentence))
    # kana-fold both sides: the Expression may be written すぺくとらむ while
    # the sentence has スペクトラム — the word IS present, so the sentence is
    # not a mis-mine and must not be replaced by some other scene that merely
    # mentions it (author's 自閉症すぺくとらむ).
    folded_sentence = kana_fold(norm_sentence_match(sentence))
    word_present = any(
        cand and kana_fold(norm_sentence_match(cand)) in folded_sentence for cand in candidates
    )
    if not looks_like_dictionary and word_present:
        return [], "no-parent-that-fills"

    # Group by the spelling that matched, not by the whole sentence: the
    # same episode's blocks are clipped differently (診断 vs 今回), and the
    # tighter clip is the right one — nesting is the agreement signal.
    hits: dict[str, list[tuple[NoteRecord, str]]] = {}
    for other in pools:
        if other.note_id == target.note_id:
            continue
        sentence = other.get("Sentence") or ""
        for cand in candidates:
            if len(cand) < 2 or sentence.count(cand) != 1:
                continue
            hits.setdefault(cand, []).append((other, sentence))
            break
    if not hits:
        return [], "no-parent-that-fills"
    # Agreement is identity first, nested clips second: several cards that
    # mined the same subtitle block carry byte-identical Sentences (the
    # strong signal); cards that clipped the same block differently nest.
    # A word that also appears in a genuinely different scene is refused.
    best_cand: str | None = None
    members: list[tuple[NoteRecord, str]] = []
    for cand, cand_members in hits.items():
        by_text: dict[str, list[tuple[NoteRecord, str]]] = {}
        for record, sentence in cand_members:
            by_text.setdefault(_cached_norm_sentence_match(record, cache), []).append(
                (record, sentence)
            )
        for _text, same in by_text.items():
            if len(same) > len(members):
                best_cand, members = cand, same
    if best_cand is None or len(members) < 2:
        # No byte-identical pair: accept nested clips of one block instead.
        for cand, cand_members in hits.items():
            if len(cand_members) < 2:
                continue
            texts = [_cached_norm_sentence_match(record, cache) for record, _ in cand_members]
            if all(left in right or right in left for left in texts for right in texts):
                best_cand, members = cand, cand_members
                break
    if best_cand is None or len(members) < 2:
        return [], "no-parent-that-fills"
    # Every matched card must come from ONE identifiable episode; a word that
    # also occurs in another show's subtitles would otherwise pull the wrong
    # scene onto the card.
    member_episodes = {_episode_key(record.get("Source")) for record, _ in members}
    member_episodes.discard(None)
    if len(member_episodes) != 1:
        return [], "no-parent-that-fills"
    representative, best_sentence = min(members, key=lambda item: len(item[1]))
    written = best_cand
    repair_rules = tuple(
        rule for rule in cfg.get("sentence_repair_rules", ()) if rule in REPAIR_RULES
    )
    new_sentence, bold_badge = _retarget_bold(
        best_sentence, representative.get("cloze-body"), written
    )
    new_sentence = _single_bold(new_sentence, written, tuple(_placeable_bold_candidates(target)))
    applied_rules: tuple[str, ...] = ()
    if repair_rules:
        repaired, applied_rules = repair_sentence(new_sentence, repair_rules)
        if applied_rules:
            new_sentence = repaired
    current = target.get("Sentence")
    writes: list[PlannedWrite] = []
    if new_sentence != current:
        badges = [
            "recovered-sentence",
            "word-in-sentence",
            f"source-note:{representative.note_id}",
            f"{len(members)}-siblings-agree",
        ]
        if bold_badge:
            badges.append(bold_badge)
        badges.extend(f"expanded-{rule}" for rule in applied_rules)
        writes.append(
            PlannedWrite(
                note_id=target.note_id,
                field="Sentence",
                op=OP_EXPAND_SENTENCE,
                old_value=current,
                new_value=new_sentence,
                source_note_id=representative.note_id,
                default_checked=True,
                badges=tuple(badges),
            )
        )
    for field_name in tuple(cfg.get("scene_fill_fields", DEFAULT_SCENE_FILL_FIELDS)):
        if field_name == "Sentence" or field_name in never_copy:
            continue
        value = representative.get(field_name)
        current_value = target.get(field_name)
        stale_ruby = (
            field_name == FURIGANA_FIELD
            and not is_empty(value)
            and not is_empty(current_value)
            and not furigana_base_matches(current_value, new_sentence)
        )
        if not stale_ruby and (not is_empty(current_value) or is_empty(value)):
            continue
        if not eligible_source(field_name, value):
            continue
        if field_name in (FURIGANA_FIELD,) and repair_rules:
            value, _applied = repair_sentence(value, repair_rules)
        if field_name == "Sentence Audio":
            refs = sound_files(value)
            if not refs:
                continue
        default_checked = (
            bool(cfg.get("translation_autocheck", True)) if field_name == "Translation" else True
        )
        writes.append(
            PlannedWrite(
                note_id=target.note_id,
                field=field_name,
                op=OP_EXPAND_SENTENCE if stale_ruby else OP_FILL,
                old_value=current_value if stale_ruby else "",
                new_value=value,
                source_note_id=representative.note_id,
                default_checked=default_checked,
                badges=("recovered-sentence", f"source-note:{representative.note_id}"),
            )
        )
    source_value = representative.get("Source")
    if (
        "Source" in replace_fields
        and "Source" not in never_copy
        and is_blob_source(target.get("Source"))
        and not is_empty(source_value)
        and not is_blob_source(source_value)
    ):
        writes.append(
            PlannedWrite(
                note_id=target.note_id,
                field="Source",
                op=OP_REPLACE_SOURCE,
                old_value=target.get("Source"),
                new_value=source_value,
                source_note_id=representative.note_id,
                default_checked=bool(cfg.get("replaces_autocheck", True)),
                badges=("recovered-sentence", f"source-note:{representative.note_id}"),
            )
        )
    return writes, None


def _bold_candidates(target: NoteRecord) -> list[str]:
    """Spellings of a card's word: itself, its cloze body, its reading, and
    every kanji/kana mix from its own furigana (類い希 -> 類いまれ)."""
    out: list[str] = []
    for cand in (
        strip_renderable(target.get("cloze-body")).strip(),
        strip_renderable(target.get("Expression")).strip(),
        kana_fold(norm_reading(target.get("reading") or "")),
        *kana_spelling_candidates(target.get("Expression") or "", target.get("furigana") or ""),
        # Reading-prefix spellings (鬱病 -> うつ病) — the same forms the bold
        # tier uses; without them a kana-prefix spelling cannot be bolded.
        *_mixed_form_candidates(target.get("Expression") or "", target.get("reading") or ""),
    ):
        if cand and cand not in out:
            out.append(cand)
    return sorted(out, key=len, reverse=True)


def _placeable_bold_candidates(target: NoteRecord) -> list[str]:
    """Spellings safe to *place* a new bold onto unbolded sentence text.

    Enforces the Q-D3 word-like gate on Expression-derived spellings and the
    >=3-kana minimum on reading-only fallbacks so short readings (し, する) and
    clauses are never bolded by guesswork. Pure.
    """
    out: list[str] = []
    body = strip_renderable(target.get("cloze-body")).strip()
    if body:
        out.append(body)
    expr = strip_renderable(target.get("Expression")).strip()
    if _expression_is_word_like(expr):
        reading = kana_fold(norm_reading(target.get("reading") or ""))
        if len(reading) < 3:
            furi_reading = kana_fold(reading_from_furigana(target.get("furigana")) or "")
            if len(furi_reading) > len(reading):
                reading = furi_reading
        candidates = [
            expr,
            *(
                cand
                for cand in kana_spelling_candidates(
                    target.get("Expression") or "", target.get("furigana") or ""
                )
                if len(cand) >= 2
            ),
            *_mixed_form_candidates(target.get("Expression") or "", target.get("reading") or ""),
            *([reading] if len(reading) >= 3 else []),
        ]
        for cand in candidates:
            if cand and cand not in out:
                out.append(cand)
    return sorted(out, key=len, reverse=True)


def _expansion_anchor(candidate: NoteRecord, parent_sentence: str) -> str:
    """The word whose unique occurrence in the parent proves the card's spot.

    The scene matcher already proved the card's whole sentence is contained in
    the parent's; the anchor exists so the completion can retarget the card's
    own word into the parent's bold and ship the row checked. Preference
    order: the card's ``cloze-body``, its ``Expression``, then any placeable
    bold candidate (:func:`_placeable_bold_candidates` — the deck's
    word-mined cards store no ``cloze-*`` and bold the word inside
    ``Sentence`` instead). A word occurring more than once in the parent
    sentence is not an anchor. Pure.
    """
    for name in (
        candidate.get("cloze-body"),
        candidate.get("Expression"),
        *_placeable_bold_candidates(candidate),
    ):
        if name and _unique_anchor(parent_sentence, name):
            return name
    return ""


def _single_bold(
    value: str,
    body: str,
    extras: tuple[str, ...] = (),
    morph_tokenizer: Callable[[str], Sequence[MorphToken]] | None = None,
) -> str:
    """Exactly one bold span — around the tested word — in ``value``.

    The deck convention is one bold term. An expanded block can carry the
    scene's own bold (ぜひ's ``<b>ぜひ</b>``) and the child's word on top of
    it (two bold items, author report on 校), or no bold at all — both are
    fixed here, markup only. The word is located under width folding
    (:func:`_word_span`); the bold wraps the sentence's own characters.
    """
    if _BOLD_TAG_RE.search(value) and _has_complex_bold_markup(value):
        return value
    names = [c for c in (strip_renderable(body).strip(), *extras) if c]
    known = {_fold(c) for c in names}
    value, _ = _trim_bold_punctuation(value, known)
    if morph_tokenizer is not None:
        expr = extras[0] if extras else body
        for span in _BOLD_RE.findall(value):
            clean_span = strip_renderable(span).strip()
            if _is_morph_match(clean_span, expr, "", morph_tokenizer):
                known.add(_fold(clean_span))
    chosen_span: tuple[int, int] | None = None
    chosen = ""
    for candidate in sorted(dict.fromkeys(names), key=len, reverse=True):
        span = _word_span(value, candidate)
        if span is not None:
            chosen_span = span
            chosen = value[span[0] : span[1]]
            break
    if chosen_span is None:
        # The word repeats in the block (校 in 校 and 医学校): keep the one
        # existing bold that wraps it and drop the others (ぜひ).
        spans = _BOLD_RE.findall(value)
        wanted = [span for span in spans if _fold(strip_renderable(span).strip()) in known]
        if len(spans) <= 1 or len(wanted) != 1:
            return value
        keep = wanted[0]
        seen = False

        def only_kept(match: re.Match) -> str:
            nonlocal seen
            if not seen and match.group(1) == keep:
                seen = True
                return match.group(0)
            return match.group(1)

        return _BOLD_RE.sub(only_kept, value)
    if not _BOLD_RE.search(value):
        # No bold at all: place it where the word is unambiguous.
        start, end = chosen_span
        return value[:start] + f"<b>{value[start:end]}</b>" + value[end:]

    def keep_wanted(match: re.Match) -> str:
        inner = match.group(1)
        return match.group(0) if _fold(strip_renderable(inner).strip()) in known else inner

    kept = _BOLD_RE.sub(keep_wanted, value)
    if _BOLD_RE.search(kept):
        return kept
    # Every bold was foreign (ぜひ next to 校): bold the word instead.
    without = _BOLD_RE.sub(lambda match: match.group(1), value)
    span = _word_span(without, chosen)
    if span is None:
        return kept
    start, end = span
    return without[:start] + f"<b>{without[start:end]}</b>" + without[end:]


def _fragment_anchored_sentence(
    parent_sentence: str, candidate: NoteRecord, rules: tuple[str, ...] = ()
) -> str | None:
    """The parent's sentence with the word bolded inside the child's fragment.

    Used when the word itself occurs several times in the block (校 appears in
    校, 医学校, 精神医学校…): the child's own fragment — ``cloze-prefix + body
    + cloze-suffix`` — is unique by construction, so bolding the body inside
    that occurrence is unambiguous. Both sides get the configured repair
    rules first (the child's fragment carries the subtitle's tail arrow, the
    parent's may not), so one write fixes the sentence and the bold together.
    Returns ``None`` when the fragment is not found exactly once.

    A sibling may bold its *own* word inside this fragment (the 飲食 card's
    parent reads ``…<b>無銭</b>飲食で逮捕されました｡…``). Bold is markup, not
    text: strip it from the parent before matching so the cloze fragment is
    contiguous again. The caller's ``_single_bold`` then retargets the one
    highlight to this card's word.
    """
    prefix = candidate.get("cloze-prefix") or ""
    body = candidate.get("cloze-body") or ""
    suffix = candidate.get("cloze-suffix") or ""
    if not strip_renderable(body):
        return None
    parent = re.sub(r"</?b>", "", parent_sentence)
    if rules:
        parent, _applied = repair_sentence(parent, rules)
    fragment = prefix + body + suffix
    if rules:
        fragment, _applied = repair_sentence(fragment, rules)
    if not fragment or parent.count(fragment) != 1:
        return None
    bolded = prefix + f"<b>{body}</b>" + suffix
    if rules:
        bolded, _applied = repair_sentence(bolded, rules)
    return parent.replace(fragment, bolded, 1)


def _scene_writes_for(
    candidate: NoteRecord,
    matches: list[SceneMatch],
    cfg: dict,
    ruby_regeneration_available: bool = False,
    cache: dict[int, str] | None = None,
) -> tuple[list[PlannedWrite], str | None]:
    """Emit scene-op writes for one candidate, or a reason string.

    ``ruby_regeneration_available`` is true when the furigana tier will run
    with a working reading engine this plan: a completion that would leave a
    stale fragment ruby may then ship checked, because that tier rebuilds the
    ruby from the completed sentence in the same plan.
    """
    # The repair rules apply to the text this function *copies* (the parent's
    # sentence and ruby carry the subtitle's tail arrows), so the fill loop
    # and the sentence block share one definition.
    repair_rules = tuple(
        rule for rule in cfg.get("sentence_repair_rules", ()) if rule in REPAIR_RULES
    )
    optin_fields = tuple(cfg.get("scene_optin_fields", DEFAULT_SCENE_OPTIN_FIELDS))
    fill_fields = tuple(cfg.get("scene_fill_fields", DEFAULT_SCENE_FILL_FIELDS))
    replace_fields = tuple(cfg.get("scene_replace_fields", DEFAULT_SCENE_REPLACE_FIELDS))
    never_copy = FIELD_NEVER_COPY | frozenset(cfg.get("never_copy_fields", ()))
    ranked = rank_scene_parents(
        matches,
        candidate,
        optin_fields=optin_fields,
        fill_fields=fill_fields,
        replace_fields=replace_fields,
        never_copy=never_copy,
        cache=cache,
    )
    if not ranked:
        return [], "no-parent-that-fills"
    groups = agreement_groups(ranked)
    # Largest agreement group wins; ties break on the first-ranked member.
    best_payload, best_members = max(
        groups.items(), key=lambda kv: (len(kv[1]), -ranked.index(kv[1][0]))
    )
    parent = best_members[0].parent
    agree = len(best_members)
    gap_ms = best_members[0].tier_gap_ms
    writes: list[PlannedWrite] = []
    group_id = f"scene-{candidate.note_id}-{parent.note_id}"
    gap_hours = gap_ms / 3_600_000
    # Confidence of the chosen source. A source that other parents disagree
    # with, or one far outside the mining session, stays visible but ships
    # unchecked — the child's fields would be wrong in a way no later check
    # could notice. Two exceptions, both from the author's own cards:
    # sentence-identical parents are self-verifying (distance does not
    # matter), and a group of **two or more parents writing byte-identical
    # payloads** is corroborated — refusing it left 自閉症-shaped cards
    # unfinished while the parent (自負) was plain to see. A divergent parent
    # therefore only demotes a *unique* source.
    corroborated = agree >= 2
    if not corroborated and len(groups) > 1:
        # Same scene, different clip: the author's 自閉症 card has parents
        # 自負 and 類い希 whose sentences are the *same subtitle block*, one a
        # prefix of the other — a divergence of clipping, not of source. When
        # every competing group is nested with the chosen one, the tighter
        # (highest-ranked) pick is trusted; genuinely different scenes still
        # demote to unchecked.
        best_text = _cached_norm_sentence_match(parent, cache)
        nested = True
        for _payload, members in groups.items():
            if members is best_members:
                continue
            other_text = _cached_norm_sentence_match(members[0].parent, cache)
            if not (best_text in other_text or other_text in best_text):
                nested = False
                break
        if nested:
            corroborated = True
    divergent = len(groups) > 1 and not corroborated
    exact = best_members[0].exact_equal
    auto_gap_hours = float(cfg.get("scene_auto_gap_hours") or 6)
    far = gap_ms > auto_gap_hours * 3_600_000
    trusted = not divergent and (exact or not far)
    context_badges = [f"source-note:{parent.note_id}", f"added-gap:{gap_hours:.1f}h"]
    if agree > 1:
        context_badges.append(f"{agree}-siblings-agree")
        if len(groups) > 1:
            context_badges.append("corroborated-against-divergence")
    elif corroborated and len(groups) > 1:
        context_badges.append("same-scene-tighter-clip")
    if divergent:
        context_badges.append("divergent-parents")
    elif far and not exact:
        context_badges.append("far-from-source")
    if not best_members[0].boundary_ok:
        context_badges.append("interior-span")
    parent_len = len(_cached_norm_sentence_match(parent, cache))
    cand_len = len(_cached_norm_sentence_match(candidate, cache))
    if parent_len > 2 * cand_len:
        context_badges.append("scene-block-parent")

    # Fill ops: only truly empty fields, only values the parent actually has.
    for fname, value in best_payload:
        if fname == "Source":
            is_empty_src = is_empty(candidate.get("Source"))
            op = OP_FILL if is_empty_src else OP_REPLACE_SOURCE
            src_badge = "fills-empty-source" if is_empty_src else "replaces-blob-url"
            writes.append(
                PlannedWrite(
                    note_id=candidate.note_id,
                    field="Source",
                    op=op,
                    old_value=candidate.get("Source") or "",
                    new_value=value,
                    source_note_id=parent.note_id,
                    default_checked=bool(cfg.get("replaces_autocheck", True)) and trusted,
                    badges=tuple([src_badge, *context_badges]),
                    window_ms=gap_ms,
                    group_id=group_id,
                )
            )
            continue
        default_checked = trusted
        if fname in optin_fields:
            default_checked = trusted and bool(cfg.get("notes_autocheck", False))
        elif fname == "Translation":
            default_checked = trusted and bool(cfg.get("translation_autocheck", True))
        # Sentence fields get the same cleanup as the expansion's copy: the
        # parent's subtitle dump carries line-tail arrows, and writing them
        # means the next run has work (one run fixes a card). The sentence
        # block below owns the paired ruby; a plain fill here would be a
        # second row for one field.
        fill_value = value
        if fname in ("Sentence", "Sentence (furigana)"):
            if any(w.field == fname for w in writes):
                continue
            if repair_rules:
                field_rules = (
                    _ruby_repair_rules(repair_rules) if fname == FURIGANA_FIELD else repair_rules
                )
                fill_value, _applied = repair_sentence(value, field_rules)
        writes.append(
            PlannedWrite(
                note_id=candidate.note_id,
                field=fname,
                op=OP_FILL,
                old_value="",
                new_value=fill_value,
                source_note_id=parent.note_id,
                default_checked=default_checked,
                badges=tuple(context_badges),
                window_ms=gap_ms,
                group_id=group_id,
            )
        )

    # Sentence completion: the card's mined fragment is a clip of the same
    # scene the parent holds, and the parent's `Sentence Audio` *is* that
    # scene's block — so a card about to receive the parent's audio must also
    # receive the parent's text, or it would play a nine-line clip under a
    # two-line sentence (author's 正道: 出来's 14.8 s scene clip under the
    # card's own two-line fragment). The scene matcher already proved the
    # fragment is contained in the parent's `Sentence`, so completing the text
    # never loses it: the "fragment survives" guard is satisfied by that
    # containment, not only by `cloze-*` (which holds just the tested word on
    # a word-mined card).
    #
    # Convergence (one run fixes a card, a second is a no-op): the completion
    # runs while the card is missing its scene audio, or while its audio is
    # byte-identical to this parent's but its sentence is still a fragment of
    # the parent's block (the "audio filled, sentence skipped" shape — Q-A2).
    # A card whose audio matches its *own* sentence is a scene unit in its own
    # right: no neighbour's block is forced onto it.
    cand_norm = norm_sentence_match(candidate.get("Sentence"))
    parent_sentence = parent.get("Sentence")
    parent_furigana = parent.get("Sentence (furigana)")
    shared_audio = bool(
        set(sound_files(candidate.get("Sentence Audio")))
        & set(sound_files(parent.get("Sentence Audio")))
    )
    if (
        (not is_empty(candidate.get("Sentence Audio")) and not shared_audio)
        or "Sentence" not in replace_fields
        or "Sentence" in never_copy
    ):
        return writes, None
    # Remaining guards: the expansion anchor must be placeable (once, or
    # inside the card's own fragment), and a stale fragment ruby must not be
    # left behind (atomic replacement, or regeneration when the engine is
    # here). Cards that store no ``cloze-*`` (word-mined style: the tested
    # word is bolded inside ``Sentence``) anchor on their own ``Expression``
    # when that word is unique in the parent.
    if cand_norm and cand_norm != norm_sentence_match(parent_sentence):
        anchor = _expansion_anchor(candidate, parent_sentence)
        unique_anchor = bool(anchor)
        fragment_bold = (
            None
            if unique_anchor
            else _fragment_anchored_sentence(parent_sentence, candidate, repair_rules)
        )
        if unique_anchor or fragment_bold is not None:
            checked = bool(cfg.get("replaces_autocheck", True)) and trusted
            kept_badge = (
                "fragment-kept-in-cloze"
                if _fragment_recoverable(candidate, cand_norm)
                else "fragment-kept-in-scene"
            )
            badges = ["expands-sentence", kept_badge, *context_badges]
            if unique_anchor:
                child_body = anchor
                new_sentence, bold_badge = _retarget_bold(
                    parent_sentence,
                    parent.get("cloze-body"),
                    child_body,
                )
                new_sentence = _single_bold(
                    new_sentence,
                    child_body,
                    tuple(_placeable_bold_candidates(candidate)),
                )
                if not candidate.get("cloze-body") and anchor:
                    bold_badge = "bold-retargeted-to-expression"
            else:
                # The word occurs more than once in the block (校 in 精神医学
                # 校, 医学校, …): bold the occurrence inside the child's own
                # fragment, which is unique by definition.
                assert fragment_bold is not None
                new_sentence = _single_bold(
                    fragment_bold,
                    candidate.get("cloze-body"),
                    tuple(_placeable_bold_candidates(candidate)),
                )
                bold_badge = "bold-added-to-cloze-word"
            if bold_badge:
                badges.append(bold_badge)
            # Repair the text the expansion writes: the parent's subtitle dump
            # may carry line-tail arrows, and leaving them in means the very
            # next run has work to do — a card must be fixed in one run
            # (owner critique 2026-11-16). Same rules, same guard as the
            # in-place repair.
            if repair_rules:
                repaired, applied_rules = repair_sentence(new_sentence, repair_rules)
                if applied_rules:
                    new_sentence = repaired
                    badges += tuple(f"expanded-{rule}" for rule in applied_rules)
            child_furigana = candidate.get("Sentence (furigana)")
            if not is_empty(child_furigana):
                if not is_empty(parent_furigana):
                    badges += ("replaces-fragment-furigana",)
                elif ruby_regeneration_available:
                    # The parent carries no ruby, but the furigana tier will
                    # rebuild one from the completed sentence in this plan: the
                    # fragment ruby is replaced, so the completion is safe.
                    badges += ("ruby-will-be-regenerated",)
                else:
                    # Would leave fragment ruby under the full scene text and
                    # nothing in this plan can rebuild it.
                    checked = False
                    badges += ("stale-fragment-furigana-risk",)
            writes.append(
                PlannedWrite(
                    note_id=candidate.note_id,
                    field="Sentence",
                    op=OP_EXPAND_SENTENCE,
                    old_value=candidate.get("Sentence"),
                    new_value=new_sentence,
                    source_note_id=parent.note_id,
                    default_checked=checked,
                    badges=tuple(badges),
                    window_ms=gap_ms,
                    group_id=group_id,
                )
            )
            # The copied ruby gets the same cleanup as the copied sentence: a
            # parent's dump carries arrows in both fields, and leaving them
            # means the next run has work to do (owner rule: one run fixes a
            # card). The arrow rule is deterministic and annotation-safe.
            parent_ruby = parent_furigana
            if repair_rules:
                parent_ruby, _applied_ruby = repair_sentence(
                    parent_furigana, _ruby_repair_rules(repair_rules)
                )
            # The ruby must describe the sentence this card ends up with — the
            # parent's ruby belongs to the parent's line and is only usable
            # when its base text matches (author's どなた: a parent's ruby was
            # copied under a different sentence).
            effective_sentence = next(
                (w.new_value for w in writes if w.field == "Sentence"),
                candidate.get("Sentence") or "",
            )
            if parent_ruby.strip() and not furigana_base_matches(parent_ruby, effective_sentence):
                parent_ruby = ""
            already_queued = any(w.field == "Sentence (furigana)" for w in writes)
            if not already_queued and not is_empty(parent_ruby):
                if is_empty(child_furigana):
                    writes.append(
                        PlannedWrite(
                            note_id=candidate.note_id,
                            field="Sentence (furigana)",
                            op=OP_FILL,
                            old_value="",
                            new_value=parent_ruby,
                            source_note_id=parent.note_id,
                            default_checked=checked,
                            badges=tuple(["scene-furigana", *context_badges]),
                            window_ms=gap_ms,
                            group_id=group_id,
                        )
                    )
                else:
                    writes.append(
                        PlannedWrite(
                            note_id=candidate.note_id,
                            field="Sentence (furigana)",
                            op=OP_EXPAND_SENTENCE,
                            old_value=child_furigana,
                            new_value=parent_ruby,
                            source_note_id=parent.note_id,
                            default_checked=checked,
                            badges=tuple(["replaces-fragment-furigana", *context_badges]),
                            window_ms=gap_ms,
                            group_id=group_id,
                        )
                    )
        else:
            writes.append(
                PlannedWrite(
                    note_id=candidate.note_id,
                    field="Sentence",
                    op=OP_EXPAND_SENTENCE,
                    old_value=candidate.get("Sentence"),
                    new_value=parent_sentence,
                    source_note_id=parent.note_id,
                    default_checked=False,
                    badges=(
                        "expansion-guard",
                        "fragment-not-recoverable"
                        if not _fragment_recoverable(candidate, cand_norm)
                        else "cloze-anchor-not-unique",
                        *context_badges,
                    ),
                    window_ms=gap_ms,
                    group_id=group_id,
                )
            )

    # Invariant: a Sentence (furigana) fill must never ship default-checked
    # while its paired Sentence expansion is guarded — full-scene ruby under
    # a fragment sentence is a broken card. The furigana fill may originate
    # from the generic fill loop, so pair it here.
    expansion = next(
        (w for w in writes if w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE), None
    )
    if expansion is not None and not expansion.default_checked:
        writes = [
            w
            if not (w.field == "Sentence (furigana)" and w.op == OP_FILL and w.default_checked)
            else replace(w, default_checked=False, badges=(*w.badges, "paired-with-expansion"))
            for w in writes
        ]
    return writes, None


def _word_writes_for(
    candidate: NoteRecord,
    group: list[NoteRecord],
    cfg: dict,
) -> list[PlannedWrite]:
    """Fill empty word-level fields from best siblings.

    Definitions (``Definition``, ``Extended definition``) belong to
    :data:`FIELD_NEVER_COPY` and are never copied between cards.
    """
    writes: list[PlannedWrite] = []
    never_copy = FIELD_NEVER_COPY | frozenset(cfg.get("never_copy_fields", ()))
    for fname in cfg.get("word_fill_fields", ()):
        if fname in never_copy:
            continue
        current = candidate.get(fname)
        if not is_empty(current):
            continue
        src = best_word_source(fname, candidate, group)
        if src is None:
            continue
        badges: list[str] = [f"source-note:{src.record.note_id}"]
        if fname in ("Pitch Accent", "Frequency"):
            badges.append("dictionary-version-may-differ")
        if src.provenance:
            badges.append("yomitan-gloss")
        elif src.consensus >= 2:
            badges.append(f"{src.consensus}-siblings-agree")
        else:
            badges.append("single-source-user-authored")
        default_checked = bool(cfg.get("replaces_autocheck", True))
        writes.append(
            PlannedWrite(
                note_id=candidate.note_id,
                field=fname,
                op=OP_FILL,
                old_value="",
                new_value=src.value,
                source_note_id=src.record.note_id,
                default_checked=default_checked,
                badges=tuple(badges),
            )
        )
    return writes


def _self_reading_write(target: NoteRecord, cfg: dict) -> PlannedWrite | None:
    """Fill empty ``reading`` from the note's own ``furigana`` field (self-oracled).

    When a card has no sibling to fill ``reading`` from (e.g. solitary book
    mines or single-card scenes), but its own ``furigana`` contains well-formed
    bracket notation (``診察[しんさつ]``), the reading is unambiguously known.
    """
    if "reading" not in cfg.get("word_fill_fields", ()):
        return None
    if not is_empty(target.get("reading")):
        return None
    derived = reading_from_furigana(target.get("furigana") or "")
    if not derived:
        return None
    return PlannedWrite(
        note_id=target.note_id,
        field="reading",
        op=OP_FILL,
        old_value="",
        new_value=derived,
        source_note_id=None,
        default_checked=bool(cfg.get("replaces_autocheck", True)),
        badges=("reading-derived-from-furigana",),
    )


_ANKI_CONTROL_TAGS = frozenset({"marked", "leech"})


def _reserved_tags(cfg: dict) -> frozenset[str]:
    """Tags that must never propagate from a source card onto a target."""
    return frozenset(
        {
            *_ANKI_CONTROL_TAGS,
            cfg.get("optout_tag", "no-autofill"),
            cfg.get("applied_tag", "auto-filled"),
        }
    )


def _tags_write_for(
    target: NoteRecord,
    target_writes: list[PlannedWrite],
    records_by_id: dict[int, NoteRecord],
    cfg: dict,
) -> PlannedWrite | None:
    """Union the target's tags with every contributing source's tags.

    Only sources of default-checked writes contribute: a card the plan does
    not act on has not earned tag authority. Control tags (Anki's
    marked/leech and this tool's own optout/applied tags) never propagate.
    The op can only ADD: ``new_value`` is a superset of ``old_value`` —
    that is the badge's contract.
    """
    reserved = _reserved_tags(cfg)
    new_tags = set(target.tags)
    sources = sorted(
        {
            w.source_note_id
            for w in target_writes
            if w.default_checked and w.source_note_id is not None
        }
    )
    for source_id in sources:
        source = records_by_id.get(source_id)
        if source is not None:
            new_tags |= set(source.tags) - reserved
    if new_tags == set(target.tags):
        return None
    return PlannedWrite(
        note_id=target.note_id,
        field="tags",
        op=OP_ADD_TAGS,
        old_value=" ".join(sorted(target.tags)),
        new_value=" ".join(sorted(new_tags)),
        default_checked=True,
        badges=("tags-union-never-removes",),
    )


def scope_from_selection(
    selected: Sequence[int], available: Iterable[int]
) -> tuple[list[int], list[int]]:
    """Resolve a Browser selection to ``(scope, unknown)``, order preserved.

    Duplicates are dropped and the user's selection order is kept. Ids that
    are not in ``available`` are reported separately so the caller can explain
    them (deleted notes, another note type) instead of silently dropping them
    from the run.
    """
    known = set(available)
    seen: set[int] = set()
    scope: list[int] = []
    unknown: list[int] = []
    for note_id in selected:
        if note_id in seen:
            continue
        seen.add(note_id)
        if note_id in known:
            scope.append(note_id)
        else:
            unknown.append(note_id)
    return scope, unknown


def _drop_rubies_that_do_not_describe_the_sentence(
    target: NoteRecord, writes: list[PlannedWrite]
) -> list[PlannedWrite]:
    """Drop a checked ruby write that does not describe the final Sentence.

    The ruby is *derived* from the sentence, so a value mined from another
    card's line must never land on a sentence it does not annotate. The
    generic payload fill copies a parent's ruby into an empty child ruby
    whenever the parent is the best source — but the parent's ruby belongs to
    the parent's line, and when this card's own sentence is a different
    (shorter) block it would annotate text the card does not show (author:
    どなた, 日取り). Only *checked* rows are dropped; a row already demoted to
    ``paired-with-expansion`` never applies, and removing the bad value here —
    before the regeneration tier decides — lets a correct ruby be generated
    in its place instead of being blocked by the bad one. Pure.
    """
    sentence = next(
        (w.new_value for w in writes if w.field == "Sentence" and w.default_checked),
        target.get("Sentence") or "",
    )
    if not sentence.strip():
        return writes
    return [
        w
        for w in writes
        if not (
            w.field == FURIGANA_FIELD
            and w.default_checked
            and w.new_value.strip()
            and not furigana_base_matches(w.new_value, sentence)
        )
    ]


def _demote_completion_with_stale_ruby(
    target: NoteRecord, writes: list[PlannedWrite]
) -> list[PlannedWrite]:
    """Demote a completion that would keep a ruby describing other text.

    A completion ships checked when the furigana tier *should* rebuild the
    card's ruby in the same plan (``ruby-will-be-regenerated``). If the engine
    is present but produces nothing, no ruby write is queued and the card's
    existing fragment ruby would annotate only part of the new sentence. The
    completion is demoted (unchecked) instead: the card keeps its own text
    until a run can rebuild the ruby. A checked ruby write that already
    describes the new sentence is fine. Pure.
    """
    completion = next(
        (
            w
            for w in writes
            if w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE and w.default_checked
        ),
        None,
    )
    if completion is None:
        return writes
    ruby_write = next((w for w in writes if w.field == FURIGANA_FIELD and w.default_checked), None)
    if ruby_write is not None and furigana_base_matches(ruby_write.new_value, completion.new_value):
        return writes
    current = target.get(FURIGANA_FIELD) or ""
    if not current.strip() or furigana_base_matches(current, completion.new_value):
        return writes
    return [
        replace(w, default_checked=False, badges=(*w.badges, "stale-fragment-furigana-risk"))
        if w is completion
        else w
        for w in writes
    ]


def _append_local_sentence_writes(
    target: NoteRecord,
    target_writes: list[PlannedWrite],
    repair_rules: tuple[str, ...],
    reasons: dict[int, str],
    warnings: list[str],
    morph_tokenizer: Callable[[str], Sequence[MorphToken]] | None = None,
    batch_bold_inflected: bool = True,
) -> None:
    """Append in-place bold and subtitle-noise repairs when Sentence is free."""
    bold_write, bold_reason, bold_detail = _bold_repair_for(
        target,
        repair_rules,
        morph_tokenizer=morph_tokenizer,
        batch_bold_inflected=batch_bold_inflected,
    )
    queued_expansion = any(
        w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE and w.default_checked
        for w in target_writes
    )
    if bold_write is not None and not queued_expansion:
        target_writes.append(bold_write)
    elif bold_reason is not None:
        suffix = " (Sentence is also queued for scene expansion)" if queued_expansion else ""
        expr = target.get("Expression") or "(sentence card)"
        warnings.append(
            f"note {target.note_id} ({expr}): no bold in Sentence — {bold_detail}{suffix}"
        )
        if not target_writes and (
            target.note_id not in reasons
            # A more specific verdict beats the generic scene reason: a
            # sentence that does not contain its own word is a mis-mine
            # the author must see (類い希-shaped cards).
            or (
                bold_reason in ("cloze-word-not-in-sentence", "cloze-word-ambiguous")
                and reasons.get(target.note_id) == "no-parent-that-fills"
            )
        ):
            reasons[target.note_id] = bold_reason
    # Subtitle-noise repair (dimension 2). Only when no *checked* other
    # Sentence write is queued for this note — expansion and bold own the
    # field first, and two applied writes to one field would fight. A
    # guarded row never applies, so it must not block the repair.
    if repair_rules and not any(w.field == "Sentence" and w.default_checked for w in target_writes):
        repair_write, repair_reason, repair_detail = _sentence_repair_write(target, repair_rules)
        if repair_write is not None:
            target_writes.append(repair_write)
        elif repair_reason is not None:
            expr = target.get("Expression") or "(sentence card)"
            warnings.append(
                f"note {target.note_id} ({expr}): sentence repair skipped — {repair_detail}"
            )


def build_plan(
    records: list[NoteRecord],
    cfg: dict,
    scope_note_ids: list[int] | None = None,
    media_exists=None,
    furigana_generator=None,
    morph_tokenizer: Callable[[str], Sequence[MorphToken]] | None = None,
) -> Plan:
    """Build the full plan.

    ``records``: all records in scope (the deck). ``scope_note_ids``: the
    notes to *fix* (targets); all records remain eligible as parents and
    word sources. ``media_exists(filename)``: optional gate — fills
    referencing missing media are dropped with a warning.
    ``furigana_generator(sentence) -> furigana | None``: optional bridge to
    the installed Japanese Support add-on's reading engine (dimension 3;
    ``None`` makes the tier report itself unavailable instead of writing).
    """
    window_ms = min(int(cfg.get("window_days", 3)) * DAY_MS, MAX_WINDOW_MS)
    min_len = int(cfg.get("min_sentence_len", 6))
    cache: dict[int, str] = {}
    by_id = {r.note_id: r for r in records}

    parents_by_id = {
        r.note_id: r
        for r in records
        if sound_files(r.get("Sentence Audio"))
        and r.get("Sentence Audio") is not None
        and not is_word_media(r.get("Sentence Audio"))
        and not is_word_media(r.get("Picture"))
        and (
            media_exists is None
            or all(media_exists(f) for f in sound_files(r.get("Sentence Audio")))
        )
    }
    # Audio-bearing cards grouped by the episode their timestamped Source
    # names — the fallback for a card whose own sentence is a mis-mine.
    episodes: dict[str, list[NoteRecord]] = {}
    audio_parents: dict[str, list[NoteRecord]] = {}
    for record in parents_by_id.values():
        key = _episode_key(record.get("Source"))
        if key is not None:
            episodes.setdefault(key, []).append(record)
        for filename in sound_files(record.get("Sentence Audio")):
            audio_parents.setdefault(filename, []).append(record)

    if scope_note_ids is None:
        scope = [r for r in records if is_blob_source(r.get("Source"))]
    else:
        scope = [by_id[nid] for nid in scope_note_ids if nid in by_id]

    writes: list[PlannedWrite] = []
    reasons: dict[int, str] = {}
    warnings: list[str] = []
    warned: set[str] = set()
    optout = cfg.get("optout_tag", "no-autofill")

    raw_repair_rules = tuple(cfg.get("sentence_repair_rules", ()))
    unknown_rules = [rule for rule in raw_repair_rules if rule not in REPAIR_RULES]
    if unknown_rules:
        warnings.append(f"unknown sentence_repair_rules ignored: {', '.join(unknown_rules)}")
    repair_rules = tuple(rule for rule in raw_repair_rules if rule in REPAIR_RULES)
    # Minimal test configs omit `batch_regenerate_furigana` when exercising
    # non-furigana tiers headless; the shipped `config.json` sets it to true.
    furigana_tier = bool(cfg.get("batch_regenerate_furigana", False))
    expression_tier = bool(cfg.get("batch_repair_expression", True))
    bold_inflected = bool(cfg.get("batch_bold_inflected", True))
    # Fields whose queued values come from the old spelling's word group;
    # the Expression rewrite must not land in the same run as them.
    word_fields = frozenset({*cfg.get("word_fill_fields", ()), "Expression"})

    target_keys = {
        word_key(t.get("Expression"), t.get("reading"), t.get("furigana")) for t in scope
    } - {None}
    if len(target_keys) <= 100:
        groups: dict[tuple[str, str], list[NoteRecord]] = {}
        if target_keys:
            for r in records:
                k = word_key(r.get("Expression"), r.get("reading"), r.get("furigana"))
                if k is not None and k in target_keys:
                    groups.setdefault(k, []).append(r)
    else:
        groups = word_groups(records)

    parent_usage: dict[int, int] = {}
    scene_dimension = bool(cfg.get("batch_scene_propagation", True))
    scene_fill_fields = tuple(cfg.get("scene_fill_fields", DEFAULT_SCENE_FILL_FIELDS))
    scene_replace_fields = tuple(cfg.get("scene_replace_fields", DEFAULT_SCENE_REPLACE_FIELDS))

    parent_index = None
    if scene_dimension and parents_by_id:
        targets_with_gap = [
            t
            for t in scope
            if any(is_empty(t.get(f)) for f in scene_fill_fields)
            or (
                "Source" in scene_replace_fields
                and (is_blob_source(t.get("Source")) or is_empty(t.get("Source")))
            )
        ]
        if targets_with_gap:
            sorted_pids = sorted(parents_by_id)
            candidate_pids: set[int] = set()
            for t in targets_with_gap:
                lo = bisect.bisect_left(sorted_pids, t.note_id - window_ms)
                hi = bisect.bisect_right(sorted_pids, t.note_id + window_ms)
                candidate_pids.update(sorted_pids[lo:hi])
            if len(candidate_pids) < len(parents_by_id):
                candidate_parents = {pid: parents_by_id[pid] for pid in candidate_pids}
                parent_index = SceneParentIndex.from_parents(candidate_parents, cache)
            else:
                parent_index = SceneParentIndex.from_parents(parents_by_id, cache)

    for target in scope:
        if optout in target.tags:
            reasons[target.note_id] = "opted-out"
            continue
        target_writes: list[PlannedWrite] = []
        scene_writes, reason = [], None
        # A shared-audio sibling whose full block still contains this card's
        # fragment is the "audio filled, sentence skipped" shape: the card
        # owns scene audio but not its scene text, so it still owes an
        # expansion (QUALITY Q-A2).
        skipped_expansion = _shared_audio_parent(target, audio_parents) is not None
        has_scene_gap = scene_dimension and (
            any(is_empty(target.get(f)) for f in scene_fill_fields)
            or (
                "Source" in scene_replace_fields
                and (is_blob_source(target.get("Source")) or is_empty(target.get("Source")))
            )
            or skipped_expansion
        )
        # Incomplete cards (missing audio/picture/ruby/translation or blob Source)
        # and cards whose audio came from a sibling but whose sentence was
        # skipped find their scene parent in creation order (session clustering).
        # Other complete cards already have their own audio/sentence and are
        # anchors/sources: they are NEVER overwritten by another scene.
        if has_scene_gap:
            matches = find_scene_parents(
                target,
                parents_by_id,
                window_ms,
                min_len,
                cache,
                index=parent_index,
            )
            scene_writes, reason = _scene_writes_for(
                target,
                matches,
                cfg,
                ruby_regeneration_available=furigana_tier and furigana_generator is not None,
                cache=cache,
            )
            if not scene_writes and reason == "no-parent-that-fills":
                recovered, _recovered_reason = _word_recovered_scene_writes(
                    target, episodes, cfg, cache=cache
                )
                if recovered:
                    scene_writes, reason = recovered, None
            if reason:
                reasons[target.note_id] = reason
        # Sentence Audio is fill-only (QUALITY Q-D1): a card that already has its
        # own audio is a scene unit and is never overwritten.
        for w in scene_writes:
            if w.source_note_id:
                parent_usage[w.source_note_id] = parent_usage.get(w.source_note_id, 0) + 1
            if media_exists is not None and w.op == OP_FILL:
                refs = [*sound_files(w.new_value), *image_files(w.new_value)]
                if refs and not all(media_exists(f) for f in refs):
                    warnings.append(
                        f"drop fill note:{w.note_id} field:{w.field} missing media {refs}"
                    )
                    continue
            target_writes.append(w)
        # Word-level fills apply to every scoped note with word gaps.
        scene_flags = {
            badge
            for w in target_writes
            for badge in w.badges
            if badge in ("divergent-parents", "far-from-source")
        }
        if scene_flags:
            expr = target.get("Expression") or "(sentence card)"
            why = (
                "scene sources disagree"
                if "divergent-parents" in scene_flags
                else "scene source is far away in time"
            )
            warnings.append(f"note {target.note_id} ({expr}): {why} — rows left unchecked")
        key = word_key(target.get("Expression"), target.get("reading"), target.get("furigana"))
        if key is not None:
            group = groups.get(key, [])
            if len(group) > 1:
                target_writes.extend(_word_writes_for(target, group, cfg))
        if not any(w.field == "reading" for w in target_writes):
            self_reading = _self_reading_write(target, cfg)
            if self_reading is not None:
                target_writes.append(self_reading)
        # Empty-glossary shells (Yomitan `<ol></ol>`) render as nothing and are
        # cleared; definitions are never copied from sibling cards.
        target_writes.extend(
            _empty_glossary_writes(
                target, cfg, {w.field for w in target_writes if w.default_checked}
            )
        )
        # Expression spelling repair (dimension 4, self-oracled). Deferred
        # while a word-level write is queued: those values come from the old
        # spelling's word group, and the rewrite must not land in the same
        # run as them (F2 risk 4).
        if expression_tier and not any(w.field in word_fields for w in target_writes):
            repair_expr_write, repair_expr_reason, repair_expr_detail = _expression_repair_for(
                target
            )
            if repair_expr_write is not None:
                target_writes.append(repair_expr_write)
            elif repair_expr_reason is not None:
                expr_label = target.get("Expression") or "(sentence card)"
                warnings.append(
                    f"note {target.note_id} ({expr_label}): expression repair"
                    f" skipped — {repair_expr_detail}"
                )
                if not target_writes and target.note_id not in reasons:
                    reasons[target.note_id] = repair_expr_reason
        # Markup-only bold repair and subtitle-noise repair when no checked
        # scene expansion already owns Sentence.
        _append_local_sentence_writes(
            target,
            target_writes,
            repair_rules,
            reasons,
            warnings,
            morph_tokenizer=morph_tokenizer,
            batch_bold_inflected=bold_inflected,
        )
        # The ruby must describe the Sentence this card ends up with: drop a
        # checked ruby copied from another card's line *before* the ruby
        # repair / regeneration steps, so the field is either cleaned or
        # correctly regenerated rather than carrying the wrong text.
        target_writes = _drop_rubies_that_do_not_describe_the_sentence(target, target_writes)
        had_checked_expansion = any(
            w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE and w.default_checked
            for w in target_writes
        )
        effective_sentence = next(
            (w.new_value for w in target_writes if w.field == "Sentence" and w.default_checked),
            target.get("Sentence") or "",
        )
        # The ruby field carries the same subtitle arrows (owner report
        # 2026-11-16): clean it too, unless a checked write already targets it
        # or a checked expansion changed Sentence to text this ruby no longer
        # describes (in which case furigana regeneration must own the field).
        if repair_rules and not any(
            w.field == FURIGANA_FIELD and w.default_checked for w in target_writes
        ):
            ruby_write, _ruby_reason, _ruby_detail = _ruby_repair_write(target, repair_rules)
            if ruby_write is not None and (
                not had_checked_expansion
                or not effective_sentence.strip()
                or furigana_base_matches(ruby_write.new_value, effective_sentence)
            ):
                target_writes.append(ruby_write)
        # Furigana regeneration (dimension 3). Runs after any queued
        # Sentence write — the ruby layer must match the text the plan
        # leaves behind (BULK_FIX.md ordering). Only when no other
        # Sentence (furigana) write is queued: a scene fill owns the field.
        if furigana_tier:
            if furigana_generator is None:
                if "furigana-generator-unavailable" not in warned:
                    warned.add("furigana-generator-unavailable")
                    warnings.append(
                        "batch_regenerate_furigana is on but no furigana generator"
                        " is available (Japanese Support unreachable) — dimension"
                        " skipped"
                    )
            elif not any(w.field == FURIGANA_FIELD and w.default_checked for w in target_writes):
                furigana_write, furigana_reason, furigana_detail = _furigana_write_for(
                    target, furigana_generator, target_writes
                )
                if furigana_write is not None:
                    target_writes.append(furigana_write)
                elif furigana_reason is not None:
                    expr = target.get("Expression") or "(sentence card)"
                    warnings.append(
                        f"note {target.note_id} ({expr}): furigana regeneration"
                        f" skipped — {furigana_detail}"
                    )
        # A completion must never ship checked under a ruby that still
        # describes the card's old text (Q-D4/Q-D15): if the regeneration
        # engine was present but produced nothing, demote it (keeping the
        # unchecked expansion visible with `stale-fragment-furigana-risk`), and
        # still clean subtitle noise in the kept fragment ruby.
        target_writes = _demote_completion_with_stale_ruby(target, target_writes)
        if (
            had_checked_expansion
            and not any(
                w.field == "Sentence" and w.op == OP_EXPAND_SENTENCE and w.default_checked
                for w in target_writes
            )
            and repair_rules
            and not any(w.field == FURIGANA_FIELD and w.default_checked for w in target_writes)
        ):
            ruby_write, _ruby_reason, _ruby_detail = _ruby_repair_write(target, repair_rules)
            if ruby_write is not None:
                target_writes.append(ruby_write)
        # Tags: union of the target's tags with every contributing source's.
        tags_write = _tags_write_for(target, target_writes, by_id, cfg)
        if tags_write is not None:
            target_writes.append(tags_write)
        # A guarded (unchecked) row never applies; when a checked write now
        # covers the same field, the guarded row would only fight it at apply
        # time (one field, two rows, one of them dead). Drop it — its reason
        # stays in the plan's warnings/reasons.
        applied_fields = {w.field for w in target_writes if w.default_checked}
        if applied_fields:
            target_writes = [
                w for w in target_writes if w.default_checked or w.field not in applied_fields
            ]
        if not target_writes and target.note_id not in reasons:
            reasons[target.note_id] = "nothing-to-fill"
        if target_writes:
            reasons.pop(target.note_id, None)
        writes.extend(target_writes)

    for parent_id, uses in parent_usage.items():
        if uses > 20:
            warnings.append(f"parent {parent_id} fills {uses} rows — verify manually")

    writes.sort(key=lambda w: (w.note_id, w.field, w.op))
    # De-duplicate identical (note, field, new_value) rows (scene + word
    # matchers can agree on the same fill).
    deduped: list[PlannedWrite] = []
    seen: set[tuple] = set()
    for w in writes:
        k = (w.note_id, w.field, w.op, w.new_value)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(w)
    return Plan(version=PLAN_VERSION, writes=tuple(deduped), reasons=reasons, warnings=warnings)
