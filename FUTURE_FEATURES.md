# Future Features — ideas, not commitments

Text dump of the author's ideas. Nothing here is in scope; work enters through
`IMPLEMENTATION_PLAN.md` (Backlog B6) only when it is designed, tested and
documented like everything else.

## Vocabulary (the author's lingo)

- **Source card** — a fully mined card: every field filled correctly (full
  scene `Sentence`, `Sentence (furigana)`, scene audio, picture, timestamped
  `Source`, word fields). It is the card the add-on copies *from*.
- **Child card** — a card related to a source card: mined from the same show,
  typically a few minutes apart, and sharing the same sentence block (the
  child's mined fragment is contained in the source's scene sentence). This is
  exactly the relation the scene matcher tries to establish.

## F1 — Fix the source card *before* fixing the children

Today the add-on treats a source card as ground truth: whatever its `Sentence`
holds is copied to the children. That is backwards when the source itself is
imperfect — and asbplayer/Yomitan sources usually are:

- **Arrows.** asbplayer inserts the continuation marker `➡` (and sometimes
  `→`) where a subtitle line was cut. The marker is punctuation noise that the
  reader has to ignore, and it leaks into every child.
- **Spacing and line breaks.** Subtitles are broken with `<br>` and padded
  with spaces for on-screen line fitting, so the text does not correspond
  one-to-one to what the audio says. A card whose `Sentence` reads as a stack
  of screen lines is harder to read and to compare with the audio than one
  flowing sentence.
- **Furigana.** The source may have no `Sentence (furigana)` at all (the
  Yomitan card was mined before furigana was generated), so every child
  inherits a ruby-less sentence.

### Proposed pipeline (source first, children second)

```text
1. Pick the source cards in a selection (they are the cards with audio,
   picture, a full scene sentence and a timestamped Source).
2. Normalise the source's Sentence:
     - remove continuation arrow markers (➡ / →), configurable
     - keep the `<br>`s and spacing exactly as mined (owner decision
       2026-11-15: line breaks are the sentence's pause structure; the
       audio-aligned direction in BULK_FIX.md replaces heuristic joining)
     - (optional) drop speaker/stage tags such as [TEL] (受付)
3. Rebuild the source's Sentence (furigana) from the normalised text.
     Japanese Support (the installed `Japanese Support` add-on, id
     3918629684) exposes `bulkreading.regenerateReadings(nids)` and
     `reading.regenerateReading(note, src_field)`; its public entry refuses
     non-empty destination fields, so the shipped dimension (T12) calls the
     same engine directly — `reading.mecab.reading(text)` — instead of
     writing our own generator. Verify the entry point against the
     installed source before depending on it (re-verified 2026-11-15).
4. Write those two fields on the source card — previewed, checked, logged,
   undoable, exactly like every other write.
5. Only then plan the children, so they inherit the clean sentence and
   furigana in one pass instead of copying the noise first and being fixed
   twice.
```

### Why it matters

- A source card is used by **many** children. Fixing one source fixes N cards;
  fixing children first multiplies the same defect N times.
- Children's `cloze-*` fields still contain the original fragment, so the
  normalisation must not destroy the anchor: the same recoverability rule as
  today (`cloze-prefix + body + suffix` must still appear in the new sentence)
  has to gate the write.
- `Sentence` and `Sentence (furigana)` must always be rewritten together; a
  normalised sentence under old ruby is a broken card (the existing
  `stale-fragment-furigana-risk` guard exists for the same reason).

### Risks / hard parts

1. **This is a non-empty write on a source card** — the largest trust step so
   far. It must be opt-in, previewed with an exact old → new diff, and
   reversible from the same run log as everything else. A new "source
   normalisation" policy tier belongs in `batch.py`, default off.
2. **Arrows and spaces are not always noise.** `➡` can be the author's own
   separator inside a long quote; `→` can be a dialogue dash. The removal list
   must be config-driven and the diff must be shown before applying.
