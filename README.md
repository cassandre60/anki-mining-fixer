# Mining Fixer

An Anki add-on that completes incompletely-mined Japanese cards by copying
from their enriched siblings — the already-finished card of the same scene,
or a sibling card of the same word. Built for asbplayer + Yomitan mining
workflows where enrichment is interrupted: the card stays without scene
audio, picture, timestamped source, or the full scene sentence, and fixing
each one by hand costs minutes per card.

**Status: v0.3.1 — working add-on, preview-first.** The matching core, the in-Anki
preview/apply dialog (with undo), offline morphological bolding via MeCab, badge
explanations with tooltips and a filter bar, and the headless audit-package
builder are complete and tested. Vocabulary-specific heuristics are tuned for the
author's deck; matching is conservative by design — it reports what it
cannot fix rather than guessing. See [Roadmap](#roadmap).

## Safety guarantees

These are the rules the core is built around — each one is enforced in code
and covered by tests, not just intended:

1. **Fills only touch empty fields.** A field with any non-whitespace
   content is never written. Markup-only values like `<hr>` count as
   occupied and are never overwritten.
2. **Only eight approved non-empty writes, each information-preserving.**
   - `Source`: the asbplayer blob URL is replaced with the timestamped
     source — the blob URL is recorded in the run log, so nothing is lost.
   - `Sentence`: a card missing its scene audio gets the scene's full text,
     so the words and the clip describe the same speech. Your mined fragment
     is never lost: the scene matcher requires the sibling's sentence to
     *contain* it (a word-mined card keeps only the word in `cloze-*`, which
     is why the whole block was previously refused), and the scene's bold
     markup is moved to your card's word, never left on the sibling's. The
     completion runs only while the card is still missing its audio, so a
     second run is a no-op, and a card that already has its own audio keeps
     its own sentence.
   - Bold markup repair (`bold_cloze_word`): when a card's `Sentence` has
     no bold term at all (its cloze-* hold dictionary-entry text instead
     of the subtitle span), the tested word is wrapped in `<b>` — but only
     when it occurs exactly once, either verbatim from `cloze-body`,
     verbatim from the `Expression` itself, or as a reading-derived
     surface form (鬱病 + うつびょう → うつ病). Markup-only: the rendered
     text is identical, verified per write. Two occurrences = never
     guessed, reported as a warning instead. Expressions that look like a
     whole clause (漢字は読めるようになりたいかな) are refused with a
     reason — never bolded — and audio-only listening cards are skipped
     (their front hides the sentence).
   - Sentence noise repair (`repair_sentence`): a rewrite of the
     card's own `Sentence` that (1) flattens the nested `<div>` block
     wrappers a styled `.vtt`/`.srt` leaves behind into plain `<br>` line
     breaks, (2) removes the subtitle's line-tail continuation arrows
     (`➡`) and (3) joins a short speaker tag like `(荒木)` to its line.
     Existing line breaks (`<br>`) and spacing are otherwise kept exactly as
     mined — they are the sentence's pause structure (owner decision
     2026-11-15, extended 2026-11-17; the recorded future direction is
     deriving breaks from the audio, see `BULK_FIX.md`). The flattening
     refuses any rewrite that would lose a visible character. Mid-line glyphs
     like the math
     arrow in `f:X→Y` are never touched, a card whose `Sentence` is already
     queued for expansion or bold is skipped, and the repair must not
     change how often the tested word occurs. On by default
     (`batch_repair_sentence` is the escape hatch to switch it off;
     `sentence_repair_rules` selects the rules).
   - Furigana regeneration (`regenerate_furigana`): a rebuild of
     `Sentence (furigana)` from the card's own sentence, through the
     Japanese Support add-on you already have (its MeCab/Kakasi reading —
     never a home-grown generator). The ruby is generated from the
     sentence as the plan will leave it (after a queued repair or
     expansion), is skipped when a scene fill already owns that field, and
     is dropped when the engine produces nothing, raises, or returns ruby
     whose base text no longer matches the sentence. On by default
     (`batch_regenerate_furigana` is the escape hatch to switch it off).
   - Expression spelling repair (`repair_expression`): rewrites the card's
     own `Expression` to the dictionary form its `furigana` field already
     records — the card mined ひと握り while Yomitan resolved the lookup
     to 一握り (`furigana`: `一握[ひとにぎ]り`). The furigana base is the
     oracle: the rewrite happens only when the furigana is well-formed,
     reads exactly as the note's `reading` field says, and the base adds
     kanji the Expression is missing (ことば → 言葉) — a spelling that
     merely swaps or drops kanji (気付き → 気づき) is reported and left
     alone. Anything else is a reported reason, never a guess; the rewrite
     is deferred while word-level fills are queued for the same card. On by
     default (`batch_repair_expression` is the escape hatch to switch it
     off).
   - Empty definitions (`strip_empty_glossary`): a definition field that
     holds nothing but empty markup (the Yomitan `<ol></ol>` shell) is
     cleared — it renders as nothing either way, so no information is lost.
     Any media or visible text means the field is never touched.
   - Mis-mined sentences (`recovered-sentence`): when a card's sentence is
     the wrong subtitle block, the app finds the scene by the word itself —
     the same episode's cards that contain it, tightened to the right clip —
     and rebuilds the sentence, its furigana, audio, picture and source in
     one run. A blob link names no episode, so those cards are reported
     instead.
   - Tags (`add_tags`): the target keeps its tags and gains every
     contributing source card's tags — a pure union that never removes;
     Anki control tags (`marked`, `leech`) and this tool's own tags
     (`no-autofill`, `auto-filled`) never propagate.
