# Mining Fixer — MVP / Current Scope

`PRODUCT.md` answers *what this add-on is*.
This file answers *what is actually built right now*. Do not widen this
scope inside a coding session — new work enters through
`IMPLEMENTATION_PLAN.md`.

## MVP goal (achieved, v0.2.0)

A user installs the add-on, opens one dialog, reviews the proposed fixes for
their newest mined cards, applies them, and can undo the whole thing — with
the collection never touched by anything the dialog did not show.

## Included capabilities (shipped and tested)

| Capability | Where | Evidence |
|---|---|---|
| Scene completion (full sentence, furigana, audio, picture, timestamped source) | `matching.find_scene_parents`, `plan._scene_writes_for` | golden fixture, byte-exact |
| Word backfill (`Pitch Accent`, `Frequency`, `Word Audio`, `furigana`, `reading`; definitions are never copied) | `matching.word_groups`, `plan._word_writes_for` | unit tests + real-collection run |
| Bold repair when `Sentence` has no bold term (cloze-body → Expression verbatim → surface form; clause-like Expressions refused, listening cards skipped) | `plan._bold_repair_for`, `plan._expression_is_word_like`, `plan._is_listening` | 鬱病 verified end to end; 2026-11-16 census classified 2,136 no-bold cards |
| Subtitle-noise repair (`flatten-div-blocks`, `strip-arrows`, `join-speaker-lines`; `<br>`/padding kept as mined) — on by default | `normalize.repair_sentence`, `plan._sentence_repair_write`, `batch.py` tier | `tests/test_normalize.py`, `test_plan.py`, `test_batch.py` |
| Furigana regeneration through Japanese Support — on by default | `normalize.furigana_base_matches`, `plan._furigana_write_for`, `collection_io.japanese_support_furigana_generator`, `batch.py` tier | `tests/test_normalize.py`, `test_plan.py`, `test_batch.py`; Ring 3 battery pending |
| Expression spelling repair from the note's own furigana (ひと握り → 一握り) — on by default | `normalize.furigana_base`, `plan._expression_repair_for`, `batch.py` tier | `tests/test_normalize.py`, `test_plan.py`, `test_batch.py`; real card 1736973220751; Ring 3 pending |
| Empty-definition cleanup (clears only a definition that renders as nothing) | `normalize.is_empty_glossary_shell`, `plan._empty_glossary_writes` | `tests/test_normalize.py`, `test_plan.py`; on by default |
| One-click bulk fix: every configured dimension in one pass, no options to tick, config keys as the escape hatch | `batch.default_ops`, `batch_dialog.BatchDialog` | `tests/test_batch.py`; owner decision 2026-11-16 |
| Scene-propagation gate (`batch_scene_propagation`: off removes scene fills at plan time) | `plan.build_plan` | `tests/test_plan.py` (scene-off gate) |
| Tag union from contributing sources (never removes) | `plan._tags_write_for` | golden test pins 診察 = `asbplayer yomitan` |
| Preview dialog with per-row checkboxes, not-fixable and warning tabs | `preview_dialog.py` | import check + real-apply run |
| Apply with per-note re-assertion and no-op re-runs | `applier.decide` | `tests/test_applier.py` (13 tests) |
| Undo of the last run | `applier.decide_undo`, `__init__._on_undo` | real-collection run |
| Browser selection as the target scope | `plan.scope_from_selection`, `collection_io.plan_preview`, `__init__._on_browser_context_menu` | `test_scope_from_selection_*`, `test_browser_context_menu_*`, selection run on a real copy |
| Source confidence: divergent or far scene sources never auto-checked | `plan._scene_writes_for` (`divergent-parents`, `far-from-source`) | `test_divergent_scene_sources_*`, `test_a_far_scene_source_*`, `test_an_identical_sentence_*` |
| Batch mode: lossless-by-default policy, text-unit atomicity, source-confidence blocking, no decision queue, log-before-write, chunked apply, post-write verification, dry run | `batch.py`, `collection_io.apply_batch`, `batch_dialog.py` | `tests/test_batch.py`, `tests/test_invariants.py`, 1000-note run on a real copy |
| Audit `.apkg` builder (dry runs) | `scripts/build_audit_package.py` | rebuilt package verified to the note |
| CI gate (ruff + suite) | `.github/workflows/tests.yml` | green on every push |

## Excluded from the MVP

- **Rendered front/back HTML per row.** Rows show field values, not the card
  as the template renders it (B2).
- **AnkiWeb release.** Today it is a dev install: symlink into `addons21/`
  plus a hand-written `meta.json`. Publishing gives a numeric package id,
  self-updates and one-click install (B3).

- **Config dialog.** Defaults ship in `mining_fixer/config.json`; Anki's
  built-in config editor (Tools → Add-ons → Config) shows the merged config
  and stores edits in `meta.json`. No custom config UI (not planned).
- **Any auto-update of the AnkiWeb listing**, any network feature: permanently
  out (PRODUCT.md, non-goals).

## Known limitations

- The Tools menu scope is a moving "last N": a card can leave it as new cards
  are mined. Select the card in the Browser (or pin it with the audit
  builder's `--note-id`) when it matters.
- The word-group key is `(Expression, reading, furigana)`-derived; a card whose
  `reading` field is empty-but-`furigana`-populated is matched on `furigana`.
  There is deliberately no reading-only matching.
- Confidence is expressed as default-check state plus badges, not as a score:
  guarded rows arrive unchecked and the user decides.
- The plan is computed when the dialog opens; a card edited in another window
  mid-review is re-asserted at apply time and reported as `changed since
  preview` rather than retried.

## Acceptance criteria (every release)

- `ruff check .` and `ruff format --check .` clean; `python3 -m pytest` green
  (264 tests with Anki installed).
- CI (`tests` workflow) green on the released commit.
- The golden fixture test still reproduces the author's manual fix
  byte-for-byte on all five fields.
- A real-collection apply + undo run on a **copy** lands every checked write
  verbatim and restores it.
