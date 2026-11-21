"""Scene and word matching over mining-note records.

Pure module: no anki/aqt imports. Callers pass :class:`NoteRecord` values
built from any backend (live Anki, the headless ``anki`` library, or test
fixtures).

Scene matching implements the verified user workflow:

- A *candidate* is an unenriched card (Source is still the asbplayer blob
  URL, no Sentence Audio).
- A *parent* is an enriched card (has Sentence Audio) whose normalized
  Sentence CONTAINS the candidate's normalized Sentence.
- Mining sessions cluster in time, so parents are only searched within a
  temporal window around the candidate's note id (a millisecond creation
  timestamp). Window is a hard cap: no silent widening, misses are
  reported for review.
- Word matching groups notes by (normalized Expression, normalized
  reading) and fills empty word-level fields from the best sibling.
"""

from __future__ import annotations

import bisect
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from .fieldconfig import (
    FIELD_NEVER_COPY,
    eligible_source,
    is_blob_source,
    is_empty,
    looks_yomitan_gloss,
)
from .normalize import (
    norm_sentence_match,
    word_key,
)

__all__ = [
    "DEFAULT_SCENE_FILL_FIELDS",
    "DEFAULT_SCENE_OPTIN_FIELDS",
    "DEFAULT_SCENE_REPLACE_FIELDS",
    "NoteRecord",
    "SceneMatch",
    "SceneParentIndex",
    "WordSource",
    "find_scene_parents",
    "is_word_media",
    "kana_spelling_candidates",
    "rank_scene_parents",
    "word_groups",
    "best_word_source",
]

#: A fragment shorter than this cannot identify a scene ("は", "です").
MIN_FRAGMENT_CHARS = 3
DAY_MS = 86_400_000
# Never widen silently past this; user must change config explicitly.
MAX_WINDOW_MS = 14 * DAY_MS
# A span starting/ending at a punctuation boundary is trustworthy; interior
# spans are allowed (the user's own golden case has no leading boundary)
# but flagged.
_BOUNDARY_CHARS = "。、！？!?「」『』（）()【】….,:;"


@dataclass(frozen=True)
class NoteRecord:
    """Immutable snapshot of one note. The aqt/core seam: plain data only."""

    note_id: int
    notetype_id: int
    fields: dict[str, str]
    tags: frozenset[str] = frozenset()

    def get(self, name: str) -> str:
        return self.fields.get(name, "") or ""


@dataclass
class SceneMatch:
    """One containing parent for a candidate."""

    parent: NoteRecord
    tier_gap_ms: int  # |parent.note_id - candidate.note_id|
    exact_equal: bool  # parent sentence equals candidate sentence
    boundary_ok: bool  # span starts/ends at punctuation boundary
    fillable: int  # number of candidate gaps this parent can fill
    fill_payload: tuple  # values it would write (for agreement checks)


@dataclass
class WordSource:
    """A sibling able to supply one word-level field."""

    record: NoteRecord
    value: str
    provenance: bool  # Yomitan gloss provenance
    consensus: int  # siblings agreeing on the normalized value


# --- scene matching ---------------------------------------------------------


def _normalized(record: NoteRecord, cache: dict[int, str]) -> str:
    if record.note_id not in cache:
        cache[record.note_id] = norm_sentence_match(record.get("Sentence"))
    return cache[record.note_id]


def _boundary_ok(candidate_norm: str, parent_norm: str, start: int) -> bool:
    """Span must start at a boundary and end at parent end or boundary.

    Interior spans are tolerated by the caller (user's golden case) but
    flagged; a span not ending at a boundary is a stronger FP signal and
    returns False, unless the parent ends exactly there.
    """
    ok_start = (
        start == 0
        or candidate_norm[0] in _BOUNDARY_CHARS
        or parent_norm[start - 1] in _BOUNDARY_CHARS
    )
    end = start + len(candidate_norm)
    ok_end = end == len(parent_norm) or parent_norm[end] in _BOUNDARY_CHARS
    return ok_start and ok_end