3. **`Sentence` and `Sentence (furigana)` are atomic.** Both always come
   from the same parent — never mixed ruby from a second note, never stale
   fragment ruby left under a full scene. If the parent has no furigana and
   the child does, the furigana tier rebuilds the ruby in the same run
   (`ruby-will-be-regenerated`); without a working engine the expansion ships
   unchecked (`stale-fragment-furigana-risk`).
4. **Risky rows ship unchecked, not hidden.** Guards like
   `cloze-anchor-not-unique`, `stale-fragment-furigana-risk`, or a furigana
   fill paired with a guarded expansion set `default_checked=False` and carry
   a visible badge. Silence never hides a risky write.
5. **Media is checked on disk first.** A fill referencing a media file that
   no longer exists is dropped with a warning instead of producing a dead
   reference.
6. **`no-autofill` opts a note out as a target.** Hand-edited notes remain
   eligible as sources (they are good sources) and are badged when used.
7. **Every run is reversible.** Every write carries its old value in a JSON
   run log; the future apply step restores field-by-field only where the
   card is untouched since.
8. **Junk is never a source.** Doubled-reading pitch-accent artifacts,
   attribution-only definitions, `<hr>`-only fields, and placeholder values
   are excluded from fills.
9. **Fully offline.** Nothing syncs, nothing contacts the network.

## How matching works

**Scene completion** — an unenriched card's normalized `Sentence` is
matched by containment into enriched cards' `Sentence`, searched only
within a temporal window around the note id (note ids are millisecond
creation timestamps; default ±3 days, hard cap, never widened silently).
Candidates are ranked: exact-equal sentence first, then tightest containing
parent, then smallest id gap. When several parents would write identical
values they collapse into one agreement group (`N-siblings-agree`); genuine
divergence needs manual review.

**Word backfill** — notes are grouped by `(Expression, reading)` (with a
`furigana`-derived reading fallback when `reading` is blank). There is
deliberately no reading-only matching: 氏/詩/師/死 all read し. Empty
word-level fields (`Pitch Accent`, `Frequency`, `Word Audio`, `furigana`,
`reading`) are filled from the highest-provenance / highest-consensus sibling;
`Definition` and `Extended definition` are unique per card and never copied.

## Installing it in Anki

The add-on is the `mining_fixer/` package. Anki discovers any folder in
`addons21/` that contains `__init__.py`, so a symlink gives a live dev
install (edits take effect on the next Anki restart):

```
ln -s "$PWD/mining_fixer" "$HOME/.local/share/Anki2/addons21/mining_fixer"
```

That folder also needs a `meta.json` (Anki writes one when a package is
installed; for a manual install, copy any installed add-on's and set
`name`, `human_version`, `package`). Then, after restarting Anki:

- **Tools → "Mining Fixer: preview and apply…"** — plans the newest
  `preview_last_n` notes of the configured deck (default 30).