3. **Children already filled from the old source** become stale. Either
   re-plan them in the same run (source first), or mark them for review when
   the source sentence changed after their fill (a "source changed since this
   card was filled" check needs a record of what was copied — the run log has
   it).
4. **Audio coherence cannot be verified offline.** We can normalise the text
   but not prove it matches the audio; the user is the judge. So the preview
   has to make the change obvious (side-by-side old/new, arrow/spacing counts).
5. **Furigana quality** depends on the external add-on and on the text being
   Japanese-clean; generated ruby must be stored exactly as that add-on
   produces it (no re-formatting), and a failed generation must leave the
   field untouched rather than half-written.

### Prerequisites / related backlog

- B4 (multi-note-type configuration) — source card detection depends on field
  names.
- Per-run "diff view" for a whole field (old vs new side by side) — useful
  beyond this feature.
- A `source_auto_gap_hours`-style confidence rule for *which* cards count as
  sources (already partially there: audio + full sentence + timestamped
  source).
- A test fixture with a real arrow/`<br>`-laden source sentence and its
  expected normalised form, plus its regenerated furigana, so the pipeline is
  pinned byte-exactly like the golden scene test.

### Experiments to run before designing it

1. Collect 20 source cards from the real collection; count arrows and
   missing furigana per card — is normalisation a 50% or a 5% problem?
   (Answered for the whole deck by the 2026-11-15 census, BULK_FIX.md:
   arrows 0.4%; `<br>` 72.1% but kept as mined by owner decision.)
2. Run Japanese Support's `regenerateReadings` on those 20 cards manually and
   eyeball the ruby; does it beat what Yomitan already stored?
3. Check whether any existing child cards were already filled from those
   sources (the run logs say so) — that tells us how much migration the
   change implies.

## F2 — Dictionary spelling for kana expressions

Owner ask 2026-11-16: "いきなり should become 行き成り", and likewise
"ときに → 時に". In plain terms: some cards carry a word in kana where a
kanji spelling exists, and the author wants the card's word rewritten to
the kanji form.

**Shipped 2026-11-16 (the self-oracled variant):** when the card's own
`furigana` field records the dictionary form (ひと握り mined under
furigana `一握[ひとにぎ]り`), the furigana base is the oracle — no
dictionary needed. `plan._expression_repair_for` rewrites the Expression
only when the furigana is well-formed, reads exactly as the `reading`
field says, the base is a word-like kanji spelling that differs from the
Expression, and it is a *kanji completion* — the base's kanji strictly
extend the Expression's (ひと握り → 一握り, とっておき → 取って置き); a
substitution or a drop (決まってる → 決まる, 気付き → 気づき, 美味い →
上手い) is reported, never written, and HTML `<ruby>` furigana is not a
usable oracle (QUALITY Q-D12, op `repair_expression`, badge
`expression-repaired-from-furigana`). On by default under the owner's
automatic-mode decision (2026-11-16); `batch_repair_expression` is the
escape hatch. Verified end to end on the author's ひと握り card
(note 1736973220751). What remains below is the dictionary seam for
kana-only Expressions with no usable furigana.

This is **not** the shipped bold repair: that one never rewrites a word, it
only wraps one unique existing occurrence in `<b>`. This feature writes a
non-empty `Expression` — a new approved write (QUALITY Q-D2 today allows
seven, `repair_expression` among them), so it needed a QUALITY amendment
(Q-D12), its own opt-in policy tier and badge, a preview diff and the same
run log/undo as every other write.

### Design sketch

```text
1. Candidate forms come from an offline dictionary, never from a rule:
   the card's own `reading` plus the installed Yomitan dictionaries the
   deck was mined with (or another bundled lookup — no network).
2. Rewrite only when the lookup yields exactly one kanji form for the word.
   Several forms (はし → 橋 / 箸 / 端) or none → reported, never guessed.
3. `Expression` changes; `reading` and `Sentence (furigana)` keep their
   existing values. The rewrite is planned before furigana regeneration so
   the ruby layer stays consistent with the final word.
4. The same lookup supplies a bold-match candidate in the other direction:
   an Expression written in kanji can be found in a kana-spelled Sentence
   (and vice versa). The exactly-one-occurrence rule still applies.
5. Preview shows the old → new word; the run log records it. (Superseded
   on shipping: by owner decision 2026-11-16 the self-oracled furigana-base
   variant runs by default under automatic mode; the dictionary seam for
   kana-only Expressions without furigana stays opt-in until Ring-3
   reviewed.)
```

