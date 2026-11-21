# Mining Fixer — Architecture

Technical structure. `PRODUCT.md` says what it must do, `QUALITY.md` what
must stay true, `TEST_STRATEGY.md` how that is proven.

## Shape

```text
mining_fixer/                 the add-on (Anki imports this folder as a package)
├── normalize.py     pure   text → matching keys (sentences, words, readings)
├── fieldconfig.py   pure   field semantics: emptiness, blob detection, media refs
├── matching.py      pure   NoteRecord → scene parents / word groups
├── plan.py          pure   records + config → Plan (PlannedWrite rows + reasons)
├── report.py        pure   Plan → Markdown run report
├── applier.py       pure   Plan + live values → ApplyDecision (writes / skips)
│                           + write timing, chunking, verification, undo
├── batch.py         pure   batch policy: tiers, group/note atomicity, review
├── collection_io.py  Anki  read the collection, execute a decision, run logs
├── preview_dialog.py  Qt   per-row dialog: rows, checkboxes, tabs, Apply
├── batch_dialog.py    Qt   counts-only dialog: tiers, review shortlist, dry run
├── __init__.py        Qt   entry point: Tools actions + Browser context menu
├── config.json            defaults (Anki merges user edits from meta.json)
└── manifest.json          packaging metadata (package id, version)

scripts/build_audit_package.py   headless: fix-by-copy into an .apkg for review
tests/                           the suite (conftest puts the repo root on sys.path)
```

**The dependency rule:** arrows point one way — Qt/Anki adapters depend on the
pure core, never the reverse. `tests/test_layer_boundary.py` enforces it with
an AST check, which is what lets the headless builder, the suite and CI run
with no Anki installed.

## The pipeline

```text
Anki collection
   │  collection_io.deck_records()      notes of the configured deck + note type
   ▼
NoteRecord list ───────────────────────────────────────────────┐
   │  matching: scene containment (time-windowed) + word groups│  all records stay
   ▼                                                          │  eligible as sources
plan.build_plan(records, cfg, scope_note_ids)                  │
   │                                                            │
   ├── scene writes    (fill / replace Source / expand Sentence)│
   ├── word writes     (fills only)                             │
   ├── bold repair     (markup-only)                            │
   ├── tag union       (add_tags)                               │
   └── reasons + warnings  ← everything it refused to do ───────┘
   ▼
Plan ──► preview_dialog (checkboxes, default from plan guards)
   │        │  user accepts rows
   │        ▼
   │   applier.decide(plan, checked, read_state, applied_tag)
   │        │  re-asserts every row against the live collection; per-note atomic
   │        ▼
   │   ApplyDecision { field_writes, tags_writes, skipped }
   │        ▼
   └──► collection_io.apply_decision()   writes, one update per note
            │
            ▼
        run-<ts>.json   (old → applied per field, tags added/removed)
            │
            ▼  Tools → undo last run
        applier.decide_undo() ──► same execute path
```

The same `Plan` feeds the audit builder (`scripts/build_audit_package.py`),
so the `.apkg` preview and the in-Anki preview cannot disagree.

**Two entry points, one seam.** `build_plan` takes `scope_note_ids` — the
notes to *fix*, while every record stays eligible as a source. The Tools menu
passes the newest `preview_last_n` notes; the Browser context menu passes the
selection (`plan.scope_from_selection` dedupes it and reports ids that do not
resolve), after `collection_io.plan_preview` has loaded any selected note that
lives outside the configured deck. Nothing else differs.

**Batch path.** For a large selection the plan is reduced by `batch.select`
before anything is decided: a policy of allowed ops (lossless by default;
Sentence expansion and Source replacement are opt-in), *note* atomicity (any
guarded row sends the whole note to the review shortlist) and *text-unit*
atomicity (the sentence and its ruby are written together or not at all;
scene audio, picture, translation and source are independent fills). The
dialog shows the resulting counts — filled notes, tag-only notes, untouched,
review — and the run itself: preflight, undo log **before**
the first write, chunked apply, post-write verification.

## Data model

- `NoteRecord` (`matching.py`) — `note_id`, `notetype_id`, `fields`, `tags`.
  The only currency between layers; built from a collection, the headless
  `anki` library, or a test fixture.
- `PlannedWrite` (`plan.py`) — `note_id`, `field`, `op`, `old_value`,
  `new_value`, `source_note_id`, `default_checked`, `badges`, `group_id`.
  A plan is a deterministic, sorted list of these plus `reasons`
  (`note_id → why not`) and `warnings` (free-text diagnostics).
- `NoteState`, `ApplyDecision` (`applier.py`) — the live values read back at
  apply time and the resulting writes/skips.

`PLAN_VERSION` (currently **6**) is bumped whenever the plan's meaning changes;
it is recorded in every audit log so old and new plans are distinguishable.

## The nine ops