- **In the Browser: select notes → right-click → "Mining Fixer: preview
  and apply on selection"** — plans exactly the selected notes. Every note of
  the configured deck still serves as a *source*, so a selection of ten cards
  finds its scene parents and word siblings even when those are not selected.
  Selected notes of another note type are reported and skipped; selections are
  deduplicated and keep their order.

Either entry opens the same dialog: every proposed write is one row (note,
field, the exact old → new value, its source note, its badges). Rows the plan
guarded arrive **unchecked**; the rest are checked. Nothing is written until
you press Apply.

- **Tools → "Mining Fixer: undo last run"** — restores exactly what the
  last apply replaced and removes exactly the tags it added.

- **In the Browser: select notes → right-click → "Mining Fixer: batch-fix
  selection…"** — the bulk mode: select any number of cards, press **Fix N
  note(s)**, done. The window stays quiet: one summary line with the counts —
  how many notes will be fixed and **what the run will do, dimension by
  dimension** (`16 writes: 8 furigana regenerated · 5 bold markup · 3 fill …`),
  how many are unchanged, how many were left alone — plus two capped glance
  lists behind toggles. **Show planned changes (N)** shows one line per note
  (the word, what changes, and a short old → new sample for the sentence
  rewrites — the risky ones); **Show what was left alone (N)** shows word —
  reason. Neither list grows with the selection: at 40 rows it stops and
  points at the report.
  Out of the box it does **everything it can, on its own, in one pass**:
  fills into empty fields, completes sentences from scene sources (the
  mined fragment stays in `cloze-*`), replaces blob `Source` with the
  timestamped source (the URL stays in the run log), strips the
  line-end arrows and the stray line break after a speaker tag such as
  ``(荒木)`` (other line breaks and spacing stay exactly as mined), rebuilds `Sentence (furigana)` through the
  installed Japanese Support add-on — always from the sentence the run
  leaves behind, so cleaning happens before the ruby — and unions tags.
  **There are no options to tick.** One button, one pass.
  The config keys remain the escape hatch if you ever want a dimension
  off: `batch_expand_sentence`, `batch_replace_source`,
  `batch_scene_propagation`, `batch_repair_sentence` (with
  `sentence_repair_rules`), `batch_regenerate_furigana`,
  `batch_repair_expression` — all default true. Without the Japanese Support
  add-on the furigana step reports itself unavailable and writes nothing.
  **View report** opens the full one-page human-readable summary inside Anki
  (word, note, what changes, what was left alone) without touching your
  collection — **Save to file…** inside the viewer keeps a copy under
  `<profile>/mining_fixer/dry-runs/` when you want one. A real run
  takes an **Anki backup** first, writes its **undo log before the first
  write**, applies in chunks, and **re-reads every written value to verify it
  landed**. Rows it cannot trust (a source another card disagrees with, or one
  far outside the mining session) are left alone, never guessed.

Apply is safe by construction:

- **Per-note atomicity.** Every checked row of a note is re-asserted against
  the collection first; if the card changed since the preview, *no* row of
  that note is written. A half-applied card is worse than an untouched one.
- **One Apply fixes everything; a second Apply writes nothing.** The run is a
  complete pass: it never leaves a card for a later press to finish — no
  cascade (a card completed here must not make a neighbour fillable only next
  time), no step deferred. A row already holding its target value is
  recognised as done, never rewritten.
- **Undo log.** Each apply writes `run-<timestamp>.json` under
  `<profile>/mining_fixer/undo/` — old values, applied values, and the
  tags involved. Undo restores only notes whose values are still exactly
  what the run wrote; anything edited since is reported and left alone.
  Logs written before the add-on was renamed (under
  `<profile>/mining_backfill/undo/`) are still found and undoable; that
  folder is only ever read.
- **The tag op only adds**, so tags gained since the preview survive.
- **A scene source is only trusted when it is unambiguous and close.** If two
  cards would give the same child different values, neither is chosen: the
  rows arrive unchecked with `divergent-parents`. A match further than
  `scene_auto_gap_hours` (default 6 h) from the child arrives unchecked with
  `far-from-source`, unless the sentences are byte-identical (self-verifying).
  In a 1,000-note selection this moved 18 notes from "will fix" to "review" —
  exactly the wrong-source cases the batch dialog exists to keep out of an
  automatic run.