### Why it matters

- A kana-only Expression is harder to recognise at review time; the kanji
  form is what the author reads as "the word".
- The bold repair (dimension 6) cannot find a kanji spelling in `Sentence`
  when `Expression` is kana. Today `_mixed_form_candidates` only goes the
  other way (kanji headword, kana spelling in the sentence, using the
  reading), and it deliberately refuses pure-kana candidates because they
  would match unrelated common words. A dictionary removes that guesswork.

### Risks / hard parts

1. **The common spelling is often kana.** いきなり is normally written in
   kana; 行き成り is attested but rare. Either any attested kanji form
   qualifies (what the owner asked for) or only the dictionary's common
   spelling (safer, would skip いきなり). Decide explicitly and record it.
2. **ときに is not a lexical unit** — it is 時 + に. The T14 word-like gate
   must apply here too, or a grammar fragment becomes a fake "word".
3. **Kana → kanji is ambiguous where readings merge.** The `reading` field
   fixes pronunciation, not meaning; context is needed. Unique dictionary
   entry only, or report.
4. **`Expression` feeds `word_key` and scene grouping**, so changing it can
   move a card between word groups. Plan the rewrite before children, or in
   a separate pass — an open question.
5. **Anki search and notes history treat the old spelling as the record.**
   Undo comes from the run log, as with every other write.

### Prerequisites / related backlog

- Offline dictionary access (the Yomitan dictionaries installed for mining
  are the obvious source; verify their format and licence before shipping).
- A QUALITY Q-D2 amendment naming the seventh approved write, with its
  information-preservation rule (the old spelling must be recoverable from
  the run log or a sibling field).
- B4 multi-note-type/profile work, since the dictionary and the "kana
  spelling is valid" convention are Japanese-specific.
- B8's MeCab/lemma work may resolve inflections at the same time.

### Experiments to run before designing it

1. Count cards in the configured deck whose `Expression` is kana-only and
   which the dictionaries resolve to a kanji form (one read-only query).
2. Sample 20 of them and ask the author whether the kanji spelling is what
   they want — including the rare ones such as 行き成り.
3. Check whether the dictionaries mark the kana form as the primary spelling
   (いきなり likely is); that decides risk 1 above.

## F3 — Bold the word when the sentence spells it differently

Owner ask 2026-11-16, deferred by the owner the same day ("no, leave them"),
then opened up by the offline MeCab engine: **shipped as T19** (Tier 5 of the
bold repair, `batch_bold_inflected`). The word-vs-clause gate stays the
punctuation/ending/length heuristic; a MeCab token-count oracle was tried and
rejected on deck evidence (see `BULK_FIX.md` slice 8).

In plain terms: a card's word is 読む but the sentence says 読め; the word is
寒い but the sentence says 寒くなかった. Those cards get **no** bold — the
shipped bold repair matches only what it can see exactly (the mined word,
the word as written, or a kana spelling of the leading kanji), and
deliberately does nothing else.

Why the obvious rule was tried and rejected (2026-11-16, read-only sample
over the real class-C cards): cutting the ending off the word and looking
for the rest — 読む → 読 followed by kana — matched 82 of 272 cards but
produced over-long bolds, because Japanese kana runs cross word
boundaries: 甘い became `<b>甘くておいしい</b>`, 好く became
`<b>好いてくれてるって</b>`, 取り留める swallowed
`取り留めたしかしその`. 32 of the 82 ran into the next kanji word. A wrong
bold changes which word the card tests, so the rule must not ship.

### What made it honest (shipped)

