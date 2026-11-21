"""Pure text normalization for Mining Fixer matching keys.

No anki/aqt imports: this module must stay importable by the headless
prototype and by tests without Anki installed.

Normalization produces *matching keys* only. Values copied into notes are
never normalized.

Field formats observed in the real collection:

- ``Sentence``: subtitle text with optional speaker/stage tags in square or
  round brackets (``[TEL](受付)``), the continuation marker ``➡``, HTML
  markup, sound tags and images.
- ``furigana``: Yomitan-style per-kanji ruby written inline as ``漢字[かんじ]``
  (base form outside, reading inside brackets), with kana/okurigana
  interleaved verbatim.
- ``reading``: plain kana (hiragana or katakana).
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "REPAIR_RULES",
    "MorphToken",
    "furigana_base",
    "is_empty_glossary_shell",
    "norm_sentence",
    "norm_sentence_match",
    "norm_word_text",
    "norm_reading",
    "reading_from_furigana",
    "repair_sentence",
    "furigana_base_matches",
    "word_key",
    "strip_renderable",
    "kana_fold",
]


@dataclass(frozen=True)
class MorphToken:
    """One morphological token produced by a tokenizer (e.g. MeCab). Pure."""

    surface: str
    pos: str
    lemma: str
    reading: str


# --- removals ---------------------------------------------------------------

_SOUND_RE = re.compile(r"\[sound:[^\]]*\]", re.IGNORECASE)
_ANKI_REF_RE = re.compile(r"\[anki:[^\]]*\]", re.IGNORECASE)
_IMG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# Zero-width/format characters that survive NFKC (CJK subtitle dumps carry
# them): ZWSP, BOM, ZWNJ, ZWJ, directional marks, variation selectors.
_INVISIBLE_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff\u200e\u200f\ufe00-\ufe0f]")

# --- punctuation folding ----------------------------------------------------
# Map CJK and fullwidth punctuation onto canonical ASCII forms so that
# subtitle dumps that disagree on fullwidth/halfwidth style still match.
_PUNCT_TABLE = {
    "。": ".",
    "．": ".",
    ".": ".",
    "、": ",",
    "，": ",",
    ",": ",",
    "！": "!",
    "!": "!",
    "？": "?",
    "?": "?",
    "：": ":",
    ":": ":",
    "；": ";",
    ";": ";",
}

_PUNCT_TRANSLATION = str.maketrans(_PUNCT_TABLE)

# --- trailing continuation / truncation markers -----------------------------
# Subtitle dumps end lines with a continuation arrow (➡), ellipsis or
# tildes. Strip them at the tail of a matching key so a fragment whose dump
# dropped the marker still matches the parent scene line.
_TRAILING_MARKS_RE = re.compile(r"(?:[\u27a1\u21d2\u2192\u2026\uff5e~\ufe0f\s])+$")

_WHITESPACE_RE = re.compile(r"\s+")
_LATIN_RE = re.compile(r"[A-Za-z0-9]")
#: A space that separates two alphanumeric tokens. Japanese typography puts
#: spaces next to CJK words for readability (``OK 少年``); those spaces are
#: presentation, not identity, so the matching key drops them. A Latin word
#: boundary (``a b`` vs ``ab``) stays, as does a digit boundary.
_LEXICAL_SPACE_RE = re.compile(r"(?<![A-Za-z0-9]) | (?![A-Za-z0-9])")

# Sentence-internal brackets that are load-bearing for scene identity:
# stage directions and speaker tags. Kept verbatim (after NFKC, which
# decomposes fullwidth round brackets ()() but leaves 【】 and ⦅⦆ intact).
#
# NOT removed anywhere: scene brackets, quotes, ➡, 。、 (only folded to their
# canonical forms).


def _strip_markup(text: str) -> str:
    """Remove sound tags, images, HTML tags and invisible characters."""
    if "[" in text:
        text = _SOUND_RE.sub("", text)
        text = _ANKI_REF_RE.sub("", text)
    if "<" in text:
        text = _IMG_RE.sub("", text)
        text = _TAG_RE.sub("", text)
    if "&" in text:
        text = html.unescape(text)
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE_RE.sub("", text)
    return text


_BRACKET_MASK_RE = re.compile(r"\[[^\[\]]*\]|\([^()]*\)|【[^【】]*】")


def _collapse_whitespace(text: str) -> str:
    """Keep only lexical spaces; drop typographic ones.

    Japanese typography puts spaces around CJK runs for readability
    (``OK 少年``, ``お願い しかと``); those spaces carry no meaning and must
    not decide whether two scene sentences match. A space between two Latin
    or digit tokens *is* lexical (``a b`` never matches ``ab``), so it is
    kept. Bracket payloads (``[TEL]``, ``(受付)``) are masked before the
    Latin test so speaker tags on Japanese lines do not flip the rule.
    """
    probe = _BRACKET_MASK_RE.sub("", text)
    if not _LATIN_RE.search(probe):
        return _WHITESPACE_RE.sub("", text)
    return _LEXICAL_SPACE_RE.sub("", _WHITESPACE_RE.sub(" ", text)).strip()


def norm_sentence(text: str) -> str:
    """Normalization used for the scene-containment key (brackets kept)."""
    return _collapse_whitespace(_strip_markup(text).translate(_PUNCT_TRANSLATION))


def norm_sentence_match(text: str) -> str:
    """Like :func:`norm_sentence`, plus trailing continuation-marker strip.

    The mining export leaves a stray ``>`` glued to a bolded word
    (``<b>芸者&gt;</b>``), so the child's sentence and its parent's differ by
    one character and containment fails — 85 cards in the newest 300 had no
    audio-bearing parent for exactly this reason (author-reported 芸者,
    ハマり, ギャンブル依存症). Angle brackets carry no meaning in these
    sentences, so the *matching key* drops them; stored values are never
    touched.
    """
    return _TRAILING_MARKS_RE.sub("", norm_sentence(text)).replace(">", "").replace("<", "")


def norm_word_text(text: str) -> str:
    """Normalization for the word-identity key (Expression)."""
    return norm_sentence(text)


def strip_renderable(text: str) -> str:
    """Plain-text rendering of a field value (for junk detection/preview)."""
    return _collapse_whitespace(_strip_markup(text).translate(_PUNCT_TRANSLATION))


_KANA_FOLD_TABLE = str.maketrans({chr(c): chr(c - 0x60) for c in range(0x30A1, 0x30F7)})


def kana_fold(text: str) -> str:
    """Katakana (U+30A1-U+30F6) to hiragana by code-point offset.

    Does not fold the long-vowel mark (ー) or dakuten: folding those merges
    distinct words (おばさん/おばあさん, か/が).
    """
    return text.translate(_KANA_FOLD_TABLE)


def norm_reading(text: str) -> str:
    """Normalization for readings: markup strip, NFKC, katakana -> hiragana."""
    return kana_fold(_collapse_whitespace(_strip_markup(text).translate(_PUNCT_TRANSLATION)))


# --- furigana ---------------------------------------------------------------

_KANJI_RE = re.compile(r"[㐀-\u4dbf\u4e00-\u9fff\u3005\u3006]")
_FURIGANA_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_FURIGANA_ANNOTATION_RE = re.compile(r"\[[^\[\]]*\]")


def reading_from_furigana(furigana: str) -> str | None:
    """Derive a plain reading from Yomitan-style inline ruby.

    Walks the string left-to-right: kanji runs disappear (their reading is
    the following bracket payload), brackets dissolve into their payload,
    bare kana survives. ``死[し]に 至[いた]る`` -> ``しにいたる``.

    Returns ``None`` when the markup is malformed (unbalanced brackets) or
    the payload contains characters a reading cannot have (kanji or Latin
    beyond the long-vowel mark, the ・ separator and iteration marks).
    """
    if not furigana.strip():
        return None
    if furigana.count("[") != furigana.count("]"):
        return None
    out: list[str] = []
    pos = 0
    for match in _FURIGANA_BRACKET_RE.finditer(furigana):
        base = furigana[pos : match.start()]
        out.append(_KANJI_RE.sub("", base))
        out.append(match.group(1))
        pos = match.end()
    out.append(_KANJI_RE.sub("", furigana[pos:]))
    reading = "".join(out)
    reading = kana_fold(
        _WHITESPACE_RE.sub("", _strip_markup(reading).translate(_PUNCT_TRANSLATION))
    )
    if not reading:
        return None
    # A reading may contain kana, the long-vowel mark, the middle dot and
    # iteration marks only.
    if not re.fullmatch(r"[ぁ-ゖー・ヽヾ]+", reading):
        return None
    return reading


def word_key(expression: str, reading: str, furigana: str = "") -> tuple[str, str] | None:
    """Word-identity key: (normalized expression, normalized reading).

    The reading is normalized; when the reading field is blank it is
    derived from the furigana field (``None`` on failure). Returns ``None``
    when no usable expression exists (empty or sentence-length values are
    the caller's decision).
    """
    expr = norm_word_text(expression)
    if not expr:
        return None
    read = norm_reading(reading)
    if not read:
        read = reading_from_furigana(furigana) or ""
        read = kana_fold(read)
    return (expr, read)


def furigana_base(furigana: str) -> str:
    """Base text of a Yomitan-style inline ruby value (``一握[ひとにぎ]り``).

    Bracket payloads stripped, markup removed, whitespace squeezed —
    ``一握[ひとにぎ]り`` → ``一握り``. Returns ``""`` for an unusable value:
    empty, unbalanced brackets (the engine mangled it), HTML ``<ruby>``/
    ``<rt>`` markup (the ``<rt>`` payload is a reading, not base text; left
    in it would corrupt the base into ``温あたためる``) or nothing left
    outside the annotations. Pure; the Expression-repair oracle reads the
    dictionary form out of the note's own furigana with this.
    """
    if not (furigana or "").strip():
        return ""
    lowered = furigana.lower()
    if "<ruby" in lowered or "<rt" in lowered:
        return ""
    if furigana.count("[") != furigana.count("]"):
        return ""
    base = _FURIGANA_ANNOTATION_RE.sub("", furigana)
    return _WHITESPACE_RE.sub("", strip_renderable(base))


def furigana_base_matches(generated: str, sentence: str) -> bool:
    """True when a generated furigana value carries exactly the sentence.

    The deck's ruby convention is per-kanji bracket annotations
    (``漢字[かんじ]``); Japanese Support's reading engine emits the same
    shape. The guard: the generated value's base text — bracket readings
    stripped, markup removed, whitespace squeezed — must equal the
    sentence's plain text (square-bracket speaker tags such as ``[TEL]``
    are stripped on the sentence side by the same convention). Unbalanced
    brackets mean the engine mangled the text; that is a mismatch too.
    Pure.
    """
    if (generated or "").count("[") != (generated or "").count("]"):
        return False
    base = _FURIGANA_ANNOTATION_RE.sub("", generated or "")
    plain_sentence = _FURIGANA_ANNOTATION_RE.sub("", sentence or "")
    return _WHITESPACE_RE.sub("", strip_renderable(base)) == _WHITESPACE_RE.sub(
        "", strip_renderable(plain_sentence)
    )


#: Tags whose presence alone carries no information: a field holding only
#: these (the Yomitan glossary shell `<div class="yomitan-glossary"><ol></ol>
#: </div>` and its variants) renders nothing.
_CONTAINER_TAG_RE = re.compile(
    r"</?(?:div|ol|ul|li|span|p|br|table|tbody|thead|tr|td|th|b|i|u|s|em|strong"
    r"|font|small|big|sub|sup|h[1-6]|dl|dt|dd)\b[^>]*>",
    re.IGNORECASE,
)


def is_empty_glossary_shell(value: str) -> bool:
    """True when a definition field holds only empty container markup.

    The deck's Yomitan exports leave shells such as
    ``<div class="yomitan-glossary"><ol></ol></div>`` when a dictionary
    entry had no body — invisible on the card, but they count as *occupied*
    (QUALITY Q-D1), so they also block the word-fill. Clearing one loses
    nothing: the rendered text is empty before and after.

    Never true when the value is already blank (nothing to clean) or when it
    holds media (``[sound:...]``, ``<img>``, ``[anki:...]``) or any visible
    text — a real definition is never touched. Pure.
    """
    text = value or ""
    if not text.strip():
        return False
    if _SOUND_RE.search(text) or _ANKI_REF_RE.search(text) or _IMG_RE.search(text):
        return False
    residue = html.unescape(_CONTAINER_TAG_RE.sub("", text))
    return not residue.replace("\u00a0", " ").strip()


# --- sentence repair (bulk-fix dimension 2: subtitle noise) -----------------
#
# The configured deck's Sentence fields are subtitle dumps: a continuation
# arrow (➡ / → / ⇒) at a line tail marks "the line continues". The census of
# 2026-11-15 found such arrows in 0.4% of the deck's 17,589 notes (<br> in
# 72.1%, padding runs in 0.3%). The owner decision of 2026-11-15 keeps the
# shipped scope to the arrows: `<br>` line breaks and spacing are the
# sentence's pause structure and stay exactly as mined — the future
# direction is deriving the breaks from the audio itself (BULK_FIX.md,
# "Audio-aligned line breaks"). Repair rewrites ONLY the noise it ships;
# bold markup, sound tags, images, brackets and every spoken character pass
# through untouched.

REPAIR_RULES = (
    "flatten-div-blocks",
    "strip-arrows",
    "join-speaker-lines",
    "strip-dash",
    "strip-nbsp",
)

#: The stray horizontal bar (U+2015) the Mining cards' Sentences carry — the
#: owner's 2026-11-20 report: the character is present in the cards and is
#: unwanted. `strip-dash` removes it in the Sentence field; `RUBY_EXCLUDED_RULES`
#: carries that scope so every caller that rewrites the ruby
#: ("Sentence (furigana)") leaves it alone (the ruby is the sentence's
#: furigana layer and must describe the sentence the card ends up with —
#: Q-D15).
_DASH_RE = re.compile("\u2015+")
RUBY_EXCLUDED_RULES = frozenset({"strip-dash"})

_LATIN_NBSP_LATIN = re.compile(r"([A-Za-z0-9])(?:&nbsp;|\u00a0)+([A-Za-z0-9])", re.IGNORECASE)
_NBSP_RE = re.compile(r"&nbsp;|\u00a0", re.IGNORECASE)


def _strip_nbsp(text: str) -> str:
    s = _LATIN_NBSP_LATIN.sub(r"\1 \2", text)
    return _NBSP_RE.sub("", s)


# A continuation arrow only ever marks "the line continues", so it is
# stripped only at a line tail — immediately before a <br>, a newline or the
# end of the value. The same glyphs in mid-line positions are real notation
# (関数f:X→Y) and are never touched.
_ARROW_TAIL_RE = re.compile(
    "[\u2192\u21d2\u27a1]+\ufe0f?(?=[ \t]*(?:<br\\s*/?>|\n|$))", re.IGNORECASE
)
# A speaker/stage tag alone on a line — ``(荒木)``, ``（葉子）`` — followed by
# a line break: the break is the subtitle's wrapping, not the speech (owner
# report: "(荒木)<br>相手が あなたなら…"). Only short parenthesised tags are
# joined, so a real parenthetical sentence keeps its break.
_SPEAKER_LINE_BREAK_RE = re.compile(
    r"(\([^()\n]{1,12}\)|（[^（）\n]{1,12}）)[ \t]*<br\s*/?>"
    r"(?![ \t\r\n]*<br\s*/?>)",
    re.IGNORECASE,
)


# A styled subtitle source (.vtt/.srt) nests the sentence in empty <div>
# wrappers that render as the line breaks they stand for. Whitespace between
# block tags is HTML's own layout, not content (dropped before the rewrite).
_DIV_TRAILING_SPACE_RE = re.compile(r"(</?div[^>]*>)\s+", re.IGNORECASE)
_DIV_LEADING_SPACE_RE = re.compile(r"\s+(</?div[^>]*>)", re.IGNORECASE)
_TAG_TOKEN_RE = re.compile(r"(<[^>]*>)")
_VISIBLE_TEXT_RE = re.compile(r"<[^>]*>")


def _tag_name(token: str) -> str:
    """Lower-case element name of a markup token (``<BR/>`` -> ``br``)."""
    inner = token[1:].lstrip("/").rstrip(">").strip()
    inner = inner.split()[0] if inner else ""
    return inner.rstrip("/").lower()


def _flatten_div_blocks(text: str) -> str:
    """Replace nested ``<div>`` wrappers with plain ``<br>`` line breaks.

    A subtitle source (a styled ``.vtt``/``.srt``) can nest the sentence in
    dozens of empty ``<div>`` wrappers. They render as the line breaks they
    stand for, but they bloat the field and confuse every later pass (author:
    淀む — 100+ tags for four visible lines). A block boundary becomes
    ``<br>``, existing ``<br>`` are kept, and every spoken character and the
    bold markup pass through untouched. A final check refuses the rewrite if
    it would drop any visible character. Idempotent. Pure.
    """
    if "<div" not in text.lower():
        return text
    collapsed = _DIV_TRAILING_SPACE_RE.sub(r"\1", text)
    collapsed = _DIV_LEADING_SPACE_RE.sub(r"\1", collapsed)
    out: list[str] = []
    boundary: list[bool] = []  # True when this entry is a <br> added at a block edge
    pending = False  # a visible text run is open with no line break yet
    for token in _TAG_TOKEN_RE.split(collapsed):
        if not token:
            continue
        if token.startswith("<") and token.endswith(">"):
            name = _tag_name(token)
            if name == "div":
                if pending:
                    out.append("<br>")
                    boundary.append(True)
                    pending = False
                continue
            out.append(token)
            boundary.append(False)
            if name == "br":
                pending = False
            continue
        out.append(token)
        boundary.append(False)
        if token.strip():
            pending = True
    # A block closing at the very end left a break for no line: drop it.
    while out and boundary[-1] and out[-1] == "<br>":
        out.pop()
        boundary.pop()
    flattened = "".join(out)
    if _VISIBLE_TEXT_RE.sub("", flattened) != _VISIBLE_TEXT_RE.sub("", collapsed):
        return text  # never lose a visible character; keep the original instead
    return flattened


def repair_sentence(text: str, rules: Iterable[str]) -> tuple[str, tuple[str, ...]]:
    """Repair subtitle noise in a ``Sentence`` value (pure).

    Config-driven rule list. The shipped rule set is exactly:

    - ``flatten-div-blocks`` — replace nested ``<div>`` wrappers with the
      ``<br>`` line breaks they stand for (runs first: it can expose a
      line-tail arrow or a speaker tag for the rules below);
    - ``strip-arrows`` — remove continuation arrows only at line tails;
    - ``join-speaker-lines`` — join a short speaker tag to its line;
    - ``strip-dash`` — remove the stray horizontal bars (U+2015) the Mining
      cards' Sentences carry (owner report 2026-11-20); the strip is
      Sentence-field scoped — callers that repair the ruby exclude it
      (``RUBY_EXCLUDED_RULES``).

    ``<br>`` line breaks and spacing are otherwise deliberately NOT rewritten
    (owner decision 2026-11-15; ``flatten-div-blocks`` is the owner's
    2026-11-17 addition and only converts block *tags*, never a ``<br>``).
    Unknown rule names are ignored here and warned about by the planner.
    Returns ``(new_text, applied_rules)``; ``applied_rules`` is empty when
    nothing changed. The transformation is idempotent.
    """
    if not (text or "").strip():
        return text, ()
    wanted = tuple(rules)
    applied: list[str] = []
    s = text
    if "flatten-div-blocks" in wanted:
        flattened = _flatten_div_blocks(s)
        if flattened != s:
            applied.append("flatten-div-blocks")
            s = flattened
    if "strip-arrows" in wanted:
        stripped = _ARROW_TAIL_RE.sub("", s)
        if stripped != s:
            applied.append("strip-arrows")
            s = stripped
    if "join-speaker-lines" in wanted:
        joined = _SPEAKER_LINE_BREAK_RE.sub(r"\1", s)
        if joined != s:
            applied.append("join-speaker-lines")
            s = joined
    if "strip-dash" in wanted:
        stripped = _DASH_RE.sub("", s)
        if stripped != s:
            applied.append("strip-dash")
            s = stripped
    if "strip-nbsp" in wanted:
        stripped = _strip_nbsp(s)
        if stripped != s:
            applied.append("strip-nbsp")
            s = stripped
    if not applied:
        return text, ()
    return s, tuple(applied)
