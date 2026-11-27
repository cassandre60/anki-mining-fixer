# Mining Fixer — Codebase & Architecture Guide

> A pedagogical tour: what this add-on does, how the code is arranged, why the
> seams are where they are, and what to be careful about when changing it.
> For the contract-level view see `ARCHITECTURE.md`; for the rules see
> `AGENTS.md`.

---

## 1. What it is, in one sentence

**Mining Fixer completes incompletely-mined Japanese cards by copying from
their enriched siblings — the same scene's fully-mined card, or another card
of the same word.**

The problem it removes: when you mine a sentence with asbplayer + Yomitan, the
first word gets everything (full sentence, furigana, audio, picture,
timestamped source) and every later word from the same scene gets a fragment.
Completing those by hand costs minutes per card, and the data already exists
in the sibling.

```text
診察 card (incomplete)               至急 card (enriched sibling)
  Sentence: "先生は今 診察中ですので➡"     Sentence: full scene, <b>至急</b>
  Source:   blob:https://…            Source:   Dr. Rintaro Episode 6.mp4 (20m19s)
  Audio:    (empty)                   Audio:    [sound:asbp_….webm]
  Picture:  (empty)                   Picture:  <img src="asbp_….jpeg">
  furigana: (empty)                   furigana: full scene with ruby

        ↓ the plan

診察 card (fixed)   Sentence: full scene with <b>診察</b> (bold moved to *this*
                    card's word), + source/audio/picture/furigana copied,
                    cloze-* untouched, tags unioned from the source
```

## 2. The one rule that shapes everything

The add-on must never destroy information and never guess. Everything else
follows: fields are filled, not overwritten; the few overwrites each keep
their payload (the blob URL is logged, the mined fragment survives in
`cloze-*`, bolding changes no text); ambiguous cases become visible *reasons*
instead of coin flips; and every write is previewed before it happens and
logged afterwards so it can be undone.

## 3. The two halves, and why the seam is where it is

```text
pure core (no Anki)                 adapters (Anki / Qt)
normalize · fieldconfig             collection_io  ← reads the collection,
matching · plan · report              executes a decision, writes run logs
applier · batch                     preview_dialog ← per-row preview dialog
                                    batch_dialog   ← one-click bulk dialog
                                    __init__       ← Tools + Browser menus
```

The seam is drawn at "needs a collection or a widget". Consequences:

- the **audit builder** (`scripts/build_audit_package.py`) can produce a
  complete `.apkg` preview with no Anki at all;
- the **suite** runs in CI where Anki is not installed;
- the **apply decision matrix** — the part that can destroy data — is testable
  against a fake `NoteState` reader instead of a real collection.

`tests/test_layer_boundary.py` fails if this is violated. If you find yourself
wanting to import `aqt` inside `plan.py`, the logic belongs in `collection_io`
or the answer is "pass it in".

## 4. Reading the code in order

1. **`normalize.py`** — text → matching keys. Sentence normalization drops
   speaker/stage brackets, HTML and sound tags; `word_key` builds the
   `(Expression, reading, furigana)` group key; `kana_fold` and
   `strip_renderable` are the two tiny helpers the rest of the code leans on
   (the latter is what proves a bold write changed no text).
2. **`fieldconfig.py`** — what a field *is*: empty (`is_empty`), an asbplayer
   blob (`is_blob_source`), media references (`sound_files`, `image_files`),
   and `FIELD_NEVER_COPY` (`cloze-prefix/body/suffix` and
   `Definition`/`Extended definition`).
3. **`matching.py`** — the two matchers. Scene: candidate = unenriched card,
   parent = enriched card whose normalized sentence contains it, searched
   inside a hard time window and ranked (exact → tightest → smallest gap),
   with identical outcomes collapsing into agreement groups. Word: grouping
   by `word_key` plus provenance and junk filters. Read
   `find_scene_parents` and `word_groups` and you know the whole match layer.
