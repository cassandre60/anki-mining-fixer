# Mining Fixer — Implementation Plan

Status: **shipped as v0.3.0** — the add-on installs, previews, applies and
undoes; the audit builder works headless. New work enters through the Backlog
below, never as silent scope creep inside a coding session.

## Shipped slices

- **T1 — Pure matching core + golden fixture.** `normalize`, `fieldconfig`,
  `matching`, `plan`, `report` with no Anki import; scene containment matching
  inside a hard time window with parent ranking and agreement collapsing; word
  groups by `(Expression, reading, furigana)` with provenance and junk
  filters; fills-only invariant; the two original approved replaces; the
  byte-exact golden acceptance test reproducing the author's manual 診察 fix.
- **T2 — Headless audit package builder.** `scripts/build_audit_package.py`
  clones the note type with fresh ids into a separate deck, assigns
  deterministic `uuid5` GUIDs, exports `.apkg` + Markdown report + JSON plan
  log, and purges any previous audit import from its work copy so a rebuild
  cannot ship stale notes.
- **T3 — Bold repair + tag union (`PLAN_VERSION` 2).** `bold_cloze_word`
  (tier 1: cloze body verbatim ×1; tier 2: reading-derived mixed-kana surface
  form ×1; ambiguity → warning) and `add_tags` (union of the target's and its
  contributing sources' tags, reserved tags excluded, never removes).
  Fixed the `<br>`-matches-`<b` defect that had silently disabled every
  repair.
- **T4 — Audit hygiene.** Audit notes carry the same tags as the cards they
  take values from (診察 = `asbplayer yomitan`), no bookkeeping tags at all;
  stable GUIDs make re-imports update in place; `--note-id` pins cards the
  moving "last N" window would drop.
- **T5 — Installable add-on.** Package layout (`mining_fixer/`), entry
  point with two Tools actions, `applier.py` (per-note atomicity, idempotence,
  exact undo), `collection_io.py` (collection adapter + run logs),
  `preview_dialog.py` (per-row checkboxes, not-fixable and warning tabs),
  config defaults shipped as Anki's config, dev install documented.
- **T6 — CI and docs.** `tests` workflow (ruff + suite, no Anki needed),
  actions pinned to current majors, and this documentation set.
- **T7 — Browser integration.** Right-click a selection in the Browse window →
  *preview and apply on selection*: the selection is the target set, the whole
  configured deck stays available as sources, foreign note types and stale ids
  are reported rather than dropped, and duplicates/order are handled in the
  pure `plan.scope_from_selection`. Verified end to end on a copy of the real
  collection (10 selected targets, 16 writes, exact undo; a solo child still
  sourced its parent outside the selection).
- **T9 — Source confidence (wrong-source protection).** A scene source is
  trusted only when it is unambiguous and close: divergent parent payloads
  ship unchecked (`divergent-parents`), containment matches beyond
  `scene_auto_gap_hours` ship unchecked (`far-from-source`), and a
  byte-identical sentence stays trusted regardless of distance. Batch sends
  every such note to review. On a 1,000-note selection this demoted 18 notes
  (48 rows) that would otherwise have been written from an arbitrarily chosen
  parent.
- **T10 — Bulk runs are the default experience.** A batch run ships with
  both information-preserving replaces enabled (`batch.py` defaults), the
  window shows counts and a preview report instead of a decision queue, and
  the dialog states exactly what it will do before the run.
- **T11 — Subtitle-noise repair (bulk-fix dimension 2, B7 slice 1).**
  `normalize.repair_sentence` (`strip-arrows` at line tails only — the
  owner decision of 2026-11-15 keeps `<br>` line breaks and spacing as
  mined; the audio-aligned-breaks direction is recorded in BULK_FIX.md)
  behind the opt-in `repair_sentence` op (`PLAN_VERSION` 3): skipped when
  expansion or bold already owns the `Sentence`, dropped when it would
  change the tested word's occurrence count, idempotent, on by default
  since the owner's automatic-mode decision (2026-11-16) —
  `batch_repair_sentence` with rules `strip-arrows`. Pinned
  against real deck values; the Ring 3 old → new diff on a collection copy
  is still pending.
- **T12 — Furigana regeneration (bulk-fix dimension 3, B7 slice 2).**
  `regenerate_furigana` (`PLAN_VERSION` 4) rebuilds `Sentence (furigana)`
  from the note's own post-write Sentence through the installed Japanese
  Support add-on's MeCab/Kakasi reading (`reading.mecab.reading` — the
  same engine its `regenerateReading` uses, which itself refuses non-empty
  destinations; entry points re-verified 2026-11-15). Bridge:
  `collection_io.japanese_support_furigana_generator` (lazy, `None`
  headless). Guards (Q-D11): generated from the sentence the plan leaves
  behind (repair precedes furigana, one plan), defers to a queued scene
  fill of the field, `furigana-generation-failed` on an empty result or a
  raised engine error, `furigana-base-mismatch` when the ruby's base text
  no longer carries the sentence (pure
  `normalize.furigana_base_matches`), one unavailable-engine warning per
  run, on by default since the owner's automatic-mode decision
  (2026-11-16; `batch_regenerate_furigana`). Ring 3
  battery pending (curated per-class cards, TEST_STRATEGY.md).