- **Nothing is written inside the dialog's event loop.** Apply closes the
  dialog, then writes once the Browser's editor has saved any pending edits —
  so an in-flight editor save cannot overwrite the run, and the write does not
  interleave with Anki's window teardown.

## The audit package

For reviewing a whole run before it touches your collection, the headless
builder still exports a standalone `.apkg`:

```
python3 scripts/build_audit_package.py --profile-dir ~/.local/share/Anki2/User\ 1 --last 20
```

- Imports into deck `zzz-Mining-Fixer-Audit`. Notes carry their target
  card's tags plus the tags of the cards they take values from — a union,
  nothing removed, no audit-bookkeeping tags; the deck is the container.
- Uses a cloned note type (`... (MB Audit)`) with fresh ids, so the import
  can never merge into your live note type or modify an existing note.
- Deterministic GUIDs (`uuid5` of the original note id) keep re-imports of
  rebuilt packages idempotent — the same notes update in place.
- `--note-id <id>` (repeatable) pins extra cards into the scope, for fixes
  explicitly requested by name — the moving "last N" window would
  otherwise drop them as new cards get mined.
- `dist/mining-fixer-report-<run>.md` — per-card preview: every write,
  its source note, badges, and default check state.
- `dist/mining-fixer-plan-<run>.json` — the audit log: every old and new
  value, per field.

After auditing: delete the audit deck and the cloned note type.

## Documentation

| Document | Answers |
|---|---|
| [`PRODUCT.md`](PRODUCT.md) | what this add-on is for, users, principles, non-goals |
| [`MVP.md`](MVP.md) | what is actually built today, limitations, acceptance criteria |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | module map, pipeline, ops table, apply contract |
| [`CODEBASE_GUIDE.md`](CODEBASE_GUIDE.md) | guided tour: reading order, safety ideas, where to add things |
| [`QUALITY.md`](QUALITY.md) | the invariants that must stay true (Q-*) |
| [`TEST_STRATEGY.md`](TEST_STRATEGY.md) | the test rings and what each one proves |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | shipped slices and the backlog |
| [`FUTURE_FEATURES.md`](FUTURE_FEATURES.md) | the author's ideas, incl. the source-first pipeline (not committed scope) |
| [`BULK_FIX.md`](BULK_FIX.md) | the product vision: dimensions, ordering, profiles, build plan |
| [`AGENTS.md`](AGENTS.md) | operating rules for agents working in this repo |

## Development

```
python3 -m pytest         # 312 tests with Anki; 310 + 2 self-skipping without
ruff check . && ruff format --check .
```

The golden test (`tests/test_golden_scene.py`) reproduces a real manual fix
from the author's collection **byte-for-byte on all five fields** — media,
source, furigana, and the sentence with the bold retargeted to the card's
own word. The core (`mining_fixer/` minus the four Qt/Anki-facing modules)
imports no `anki`/`aqt` — enforced by an AST test — so the same code runs
headless, in tests, and inside Anki. `tests/test_addon_load.py` imports the
package the way Anki does, with a stubbed `aqt`, which is what catches a
broken entry point before users see "add-on failed to load".

## Roadmap

- [x] aqt add-on shell: preview dialog, apply with re-assertion, undo log.
- [x] browser integration: run the plan on the selected notes (right-click in
      the Browse window).
- [x] offline morphological bolding: inflected and alternate-kanji bold
      recognition via MeCab (Japanese Support add-on's bundled engine).
- [x] badge explanations, tooltips (badge and check-column guard reasons), and filter bar in the preview dialog.
- [x] `.ankiaddon` packaging script and tag-triggered release workflow.
- [x] CI workflow: `ruff` + `pyright` + the full test suite on every push and
  pull request (`.github/workflows/tests.yml`).
- [ ] rendered front/back HTML per row in the preview, instead of field values.
- [ ] source-first pipeline: strip a source card's line-tail arrows (line
      breaks stay as mined), regenerate its furigana, then fix the
      children — see [`FUTURE_FEATURES.md`](FUTURE_FEATURES.md).
- [ ] AnkiWeb release (yields a numeric package id and self-updates).

## License

AGPL-3.0-or-later. See [LICENSE](LICENSE).