| op | may write non-empty? | guard |
|---|---|---|
| `fill` | no | target field is empty (`old_value == ""`) and not in `FIELD_NEVER_COPY` / `never_copy_fields` |
| `replace_source` | yes | old value is an asbplayer blob URL; the URL is kept in the run log |
| `expand_sentence` | yes | the card is missing its `Sentence Audio`; the scene matcher proved its sentence is contained in the new one (so the fragment is kept, Q-D16); the cloze anchor occurs exactly once — or inside the card's own fragment; furigana ships in the same row, is regenerated in the same plan, or the row is unchecked |
| `bold_cloze_word` | yes (markup / noise only) | `strip_renderable(new) == strip_renderable(cleaned_old)`; the word occurs exactly once |
| `repair_sentence` | yes (configured noise only) | only the configured rules change the value; no other `Sentence` write queued; tested-word occurrence count unchanged |
| `regenerate_furigana` | yes | generated from the post-write sentence; no other `Sentence (furigana)` write queued; ruby base text must equal the sentence (`furigana_base_matches`) |
| `repair_expression` | yes | self-oracled from the note's own `furigana` base; reading matches `reading_from_furigana`; word-like kanji completion only; deferred while word fills are queued |
| `strip_empty_glossary` | yes | clears a definition field holding only empty container markup (`normalize.is_empty_glossary_shell`) |
| `add_tags` | yes | `new_value` tags ⊇ `old_value` tags; reserved tags excluded |

`OPS_TOUCHING_NONEMPTY` in `plan.py` is the single list of ops allowed to
overwrite content; a test asserts nothing else ever does.

## Matching

- **Scene.** Candidate = unenriched card (blob `Source` or a missing scene
  field). Parent = enriched card (has `Sentence Audio`) whose normalized
  `Sentence` *contains* the candidate's. Searched only within ±`window_days`
  around the note id, hard-capped by `MAX_WINDOW_MS` — never widened
  silently. `matching.SceneParentIndex` pre-sorts the parent timeline once per
  plan and builds a 3-gram inverted index over normalized parent sentences so
  large-deck plans intersect the rarest 3-gram posting list with `[lo, hi)`
  instead of re-scanning every parent per target. Ranked by exact match, then
  tightest containing parent, then smallest id gap; identical outcomes collapse
  into agreement groups (`N-siblings-agree`), divergence is left for the user.
- **Word.** Grouped by a key derived from `(Expression, reading, furigana)`
  (`normalize.word_key`). No reading-only matching: 氏/詩/師/死 all read し.
  `Definition` and `Extended definition` belong to `FIELD_NEVER_COPY` and are
  never copied between cards; empty Yomitan `<ol></ol>` shells in definition
  fields are cleared by `strip_empty_glossary`.
- **Bold repair** (`plan._bold_repair_for`). Tier 1: the `cloze-body` occurs
  exactly once in `Sentence` → wrap it. Tier 2 (body absent, i.e. `cloze-*`
  hold dictionary-entry text): try `Expression` verbatim. Tier 3: mixed-kana
  spellings derived from `(Expression, reading)` (`_mixed_form_candidates`,
  requires at least one kanji). Tier 4: whole reading (if $\ge 3$ kana).
  Tier 5: offline morphological tokenization via injected `morph_tokenizer`
  (recognizes inflected verbs `よどん` $\leftarrow$ `淀む`, alternate kanji `日射し`
  $\leftarrow$ `日差し`, and bolds unambiguous single content tokens `読む` $\to$
  `読め` behind config toggle `batch_bold_inflected: true`). Trims outer
  punctuation from existing bold tags without altering renderable text.
  Anything ambiguous → warning, no write. Never issued when a `Sentence`
  expansion is queued for the same note (they would fight over the field).

## Apply contract

`applier.py` is pure; it decides, and `collection_io.py` writes.

- **Per-note atomicity.** A row is *stale* if the field no longer holds
  `old_value` (for tags: the note lost a tag the preview relied on). One
  stale row skips the note's *whole* row set — the case that motivated this
  is a card edited between preview and apply, where writing the tags row but
  not the sentence row would be worse than doing nothing.
- **Idempotence.** A row whose target value is already present counts as
  *already applied*; re-running a plan writes nothing and reports
  `already applied`.
- **Tags only grow.** The tag write is a union with the live tag set.
- **Undo.** `build_run_log` records, per note, each `(field, prior, applied)`
  and the tags before/after. `decide_undo` restores only fields still holding
  the applied value, removes only the tags the run added, and reports
  `changed since the run` for anything touched since. Undone logs are
  renamed `undone-*.json` so the same run cannot be undone twice.
- **Timing.** The dialog never writes: Apply records the decision and closes,
  then `run_after_editor_saved` flushes an open Browser editor and writes
  afterwards (with an idempotent safety net if the editor's async save
  callback is lost). Writes therefore never run inside a nested event loop,
  and a pending editor save cannot land on top of them.

## Configuration

`mining_fixer/config.json` is both the shippable default and Anki's config
default; user edits live in `meta.json` and are merged over it by
`mw.addonManager.getConfig`. Keys: `config_version`, `deck_name`,
`note_type_name`, `window_days`, `preview_last_n`, `scene_auto_gap_hours`,
`batch_expand_sentence`, `batch_replace_source`, `batch_scene_propagation`,
`batch_repair_sentence`, `sentence_repair_rules`, `batch_regenerate_furigana`,
`batch_repair_expression`, `min_sentence_len`, `scene_fill_fields`,
`scene_replace_fields`, `scene_optin_fields`, `word_fill_fields`,
`never_copy_fields`, `definition_fields`, `translation_autocheck`,
`notes_autocheck`, `replaces_autocheck`, `optout_tag`, `applied_tag`.

## Why the package layout

Anki puts the shared `addons21/` root on `sys.path` and imports each add-on
folder as a package. Flat top-level modules named `plan`, `normalize` or
`report` would collide with any other add-on using the same names, and a
repo folder name that is not a Python identifier (this repo's own name
contains hyphens) cannot be imported at all. Hence: one valid-identifier
package, relative imports inside it, and a symlinked dev install
(`addons21/mining_fixer → <repo>/mining_fixer`).