- **T8 — Batch mode for hundreds/thousands of notes.** A counts-only dialog
  with a lossless-by-default policy (`batch.py`), opt-in Sentence/Source
  tiers, note- and group-atomic selection, and a review shortlist instead of
  a thousand checkboxes. The run itself: undo log written before the first write, chunked apply, post-write
  verification, and a dry-run report. Hardening shipped with it: distinct
  note rows (a note with several cards appeared twice), case-insensitive tag
  handling, unique undo-log names, field preflight, per-note write failures
  reported instead of aborting, and a log-first single-run path. Verified on
  1000 real notes: 2.2 s plan, 1.5 s apply, verification clean, 793 untouched
  notes byte-identical, re-run a no-op, undo exact. The report is now
  **viewed inside Anki** (2026-11-16; the file became an optional copy in the
  viewer), and the window gained a per-dimension breakdown line plus a capped
  glance list of the planned changes (`report.change_lines`), so a run's
  soundness is checkable without leaving the dialog or drowning in rows.

## Slice map

| Slice | Verified by |
|---|---|
| T1 | Ring 1 golden fixture; `test_matching.py`, `test_plan.py` |
| T2 | Round-trip import of the built `.apkg` (Ring 3) |
| T3 | `test_bold_repair_*`, `test_add_tags_*`; 鬱病 verified end to end |
| T4 | `test_golden_scene.py` tag assertion; apkg tag census |
| T5 | `tests/test_applier.py`, `test_addon_load.py`, Ring 3 apply + undo |
| T6 | Green CI on every push |
| T7 | `test_scope_from_selection_*`, `test_browser_context_menu_*`; Ring 3 selection run |
| T8 | `test_batch.py`, `test_invariants.py` (Hypothesis), `test_report_summary.py` (glimpse); Ring 3 1000-note run |
| T9 | `test_divergent_scene_sources_*`, `test_a_far_scene_source_*`, `test_an_identical_sentence_*`; Ring 3 demotion count |
| T10 | `test_batch.py` (defaults), `tests/test_report_summary.py` (readable preview); Ring 3 run |
| T11 | `test_normalize.py` (repair rules), `test_plan.py` (guards, exclusivity), `test_batch.py` (tier), `test_report_summary.py`; Ring 3 diff pending |
| T12 | `test_normalize.py` (base-text guard), `test_plan.py` (guards, ordering, exclusivity, engine failures), `test_batch.py` (tier), `test_report_summary.py`; Ring 3 battery pending |
| T13 | superseded 2026-11-16 (picker built, then removed at the owner's request); `test_plan.py` keeps the `batch_scene_propagation` gate |
| T14 | `test_plan.py` (expression tier, word-like gate, listening predicate incl. the Frequency exception); Ring 3 battery pending |
| T15 | `test_normalize.py` (`furigana_base`), `test_plan.py` (rewrite + every Q-D12 guard), `test_batch.py` (on-by-default tier with its opt-out key); real card 1736973220751; Ring 3 pending |
| T16 | `test_plan.py` (word-mined fragment completed to the scene block; a card that owns its audio is left alone; a stale fragment ruby regenerated or demoted; a dictionary word recording refused); real card 正道; Ring 3 pending |
| T17 | `test_matching.py` (Hypothesis property test: `SceneParentIndex` matches linear scan under arbitrary timelines and windows), `test_normalize.py` (Hypothesis test: `kana_fold` translation table matches codepoint spec); benchmark verified |
| T18 | `tests/test_plan.py` (note 1793112599834 とんでもない: bold delimiter trimming on repeated words, HTML entities &gt;/&lt;, markup-only verification, second-run idempotence) |
| T19 | `tests/test_plan.py` (Issue #1 notes 淀む, 日差し: inflected and alternate-kanji bold recognition via `morph_tokenizer`, unambiguous inflected bold placement `読む` → `<b>読め</b>`, ambiguity refusal, clause safety, `batch_bold_inflected` config toggle) |
| T20 | `tests/test_packaging.py` (archive structure, content completeness, dev-artefact exclusion, version consistency) |
| T21 | `tests/test_report_summary.py` (static badge lookup, dynamic badge patterns, unknown-badge fallback, exhaustive badge coverage) |
| T22 | `test_normalize.py` (`test_repair_strips_stray_horizontal_bars`), `test_plan.py` (Sentence-field-only strip, shipped default) |
| T23 | reverted 2026-11-21; the deck evidence is in B8/B11 |
| T24 | `test_normalize.py` (`test_repair_strips_nbsp_entities_and_unicode_nbsp`), `test_plan.py` (`test_a_stray_nbsp_is_stripped_from_the_sentence`, `test_strip_nbsp_is_on_by_default_in_the_shipped_config`); `config.json` shipped default |
| T25 | `test_plan.py` (`test_empty_reading_filled_from_own_furigana`), `test_report_summary.py` (`reading-derived-from-furigana` badge coverage) |
| T26 | `test_plan.py` (`test_bold_repair_skips_the_shell_only_definition_listening_shape`); closes B11 |

- **T13 — Dimension picker in the batch window (B7) — superseded.** Built
  on 2026-11-16 with three checkboxes (scene / arrows / furigana) and pure
  overlay helpers (`batch.dimension_flags`, `batch.cfg_for_dimensions`),
  then removed the same day: the owner's decision is **automatic mode** —
  "I want the app to fix things on its own... no boxes, reduce useless
  friction". The window now has no options; every configured dimension
  runs in one pass (`batch.default_ops`, all on by default), and the
  `batch_*` config keys are the escape hatch. The scene-propagation gate
  (`build_plan` honours `batch_scene_propagation`) stays and is tested.
- **T14 — Bold from expression (B8 slice 1).** `plan._bold_repair_for`
  gained a verbatim-Expression tier (`bold-added-to-expression` badge)
  for cards whose `cloze-body` is missing or junk, the word-like gate
  (`plan._expression_is_word_like`: no whitespace/punctuation, at most 10
  characters — clause-like Expressions reported as
  `expression-not-word-like`, never bolded; over-long words skipped
  conservatively) applied to both expression-derived tiers, and the
  template's listening predicate (`plan._is_listening`: tag + usable
  audio, or no Definition/Extended definition/Frequency with Sentence
  Audio — Frequency-without-glosses stays repaired). Inflected matching
  stays open (BULK_FIX slice 8; 278 census cards). Ring 3 battery
  pending.
- **T15 — Expression spelling repair, self-oracled variant (BULK_FIX
  slice 4, F2; `PLAN_VERSION` 5).** The owner's ひと握り card
  (1736973220751) was reported "already complete" while its Expression
  kept the subtitle's surface form — its own `furigana` field records the
  dictionary form (`一握[ひとにぎ]り`). Op `repair_expression`
  (`plan._expression_repair_for`): rewrite the Expression to the furigana
  base when the furigana is well-formed, `reading_from_furigana` equals
  the normalized `reading` field, the base differs from the Expression,
  contains kanji, and passes the word-like gate. Supports kanji completions
  (ひと握り → 一握り) and inflection normalizations to dictionary headword
  (倍にして → 倍にする, 決まってる → 決まる; owner decision 2026-11-18, closing Issue #5).
  Substitutions and kanji-dropping kana preferences stay refused; deferred while
  word-level writes are queued (F2 risk 4: the rewrite must not land in
  the same run as fills sourced from the old spelling's word group). On
  by default under the owner's automatic-mode decision ("I want things
  to be easy", 2026-11-16); `batch_repair_expression` is the escape
  hatch, badges `expression-repaired-from-furigana` and `expression-normalized-to-lemma`,
  old spelling in the run log (Q-D7), reasons `expression-repair-reading-mismatch`,
  `expression-repair-no-kanji-form`, `expression-not-word-like` and
  `expression-repair-not-a-kanji-completion` in the left-alone report.
  The dictionary seam for furigana-less kana Expressions stays future (F2).
- **T16 — Scene completion from the missing audio (Q-D16).** The owner's
  正道 card (1770544325713) kept its two-line word fragment while a run
  filled the sibling 出来's 14.8 s nine-line scene clip: the completion
  guard required `cloze-*` to reconstruct the card's `Sentence`, which a
  word-mined card never satisfies (its `cloze-*` holds only the tested
  word), so the block was refused. `plan._scene_writes_for` now completes
  any card that is still missing its `Sentence Audio` to the parent's whole
  block — the scene matcher already proved the fragment is *contained* in
  it, so nothing is lost (`fragment-kept-in-scene`). The `already_complete`
  convergence guard is replaced by the audio condition: the run fills the
  audio, so the next run sees a card that owns its scene and leaves the
  sentence alone (one run fixes a card, a second is a no-op). A card whose
  `Sentence Audio` is a dictionary pack's word recording (`yomitan_audio…`)
  is still refused as a scene source; `jidoujisho-…` is not such a marker
  (it is Jidoujisho's video-player clip export — all 683 such files in the
  deck appear only in `Sentence Audio`, never in `Word Audio`), so
  `matching.is_word_media` no longer lists it. When the parent lacks ruby
  and the child has a fragment one, the completion ships checked with
  `ruby-will-be-regenerated` if the furigana tier has an engine, and
  unchecked (`stale-fragment-furigana-risk`) otherwise.
- **T17 — Scene parent indexing and fast kana normalization.**
  `matching.SceneParentIndex` pre-sorts the audio-bearing parent timeline once
  per plan and builds a 3-gram inverted index over normalized parent sentences,
  intersecting the rarest 3-character posting list with the `[lo, hi)` timestamp
  bounds via binary search. Mathematically identical to brute-force linear window
  scans (proven by Hypothesis property tests) while yielding a 275× speedup on
  dense 4,000-note benchmarks (0.077 s vs 21.15 s). Replaced `normalize.kana_fold`
  per-character generator with a precomputed `str.maketrans` table (~25× faster),
  vectorized consensus counting in `matching.best_word_source` with
  `collections.Counter` ($O(K)$ vs $O(K^2)$), and cached normalized sentence lookups
  in `plan.build_plan` and `matching.rank_scene_parents`.
- **T18 — Punctuation-wrapped bold trimming (Issue #2).**
  `plan._trim_bold_punctuation` moves outer non-word punctuation and escaped
  entities (`>`, `<`, `&gt;`, `&lt;`, quotes, brackets, Japanese punctuation)
  outside existing `<b>...</b>` spans when the trimmed core matches a recognized
  spelling of the card's word. Preserves the chosen occurrence on repeated-word
  sentences (note 1793112599834 とんでもない: `<b>とんでもない></b>大物か...`
  → `<b>とんでもない</b>>大物か...`), verified strictly markup-only
  (`strip_renderable(new) == strip_renderable(old)`), badge
  `bold-trimmed-punctuation`, with second-run idempotence.
- **T19 — Offline morphological bolding via MeCab (Issue #1, Backlog B10).**
  Pure `MorphToken` dataclass in `normalize.py` and optional injected seam
  `morph_tokenizer` in `plan.build_plan` (keeping the core pure Python).
  Recognizes existing inflected and alternate-kanji bold spans (note 1793400721969
  淀む: `よどん`, note 1792501126791 日差し: `日射し`) as valid bold instead of
  treating them as foreign or missing. Adds Tier 5 bold placement for unbolded
  sentences: when `batch_bold_inflected` is true (shipped on by default in
  `config.json`), tokenizes the sentence and bolds exactly-one matching inflected
  content token (`読む` → `<b>読め</b>`) with badge `bold-added-to-inflected-form`.
  Ambiguity is refused and reported; listening cards and clause-like expressions
  are skipped. Bridged to Japanese Support's bundled offline MeCab binary in
  `collection_io.japanese_support_morph_tokenizer`.
- **T20 — Packaging script and release workflow (Backlog B3 partial).**
  `scripts/build_ankiaddon.py` builds a `.ankiaddon` zip (ZIP_DEFLATED) from
  `mining_fixer/` with the add-on's flat layout at the zip root (`manifest.json`
  and `__init__.py` at top level). Excludes `meta.json`, `user_files/`,
  `__pycache__/`, `*.pyc`, and dotfiles. `.github/workflows/release.yml`
  triggers on `v*` tag push: runs the full test gate (ruff + pyright + pytest),
  builds the archive, and creates a GitHub Release with the `.ankiaddon`
  attached. `tests/test_packaging.py` verifies archive structure, content
  completeness, dev-artefact exclusion, and version consistency between
  `manifest.json` and `pyproject.toml`.
- **T21 — Badge explanations, tooltips, and filter bar (Backlog B5).**
  `report.badge_explanation` maps every static and dynamic badge to a
  human-readable sentence (44 static badges across bold, scene, source
  confidence, furigana, sentence, expression, glossary, tags, and repair
  rules; four dynamic patterns: `source-note:*`, `N-siblings-agree`,
  `added-gap:*`, `expanded-*`). `report.guard_reason` provides plain-language
  tooltips on the preview table's check column explaining why a row is
  checked by default or which safety guard / configuration held it back.
  The preview dialog features badge tooltips on the badges column,
  guard-reason tooltips on the check column with dynamic manual-override
  tracking, per-badge explanations in the detail pane, and a `QLineEdit`
  filter bar above the table (filters rows by expression, field, badge, or
  change summary, case-insensitive).
- **T22 — Stray-dash strip in Sentences (owner report 2026-11-20).** The
  Mining cards' Sentences carried the stray horizontal bar (U+2015) — the
  owner's report of the character being present. `normalize.repair_sentence`
  gained the `strip-dash` rule (added to `REPAIR_RULES`; the shipped
  `config.json` ships it — automatic mode), and the write is the existing
  `repair_sentence` op (`OP_REPAIR_SENTENCE`): checked, badge
  `strip-dash`, guarded by the tested-word occurrence guard (Q-D10). The
  strip is **Sentence-field-only**: `plan._ruby_repair_rules` (with
  `normalize.RUBY_EXCLUDED_RULES`) filters the rule out of the ruby
  ("Sentence (furigana)") repairs — the owner asked for the strip "only
  inside the sentence field, not everywhere" — so the ruby, the Definition
  and every other field keep the bar. Pinned in `test_normalize.py`
  (`test_repair_strips_stray_horizontal_bars`) and `test_plan.py`
  (`test_a_stray_dash_is_stripped_from_the_sentence_field_only`,
  `test_strip_dash_is_on_by_default_in_the_shipped_config`);
  `test_real_cards.py`'s CFG mirrors the new shipped default.
- **T23 — MeCab word-vs-clause oracle (tried, reverted).** Built on
  2026-11-21 and reverted the same day on deck evidence. The token-count
  oracle (`_tokens_are_word_like`, wired into the bold tiers that place a
  new bold) refused legitimate compound verbs and dictionary expressions —
  響き渡る, いろいろあって, 諦めてなかった — while every full-sentence
  Expression in the configured deck was a listening placeholder whose card
  should be skipped as listening (B11). The change therefore suppressed
  correct bolds and fixed no real card; the punctuation/ending/length gate
  is restored. The attempt is recorded so it is not repeated without new
  evidence.
- **T24 — Non-breaking space strip (`strip-nbsp`, owner report 2026-11-21).**
  The deck census revealed `&nbsp;` and `\u00a0` in 8,550 notes across fields
  (and in 316 Sentence fields), dumped by web/subtitle mining. Added `strip-nbsp`
  rule to `normalize.repair_sentence`: converts to a single ASCII space between
  Latin/digit tokens, strips when adjacent to CJK or Japanese punctuation. Unlike
  `strip-dash`, applies to both `Sentence` and its ruby `Sentence (furigana)`
  (not excluded by `RUBY_EXCLUDED_RULES`). On by default in `config.json`.
  Pinned in `test_normalize.py` and `test_plan.py`.
- **T25 — Self-oracled reading fill from furigana (owner report 2026-11-21).**
  The deck census identified 1,577 cards with an empty `reading` field and
  non-empty `furigana`. In solitary cards (orphan mines, books, single scenes),
  `best_word_source` has no sibling to copy from. Added `_self_reading_write`:
  when `reading` is empty and no sibling fills it, `reading_from_furigana`
  derives the kana reading directly from the note's own `furigana` field
  (`OP_FILL`, badge `reading-derived-from-furigana`, self-oracled). Resolves
  1,507 real cards in the deck with 100% precision. Pinned in `test_plan.py`.

## Backlog

- **B7 — Bulk fix by dimensions** (the product direction; design in
  [`BULK_FIX.md`](BULK_FIX.md)): one window, select any number of cards, pick
  the dimensions to fix (source–child propagation, sentence repair, furigana
  generation after the sentence is repaired, later expression repair and
  formatting), press apply. Includes multi-deck selection and, later, other
  note types and languages behind a language/note-type profile. Slice 1 (the
  sentence-repair dimension) shipped as T11, slice 2 (furigana regeneration)
  as T12 — both **on by default** since the owner's automatic-mode decision
  (2026-11-16) — and the dimension picker (T13) was built then removed at
  the owner's request; the repair→propagate DAG sequencing is still open.
- **B6 — Source-first pipeline** (author's idea, text in
  [`FUTURE_FEATURES.md`](FUTURE_FEATURES.md)): normalise a source card before
  its children — strip continuation arrows (line breaks stay as mined; see
  BULK_FIX.md), regenerate its furigana through the installed Japanese
  Support add-on, and only then plan the children. Requires
  a new opt-in policy tier (it rewrites a non-empty source field) and a
  "source changed since this card was filled" check for already-filled
  children.
- **B8 — Bold from expression** (owner ask 2026-11-16; design in
  [`BULK_FIX.md`](BULK_FIX.md) dimension 6 / slice 8). **Shipped portions:**
  the bold repair gained a verbatim-Expression tier (前人未到 shaped cards; T14),
  the word-like gate that refuses clause-like Expressions
  (漢字は読めるようになりたいかな), the template's own listening predicate (tag +
  usable audio, or no glosses/no Frequency with `Sentence Audio`), and offline
  morphological bolding via MeCab (T19). **Tried and rejected 2026-11-21:**
  the MeCab token-count word-vs-clause oracle (reverted in T23). On the real
  deck it refused legitimate compound verbs and dictionary expressions
  (響き渡る, いろいろあって, 諦めてなかった) while every full-sentence
  Expression was a listening placeholder — the real defect is listening
  detection (B11), not clause detection. The punctuation/ending/length gate
  stays.
- **B11 — Listening cards with shell-only definitions are misclassified.**
  Shipped as T26: `plan._is_listening` now treats shell-only definitions as empty
  (`is_empty(v) or is_empty_glossary_shell(v)`) on both definition fields.
  Resolves the 216 misclassified cards in the configured deck; pinned in
  `test_plan.py`.
- **B9 — Dictionary spelling for kana expressions** (owner ask 2026-11-16;
  text in [`FUTURE_FEATURES.md`](FUTURE_FEATURES.md) §F2, design in
  [`BULK_FIX.md`](BULK_FIX.md) dimension 4 / slice 4). Rewrite a kana-only
  `Expression` to its kanji spelling (いきなり → 行き成り, ときに → 時に)
  using an offline dictionary — never a hand-written table. This is a new
  non-empty write on `Expression` (QUALITY Q-D2 needs a seventh approved
  write first), so it ships opt-in with a preview diff, its own badge and
  the run log. Exactly one dictionary form, or the card is reported and left
  alone; the T14 word-like gate applies (ときに is 時 + に). The same lookup
  also supplies bold-match candidates in the other spelling direction.
  Blocked on: dictionary source decision, QUALITY amendment, Ring 3 sample
  review. Not started.
- **B10 — Bold when the sentence spells the word differently.** Shipped as T19
  via offline MeCab morphological analysis (`normalize.MorphToken`,
  `plan._is_morph_match`, `plan._bold_repair_for` Tier 5 with
  `batch_bold_inflected: true`). Closed Issue #1.
- **B2 — Rendered card preview.** Render each row's real front/back HTML
  (Anki's card rendering API) in a side pane, so a reviewer sees the card as
  the template shows it rather than as field values.
- **B3 — AnkiWeb release.** ~~Package script~~ (shipped as T20),
  numeric package id so AnkiWeb can deliver updates, and a changelog.
- **B4 — Multi-note-type configuration.** Today one deck + note type and one
  field-name set; a per-note-type field mapping would let the same install
  serve a second mining setup.
- **B5 — Review ergonomics for guards.** Shipped as T21 (badge tooltips,
  detail-pane explanations, filter bar, check-column guard-reason tooltips with
  manual override tracking).

## Working agreements (from experience)

- One user request = one workstream: finish, verify on a copy, commit, push,
  watch CI.
- Docs change with behaviour in the same commit (see `AGENTS.md` §6).
- Real-collection verification happens on a copy, never on the live profile.
- Root-level scratch files (`*.backup`, `debug_*.py`, probe collections) must
  never be committed — `dist/`, `meta.json`, `__pycache__/` are gitignored for
  the same reason.
- Numbers in docs (test counts, default config values, field names) are facts:
  re-verify or delete them.