#: Media that belongs to a *word* card, not to a scene: Yomitan and dictionary
#: packs name their word recordings this way (``yomitan_audio_….ogg``).
#: ``jidoujisho-…`` is deliberately NOT a marker: it is the Jidoujisho mobile
#: video player's clip export, i.e. the scene's own audio/picture. Verified on
#: the configured deck (2026-11-17, read-only): all 683 distinct
#: ``jidoujisho-*`` files appear only in a ``Sentence Audio`` field and never
#: in any ``Word Audio``; the 正道 scene's clip is 14.8 s of the nine-line
#: speech (232 KB) while the same note's dictionary word audio is a separate
#: 3 KB ``local audio_…oga``. Counting ``jidoujisho`` as word-level removed
#: 683 genuine scene sources' media.
_WORD_MEDIA_MARKERS = ("yomitan", "dictionary", "forvo")
_MEDIA_REF_RE = re.compile(r"\[sound:([^\]]+)\]|<img[^>]+src=[\"']?([^\"'>]+)", re.IGNORECASE)


def is_word_media(value: str) -> bool:
    """True when a media field holds word-level media rather than a scene's.

    A card whose Sentence Audio or Picture is a dictionary pack's word
    recording would copy that recording onto a child's scene fields, so it is
    refused as a scene source. Pure.
    """
    for audio_name, image_name in _MEDIA_REF_RE.findall(value or ""):
        filename = (audio_name or image_name or "").lower()
        if any(marker in filename for marker in _WORD_MEDIA_MARKERS):
            return True
    return False