4. **`plan.py`** — the heart. `build_plan` walks the scoped notes and emits
   `PlannedWrite` rows and `reasons`. Guard helpers live next to their op:
   `_fragment_recoverable`, `_retarget_bold` (move the scene's bold onto this
   card's word), `_bold_repair_for` (the tiered bold repair),
   `_tags_write_for` (the union). Badges are the audit trail; read them to
   understand why a row exists.
5. **`applier.py`** — the write-time half of safety. `decide` re-asserts every
   accepted row, refuses to write a note where any row went stale, recognises
   already-applied rows, and unions tags. `build_run_log` / `decide_undo`
   are the inverse operation.
6. **`collection_io.py`** — the Anki adapter: build `NoteRecord`s from the
   deck, read `NoteState`, execute a decision (one update per note), and store
   run logs under `<profile>/mining_fixer/undo/`.
7. **`preview_dialog.py`** — presentation only: one row per write, checkbox
   from `default_checked`, tabs for not-fixable and warnings, and Apply,
   which calls `build_decision` then `apply_decision`.
8. **`__init__.py`** — two menu actions, defensive to the point of paranoia
   because an exception here is a "failed to load" dialog for the user.

## 5. The safety ideas, named

- **Preview is the plan.** The dialog renders `build_plan` output; the applier
  consumes the same object. There is no second code path that decides what to
  write.
- **Per-note atomicity.** A plan for one card is a unit: if the sentence moved
  under you, the tags row is dropped too. Half-applied cards are the failure
  mode this exists to prevent.
- **Idempotence.** Re-running the same plan after a successful apply writes
  nothing and says so. That is what makes "apply, look, apply again" safe.
- **Exact undo.** The run log stores `prior` *and* `applied` per field, so
  undo can distinguish "this is still my write" (restore it) from "the user
  edited it since" (leave it, report it).
- **Reported misses.** `plan.reasons` and `plan.warnings` are first-class
  output: the dialog has tabs for them, the Markdown report prints them.

## 6. Cost and care points

- The configured deck can hold ~17k notes of this note type. Record loading is
  one SQL join and fine; the *preview* is scoped to the newest
  `preview_last_n` (default 30) because a 17k-row review is unusable.
- Note ids are millisecond timestamps and are the only time axis available;
  scene matching depends on the machine clock at mining time.
- Media checks hit the filesystem (`col.media.dir()`); an inconclusive check
  keeps the fill rather than dropping it.
- `meta.json` inside the package is a dev-install artefact (gitignored); the
  shipped metadata is `manifest.json`.
- The audit builder copies the profile into a work dir before touching
  anything. Never point it at a live profile without `--work-dir`.

## 7. Where to add things

| You want to… | Touch |
|---|---|
| support another note type's field names | `config.json` (field lists) and possibly `fieldconfig.py` |
| make a match rule stricter/looser | `matching.py` (+ a Ring 0 test, + `QUALITY.md` if it is an invariant) |
| add a new kind of write | `plan.py` (op + guard + badges), `report.py` label, `applier` only if the write type is new, `QUALITY.md` Q-D2 |
| change what apply refuses | `applier.py` + `tests/test_applier.py` + `QUALITY.md` Q-A* |
| change the dialog | `preview_dialog.py` (keep decisions out of it) |
| change scope selection (Browser selection vs newest N) | `plan.scope_from_selection`, `collection_io.plan_preview`, `__init__` menu/context-menu wiring |
| change what a target set grows to (same-line neighbours) | `plan.expand_scope_with_neighbours`, `collection_io.plan_preview` (`fix_neighbours`), the `NEIGHBOUR_LABEL` checkbox in both dialogs, `QUALITY.md` Q-C12 |
| change the dry-run package | `scripts/build_audit_package.py` |

## 8. Glossary

- **scene parent** — the enriched card whose sentence contains the candidate's.
- **cloze trio** — `cloze-prefix` / `cloze-body` / `cloze-suffix`, the mined
  fragment. `body` is the card's word; the trio is never copied.
- **badge** — a short machine-readable note on a row (`source-note:<id>`,
  `2-siblings-agree`, `bold-added-to-surface-form`, …); it is the audit trail
  for why a write was proposed.
- **reason** — why a *note* got no writes (`no-parent-that-fills`,
  `nothing-to-fill`, `opted-out`, `inline-reading-no-ruby`, …).
- **warning** — a free-text diagnostic that does not stop a write
  (e.g. an unfixable bold, a dropped media fill).
- **guarded row** — a row whose guard was conditional: it ships
  `default_checked=False` and the user decides.
- **audit build** — the headless `.apkg` dry run for reviewing a whole plan
  outside the live collection.