- **Segmentation (T19).** The installed Japanese Support add-on's MeCab engine
  splits a sentence into words and gives each word's dictionary form; a
  match then means "exactly one token whose base form is this card's word".
  That is the same engine the furigana regeneration already calls (T12) —
  a second use of it, not a new dependency — and it only exists inside
  Anki.
- **The guards stay unchanged**: exactly one matching token, markup-only
  write, word-like Expression, listening cards skipped, every miss
  reported.
- **Still out of reach, and reported as such**: cards whose data is simply
  wrong (word 三人, sentence says 3人) and kana-only words with no kanji to
  match (びびる → ビビった needs the reading, not the spelling).

### Sizing (superseded by the shipped T19)

1. Run MeCab segmentation over the 272 class-C cards and count how many get
   exactly one matching token — that number, not the stem rule's 82, is the
   real size of the win.
2. Eyeball 20 of the resulting bolds before wiring anything in.

## Where each idea lives (traceability)

Every ask from the author, and where it is written down so it can be built
later. Status is one of *shipped*, *documented* (no code yet).

| Owner ask | Documented in | Status |
|---|---|---|
| Bulk fixer: select many cards, pick dimensions, apply, no per-card review | `BULK_FIX.md` (whole file), `QUALITY.md` Q-C9 | propagation + window shipped |
| Fix the source card before the children | `BULK_FIX.md` §Ordering, this file §F1 | documented (slices 1–2) |
| Sentence repair: remove continuation arrows, fix spacing and line breaks so the text coheres with the audio | `BULK_FIX.md` dimension 2, this file §F1 | documented (slice 1) |
| Generate furigana **after** the sentence is fixed, through the installed Japanese Support add-on | `BULK_FIX.md` dimension 3, this file §F1 | documented (slice 2) |
| Fix the expression itself: inflections, expressions written partly in kana where the kanji form exists | `BULK_FIX.md` dimension 4 | documented (slice 4) |
| Rewrite a kana expression to its kanji spelling (いきなり → 行き成り, ときに → 時に) | this file §F2, `BULK_FIX.md` dimension 4 / slice 4, `IMPLEMENTATION_PLAN.md` B9 | documented (no code yet) |
| Cards with no bold term in `Sentence` (non-listening): bold the expression's inflected occurrence — 前人未到 is a valid target, 漢字は読めるようになりたいかな is not | `BULK_FIX.md` dimension 6 + slice 8 (census 2026-11-16) | shipped (T14 verbatim, T19 inflected) |
| Bold when the sentence spells the word differently (読む → 読め) | this file §F3, `BULK_FIX.md` dimension 6 / slice 8, `IMPLEMENTATION_PLAN.md` B10 | shipped (T19 via offline MeCab) |
| Indentation / formatting | `BULK_FIX.md` dimension 5 | documented (slice 5) |
| Apply to other decks when they are selected | `BULK_FIX.md` §Scope and profiles | targets shipped; sources still limited to the configured deck |
| Apply to other languages (Chinese, …) and other note types | `BULK_FIX.md` §Scope and profiles, slice 6 | documented (profile abstraction) |
| "Ideally there should be no notes that need humans" | `QUALITY.md` Q-C9, `BULK_FIX.md` §The target experience | shipped (left alone with a reason; no decision queue) |
| A report a human can read, not a data dump | `report.render_batch_summary`, `README.md` | shipped |
| Never crash the author's machine again | `AGENTS.md` §2 rules 2–3, Lessons Learned | shipped (rules in force) |
| Fixing a selected card also fixes its neighbours (checkbox); do not re-fix a card that is already fixed | `IMPLEMENTATION_PLAN.md` B12 | documented (no code yet) |

## Other candidates (already listed elsewhere)

- Browser integration for source cards (select sources, fix them, then their
  children in the same run).
- Rendered front/back preview per row (`MVP.md` B2).
- AnkiWeb release and packaging (`MVP.md` B3).
- Anki's own note history / backup browser as the recovery path, instead of
  only our run log.