@dataclass
class SceneParentIndex:
    """Pre-sorted timeline and 3-gram inverted index over audio-bearing parents.

    Mathematically equivalent to :func:`find_scene_parents`'s linear window
    scan: since every accepted candidate fragment has length >=
    :data:`MIN_FRAGMENT_CHARS` (3), any parent containing ``ctext`` must also
    contain every 3-character substring of ``ctext``. Intersecting the rarest
    sampled 3-gram posting list with the ``[lo, hi)`` timestamp window yields
    the exact same matches in ascending ``note_id`` order while avoiding
    redundant sorting and substring scans across large decks. Pure.
    """

    parent_ids: tuple[int, ...]
    parents: tuple[NoteRecord, ...]
    parent_norms: tuple[str, ...]
    trigrams: dict[str, list[int]]

    @classmethod
    def from_parents(
        cls,
        parents_by_id: dict[int, NoteRecord],
        cache: dict[int, str] | None = None,
    ) -> SceneParentIndex:
        ids = tuple(sorted(parents_by_id))
        parents = tuple(parents_by_id[nid] for nid in ids)
        norms: list[str] = []
        trigrams: dict[str, list[int]] = {}
        for idx, parent in enumerate(parents):
            ptext = (
                _normalized(parent, cache)
                if cache is not None
                else norm_sentence_match(parent.get("Sentence"))
            )
            norms.append(ptext)
            plen = len(ptext)
            if plen >= MIN_FRAGMENT_CHARS:
                seen_tris = {ptext[i : i + 3] for i in range(plen - 2)}
                for tri in seen_tris:
                    trigrams.setdefault(tri, []).append(idx)
        return cls(
            parent_ids=ids,
            parents=parents,
            parent_norms=tuple(norms),
            trigrams=trigrams,
        )

    def find(
        self,
        candidate: NoteRecord,
        window_ms: int = 3 * DAY_MS,
        min_len: int = 6,
        cache: dict[int, str] | None = None,
    ) -> list[SceneMatch]:
        ctext = (
            _normalized(candidate, cache)
            if cache is not None
            else norm_sentence_match(candidate.get("Sentence"))
        )
        clen = len(ctext)
        if clen < MIN_FRAGMENT_CHARS or not self.parent_ids:
            return []
        lo = bisect.bisect_left(self.parent_ids, candidate.note_id - window_ms)
        hi = bisect.bisect_right(self.parent_ids, candidate.note_id + window_ms)
        if lo >= hi:
            return []

        tri_count = clen - 2
        offsets: Sequence[int]
        if tri_count <= 8:
            offsets = range(tri_count)
        else:
            step = max(1, (tri_count - 1) // 7)
            offsets = (*range(0, tri_count - 1, step)[:7], tri_count - 1)

        best_postings: list[int] | None = None
        for off in offsets:
            postings = self.trigrams.get(ctext[off : off + 3])
            if postings is None:
                return []
            if best_postings is None or len(postings) < len(best_postings):
                best_postings = postings
        assert best_postings is not None

        indices: Sequence[int]
        if len(best_postings) < (hi - lo):
            p_lo = bisect.bisect_left(best_postings, lo)
            p_hi = bisect.bisect_left(best_postings, hi)
            indices = best_postings[p_lo:p_hi]
        else:
            indices = range(lo, hi)

        matches: list[SceneMatch] = []
        cand_id = candidate.note_id
        for idx in indices:
            parent = self.parents[idx]
            if parent.note_id == cand_id:
                continue
            ptext = self.parent_norms[idx]
            if len(ptext) < min_len:
                continue
            start = ptext.find(ctext)
            if start < 0:
                continue
            parent = self.parents[idx]
            matches.append(
                SceneMatch(
                    parent=parent,
                    tier_gap_ms=abs(parent.note_id - cand_id),
                    exact_equal=ptext == ctext,
                    boundary_ok=_boundary_ok(ctext, ptext, start),
                    fillable=0,
                    fill_payload=(),
                )
            )
        return matches


def find_scene_parents(
    candidate: NoteRecord,
    parents_by_id: dict[int, NoteRecord],
    window_ms: int = 3 * DAY_MS,
    min_len: int = 6,
    cache: dict[int, str] | None = None,
    index: SceneParentIndex | None = None,
) -> list[SceneMatch]:
    """Parents within the temporal window whose Sentence contains the candidate's.

    ``parents_by_id`` must contain only audio-bearing enriched notes.
    ``min_len`` is the **parent's** minimum sentence length (a real scene
    block); the candidate only has to be a non-trivial fragment
    (:data:`MIN_FRAGMENT_CHARS`). The window is symmetric around the
    candidate's note id (creation timestamp) and a hard cap; callers widen
    only by re-running with a larger config value.
    """
    if index is not None:
        return index.find(candidate, window_ms=window_ms, min_len=min_len, cache=cache)
    cache = cache if cache is not None else {}
    ctext = _normalized(candidate, cache)
    # A fragment only needs to be non-trivial to identify a scene; the
    # parent's sentence must be a real scene block (``min_len``). The author's
    # 親御 card is a five-character fragment ("親御さんは") and was skipped
    # entirely by the old candidate-side floor.
    if len(ctext) < MIN_FRAGMENT_CHARS:
        return []
    ids = sorted(parents_by_id)
    lo = bisect.bisect_left(ids, candidate.note_id - window_ms)
    hi = bisect.bisect_right(ids, candidate.note_id + window_ms)
    matches: list[SceneMatch] = []
    cand_id = candidate.note_id
    for nid in ids[lo:hi]:
        if nid == cand_id:
            continue
        parent = parents_by_id[nid]
        ptext = _normalized(parent, cache)
        if len(ptext) < min_len:
            continue
        start = ptext.find(ctext)
        if start < 0:
            continue
        exact = ptext == ctext
        matches.append(
            SceneMatch(
                parent=parent,
                tier_gap_ms=abs(parent.note_id - candidate.note_id),
                exact_equal=exact,
                boundary_ok=_boundary_ok(ctext, ptext, start),
                fillable=0,
                fill_payload=(),
            )
        )
    return matches


DEFAULT_SCENE_FILL_FIELDS = ("Sentence Audio", "Picture", "Sentence (furigana)", "Translation")
DEFAULT_SCENE_REPLACE_FIELDS = ("Source", "Sentence")
DEFAULT_SCENE_OPTIN_FIELDS = ("Notes", "context", "Kanji Notes")


def _fillable_and_payload(
    candidate: NoteRecord,
    parent: NoteRecord,
    optin_fields: tuple[str, ...],
    fill_fields: tuple[str, ...] = DEFAULT_SCENE_FILL_FIELDS,
    replace_fields: tuple[str, ...] = DEFAULT_SCENE_REPLACE_FIELDS,
    never_copy: frozenset[str] = FIELD_NEVER_COPY,
    cache: dict[int, str] | None = None,
) -> tuple[int, tuple]:
    """Count gaps this parent can fill and the exact payload it would write."""
    payload: list[tuple[str, str]] = []
    count = 0
    for fname in (*fill_fields, *optin_fields):
        if fname in never_copy or not is_empty(candidate.get(fname)):
            continue
        value = parent.get(fname)
        if not eligible_source(fname, value):
            continue
        payload.append((fname, value))
        count += 1
    if (
        "Source" in replace_fields
        and "Source" not in never_copy
        and (is_blob_source(candidate.get("Source")) or is_empty(candidate.get("Source")))
        and not is_blob_source(parent.get("Source"))
        and not is_empty(parent.get("Source"))
    ):
        payload.append(("Source", parent.get("Source")))
        count += 1
    # The sentence expansion is a write too: a card whose fragment is much
    # shorter than the same-episode block gains the full sentence (and its
    # bold), even when every scene field is already present — the author's
    # 校 card should take the long block from ぜひ's scene.
    if "Sentence" in replace_fields and "Sentence" not in never_copy:
        pnorm = (
            _normalized(parent, cache)
            if cache is not None
            else norm_sentence_match(parent.get("Sentence"))
        )
        cnorm = (
            _normalized(candidate, cache)
            if cache is not None
            else norm_sentence_match(candidate.get("Sentence"))
        )
        if pnorm != cnorm:
            count += 1
    return count, tuple(payload)


def rank_scene_parents(
    matches: list[SceneMatch],
    candidate: NoteRecord,
    optin_fields: tuple[str, ...] = DEFAULT_SCENE_OPTIN_FIELDS,
    fill_fields: tuple[str, ...] = DEFAULT_SCENE_FILL_FIELDS,
    replace_fields: tuple[str, ...] = DEFAULT_SCENE_REPLACE_FIELDS,
    never_copy: frozenset[str] = FIELD_NEVER_COPY,
    cache: dict[int, str] | None = None,
) -> list[SceneMatch]:
    """Order matches by: exact-equality, fillability, tightness, recency.

    1. Parents that fill nothing are dropped.
    2. Exact sentence-equality first (Tier E).
    3. Among containment matches: shorter parent (tightest clip), then
       smaller |id gap| (same session), then more fillable, then lowest id.
    """
    scored: list[SceneMatch] = []
    for m in matches:
        fillable, payload = _fillable_and_payload(
            candidate,
            m.parent,
            optin_fields,
            fill_fields=fill_fields,
            replace_fields=replace_fields,
            never_copy=never_copy,
            cache=cache,
        )
        if fillable == 0:
            continue
        m.fillable = fillable
        m.fill_payload = payload
        scored.append(m)
    scored.sort(
        key=lambda m: (
            0 if m.exact_equal else 1,
            len(
                _normalized(m.parent, cache)
                if cache is not None
                else norm_sentence_match(m.parent.get("Sentence"))
            ),
            m.tier_gap_ms,
            -m.fillable,
            m.parent.note_id,
        )
    )
    # Dedup identical normalized keys (two parents with byte-identical
    # sentences and payloads collapse into one agreement group).
    return scored


def agreement_groups(ranked: list[SceneMatch]) -> dict[tuple, list[SceneMatch]]:
    """Group ranked parents by the payload they would write.

    Parents writing identical values are not "ambiguous": they agree. Only
    divergent payloads need user review.
    """
    groups: dict[tuple, list[SceneMatch]] = {}
    for m in ranked:
        groups.setdefault(m.fill_payload, []).append(m)
    return groups


def kana_spelling_candidates(expression: str, furigana: str) -> list[str]:
    """Spellings of a word with any of its kanji written in kana.

    The deck spells a word inconsistently between its fields and its sentence:
    the word is 類い希 while the sentence says 類いまれ. The note's own
    furigana (``類[たぐ]い 希[まれ]``) carries the per-kanji reading, so every
    combination of "kanji kept / kanji written in kana" is a candidate —
    ``類い希``, ``類いまれ``, ``たぐい希``, ``たぐいまれ``. Up to three kanji
    are expanded (eight spellings); beyond that only the all-kanji and
    all-kana forms are produced, so the list stays small and the caller's
    uniqueness oracle stays meaningful.

    Returns only candidates that contain at least one kanji or equal the
    whole reading, deduplicated, in a stable order.
    """
    import re as _re

    from .normalize import kana_fold

    word = (expression or "").strip()
    ruby = (furigana or "").strip()
    if not word or not ruby:
        return []
    drop_space = not any(ch.isspace() for ch in word)
    kana_run = _re.compile(r"[㐀-\u4dbf\u4e00-\u9fff\u3005\u3006]+$")
    segments: list[tuple[str, str | None]] = []

    def add_literal(text: str) -> None:
        if not text:
            return
        if drop_space:
            text = _re.sub(r"\s+", "", text)
        pieces = [p for p in (text,) if p]
        if pieces:
            segments.append((pieces[0], None))

    pos = 0
    for match in _re.finditer(r"\[([^\[\]]*)\]", ruby):
        prefix = ruby[pos : match.start()]
        annotated = kana_run.search(prefix)
        if annotated is None:
            add_literal(prefix)
        else:
            add_literal(prefix[: annotated.start()])
            segments.append((annotated.group(0), kana_fold(match.group(1))))
        pos = match.end()
    add_literal(ruby[pos:])
    if not any(reading for _, reading in segments):
        return []
    kanji_count = sum(1 for _, reading in segments if reading)
    out: list[str] = []
    if kanji_count <= 3:
        combos = range(1 << kanji_count)
        bit_for: list[int] = []
        index = 0
        for _, reading in segments:
            if reading:
                bit_for.append(index)
                index += 1
            else:
                bit_for.append(-1)
        for mask in combos:
            pieces: list[str] = []
            for (text, reading), bit in zip(segments, bit_for, strict=True):
                if reading is None:
                    pieces.append(text)
                elif mask & (1 << bit):
                    pieces.append(reading)
                else:
                    pieces.append(text)
            out.append("".join(pieces))
    else:
        out.append(word)
        out.append(kana_fold(_re.sub(r"[\[\]]", "", ruby)))
    seen: set[str] = set()
    unique: list[str] = []
    for cand in out:
        cand = cand.strip()
        if cand and cand not in seen:
            seen.add(cand)
            unique.append(cand)
    return unique


def word_groups(records: list[NoteRecord]) -> dict[tuple[str, str], list[NoteRecord]]:
    """Group records by (normalized Expression, normalized reading).

    Reading-only grouping is intentionally absent: it mixes distinct words
    that share a reading (氏/詩/師 all read し). Kana-only expressions are
    returned in their own group; callers may treat them as low-trust.
    """
    groups: dict[tuple[str, str], list[NoteRecord]] = {}
    for r in records:
        key = word_key(r.get("Expression"), r.get("reading"), r.get("furigana"))
        if key is None:
            continue
        groups.setdefault(key, []).append(r)
    return groups


def best_word_source(
    field_name: str,
    candidate: NoteRecord,
    group: list[NoteRecord],
) -> WordSource | None:
    """Pick the best sibling value for one empty word-level field.

    Selection order: Yomitan gloss provenance, then consensus (number of
    siblings agreeing on the normalized value), then earliest note id.
    Junk values are ineligible sources. ``candidate`` itself is excluded.
    """
    candidates: list[WordSource] = []
    for r in group:
        if r.note_id == candidate.note_id:
            continue
        value = r.get(field_name)
        if not eligible_source(
            field_name, value, expression=r.get("Expression"), reading=r.get("reading")
        ):
            continue
        candidates.append(
            WordSource(
                record=r,
                value=value,
                provenance=looks_yomitan_gloss(value),
                consensus=0,
            )
        )
    if not candidates:
        return None
    # Consensus: how many sources share the candidate's normalized value.
    from .normalize import strip_renderable  # local import keeps cycle-free

    norm_values = [strip_renderable(c.value).strip() for c in candidates]
    counts = Counter(norm_values)
    for c, norm_val in zip(candidates, norm_values, strict=True):
        c.consensus = counts[norm_val]
    candidates.sort(key=lambda c: (not c.provenance, -c.consensus, c.record.note_id))
    return candidates[0]


def singleton_kanji_guard(
    candidate: NoteRecord,
    groups: dict[tuple[str, str], list[NoteRecord]],
) -> bool:
    """True when the candidate's kanji Expression looks like a Yomitan
    kanji-attribution artifact (e.g. ``尾ける`` for おける).

    Heuristic: the exact (Expression, reading) group is a singleton while
    another group with the same reading and a kana-only (or kanji-less)
    expression has two or more members. Sense fields must then not be
    auto-filled; media/furigana/reading may still be.
    """
    key = word_key(candidate.get("Expression"), candidate.get("reading"), candidate.get("furigana"))
    if key is None:
        return False
    expr, read = key
    if len(groups.get(key, [])) > 1:
        return False
    if not re.search(r"[㐀-\u4dbf\u4e00-\u9fff]", expr):
        return False  # kana-only singleton is not an artifact
    for (other_expr, other_read), members in groups.items():
        if (
            other_read == read
            and other_expr != expr
            and len(members) >= 2
            and not re.search(r"[㐀-\u4dbf\u4e00-\u9fff]", other_expr)
        ):
            return True
    return False
