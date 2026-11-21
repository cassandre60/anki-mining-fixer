# Mining Fixer — Project Notes & Lessons Learned

## Project Notes

Durable, verified facts. Append; do not duplicate. Prune when superseded.

- [2026-11-14, OpenCode] Note ids are millisecond epoch creation timestamps and
  are the time axis for scene matching (window in days, hard-capped).
  Verified by: `matching.py` window math and the audit build's `added-gap:*`
  badges.
- [2026-11-14, OpenCode] The mining note type has 20 fields; the deck holds
  ~17k notes of that type, so deck-wide record loads are fine. The Tools menu
  deliberately scopes to the newest `preview_last_n` (default 30); the Browser
  context menu scopes to the selection.
  Verified by: `deck_records` query timings in the integration run.
- [2026-11-14, OpenCode] In this Anki version the Browser selection API is
  `browser.selected_notes()` (snake_case); `selected_notes_as_cards()` /
  legacy `selectedNotesAsCards` return cards, not notes. The context-menu hook
  is `gui_hooks.browser_will_show_context_menu(browser, menu)`.
  Verified by: `aqt/browser/browser.py` and `_aqt/hooks.py`, and the
  context-menu wiring test.
- [2026-11-14, OpenCode] Anki reads an add-on's packaged `config.json` as the
  default config and stores user edits in `meta.json`; `meta.json` is also
  what supplies the displayed name/version. `manifest.json` is for packaging
  and AnkiWeb.
  Verified by: `aqt/addons.py` (`addonConfigDefaults`, `getConfig`,
  `AddonMeta.from_json_meta`).
- [2026-11-14, OpenCode] Bold markup in `Sentence` is load-bearing: the card
  template's cloze fallback rebuilds the front from `cloze-*` exactly when no
  bold term exists. A card whose `cloze-*` hold dictionary text therefore
  renders the dictionary entry on the front until the repair lands.
  Verified by: reading the author's note-type templates and the 鬱病 card.
- [2026-11-14, OpenCode] `cards ⋈ notes` yields one row per *card*, so a note
  with several cards appeared twice in the record list (duplicate plan rows,
  duplicate writes). Record queries use `select distinct n.id, …`.
  Verified by: duplicate probe on 17,631 notes (`count(*) - count(distinct n.id)`
  = 0 after the fix).
- [2026-11-14, OpenCode] Anki tag matching is case-insensitive
  (`Note.has_tag` → `col.tags.in_list`), so tag unions, staleness checks and
  verification compare tags lower-cased while writing the spelling seen first.
  Verified by: `applier._tag_key` / `_merge_tags` unit tests.
- [2026-11-14, OpenCode] CI needs `hypothesis` (the property tests) in addition
  to pytest and ruff; the add-on itself needs neither.
  Verified by: `.github/workflows/tests.yml` install step + green run.
- [2026-11-14, OpenCode] The old scene planner always picked the largest
  agreement group and marked its rows checked, even when several parents would
  write *different* payloads — the wrong-source failure the author fears at
  scale. On a 1,000-note selection the divergence guard demoted 18 notes (48
  rows) from auto-fix to review; the far-from-session guard demoted none in
  that selection (sessions cluster within hours).
  Verified by: Ring 3 run on a copy + `test_divergent_scene_sources_*`.
- [2026-11-14, OpenCode] Japanese Support (installed add-on id 3918629684)
  exposes `bulkreading.regenerateReadings(nids)` and
  `reading.regenerateReading(note, src_field)` — the entry points a
  source-first furigana pipeline would call. Verify again before depending on
  them; the add-on may change.
  Verified by: reading the installed add-on's source.
- [2026-11-15, OpenCode] The add-on/repo rename to Mining Fixer is complete
  (GitHub `cassandre60/anki-mining-fixer`, package `mining_fixer`). Undo logs
  written before the rename stay undoable: `collection_io.LEGACY_UNDO_FOLDER`
  reads `<profile>/mining_backfill/undo/` but never writes there; the two
  existing logs (run-20261114-*) live there.
  Verified by: `tests/test_applier.py` lookup tests + the logs on disk.
- [2026-11-15, OpenCode] A raw read-only SQL census against the live
  collection needs `con.create_collation("unicase", …)`, and the `decks`
  table stores tree names with `\x1f` separators, so `::`-style name
  equality fails — match deck names in Python instead. Do not write;
  open with `file:…?mode=ro`.
  Verified by: the 2026-11-15 census run.
- [2026-11-15, OpenCode] Sentence-noise census (one read-only query, 17,589
  notes of the configured deck): `<br>` in 72.1%, arrows `➡`/`→` in 0.4%,
  padding runs in 0.3% — the repair dimension is a `<br>`-collapse problem.
  Recorded in `BULK_FIX.md` (open questions).
  Verified by: the census script's printed aggregates.
- [2026-11-15, OpenCode] The collection has a second, empty deck-tree path
  `<Legacy Profile> Life Decks::Japanese::My New Japanese Deck`;
  `config.json` points at the populated `My Life Decks…` one. No action
  taken — data observation only.
  Verified by: the `decks` table listing (0 vs 17,589 notes).
- [2026-11-15, OpenCode] Owner decision: sentence repair ships
  `strip-arrows` only — `<br>` line breaks and padding stay exactly as
  mined (they are the pause structure), and the replacement direction is
  audio-aligned line breaks recorded in `BULK_FIX.md`.
  Verified by: the rollback commit's diff + `tests/test_normalize.py`.
- [2026-11-16, OpenCode] Japanese Support's public `regenerateReading`
  refuses non-empty destination fields, so it cannot repair stale ruby; the
  shipped furigana dimension (T12) calls its engine directly
  (`reading.mecab.reading`, the same function) through an injected
  generator, keeping `plan.py` pure and the suite independent of a live
  MeCab. Feed it the post-write Sentence (repair first), not the mined one.
  Verified by: reading the installed add-on's `reading.py` and the regen
  tests in `tests/test_plan.py`.
- [2026-11-16, OpenCode] "Listening card" is template behaviour, not a
  template: the mining note type has one template (`Card 1`) whose front
  goes audio-only (sentence views removed) when either path fires —
  (a) a `listening` tag token AND usable audio (`Sentence Audio` or
  `Word Audio`), or (b) no `Definition`, no `Extended definition`, no
  `Frequency`, and `Sentence Audio` present. `Frequency` without glosses is
  deliberately NOT listening (the template shows the normal sentence).
  Verified by: the template's qfmt HTML/JS read from the collection
  (read-only) on 2026-11-16.
- [2026-11-16, OpenCode] No-bold census (mining note type, 17,642 notes,
  one read-only query): 2,136 `Sentence` fields have no `<b>` — 307 empty
  (kanji-only cards), 3 tag-listening + 240 no-gloss listening shape,
  828 fixable by the shipped bold repair today (`cloze-body` occurs once),
  475 expression-verbatim word-like, 1 clause-like expression (must stay
  invalid), 278 inflection/mis-mine cases (読む→読め, 寒い→寒くなかった),
  4 with an empty `Expression`. Recorded in BULK_FIX.md dimension 6 /
  slice 8.
  Verified by: the census query's per-class counts.
- [2026-11-16, OpenCode] Bold repair now has three tiers — cloze-body,
  Expression verbatim, reading-derived surface form — each requiring one
  occurrence; expression-derived tiers are gated by a word-like check
  (whitespace/punctuation/length ≤ 10) and by the template's listening
  predicate (`plan._is_listening`). Clause-like Expressions are reported
  as `expression-not-word-like`, never bolded; class-C cards
  (inflected/mis-mined, 278) stay silent when nothing was mined.
  Verified by: `tests/test_plan.py` bold-repair tests + the 2026-11-16
  census.
- [2026-11-16, OpenCode] The okurigana-stem rule for inflected bolding
  was tried on the real class-C cards (read-only scratch experiment) and
  rejected: 82 of 272 matched, 32 in the risky kana+kanji shape, many
  over-long because Japanese kana runs cross word boundaries (甘い →
  甘くておいしい). Do not ship stem matching; MeCab segmentation is the
  only honest oracle. Scratch script deleted.
  Verified by: the experiment output recorded in BULK_FIX.md
  (dimension 6, open questions).
- [2026-11-16, OpenCode] One run fixes a card; a second run is a no-op
  (owner requirement). Two things were needed: a definition field holding
  only the empty Yomitan shell is filled *through* (old_value = the shell,
  `filled-through-empty-shell`) — the clear op is only the no-sibling
  fallback; and a scene expansion/fill cleans the text it copies with the
  configured repair rules (`expanded-<rule>`) — for **both** `Sentence` and
  `Sentence (furigana)`, because the parent's dump carries tail arrows.
  Verified on a copy of the real collection (newest 30 notes): run 1 = 46
  writes on 23 notes, verify clean, undo byte-identical, run 2 = 0 rows.
  The one deliberate second-run pair left: the Expression spelling rewrite
  waits for that card's word fills (F2 risk 4, the T15 session's design).
  Verified by: `test_a_shell_definition_is_filled_in_the_same_run_and_then_is_a_noop`,
  `test_expansion_cleans_the_copied_sentence_in_the_same_write`,
  `tests/test_real_cards.py`, and the Ring 3 replay.
- [2026-11-16, OpenCode] Two corroboration rules lift the source-confidence
  demotion, both from the author's cards: two or more parents writing
  byte-identical payloads (badge `corroborated-against-divergence`), and
  competing parents whose sentences are nested — the same subtitle block at
  different clips (badge `same-scene-tighter-clip`, tighter pick wins).
  Genuinely different scenes still demote. Also: `min_sentence_len` is the
  **parent's** floor; a fragment only needs ≥3 chars
  (`matching.MIN_FRAGMENT_CHARS`, 親御 was a 5-char fragment and was
  skipped). Bold gained a reading tier: the note's own reading in kana of
  ≥3 kana occurring exactly once is bolded (予て → かねて,
  `bold-added-to-reading-form`) — shorter readings and ambiguity are
  reported, never guessed.
  Verified by: `test_two_agreeing_parents_beat_a_divergent_one`,
  `test_nested_scene_clips_are_trusted`,
  `test_a_short_fragment_still_finds_its_parent`,
  `test_bold_repair_uses_the_reading_when_the_sentence_is_all_kana`, and the
  replayed real cards (予て, 自閉症, 造形, 親御 all fixed in one run).
- [2026-11-16, OpenCode] Real cards, not synthetic fixtures, are what catch
  these bugs: the arrows the author still saw lived in `Sentence (furigana)`
  (the repair only cleaned `Sentence`), and a scene fill copied the parent's
  ruby *with* its arrows. Both are fixed; the committed real-card suite
  (`tests/test_real_cards.py` + `tests/fixtures/real_cards.json.gz`, 16
  captured cards, seven classes) caught them within minutes of being written
  and pins the properties: second run = no-op, no arrow in either sentence
  field, no junk shell left, clauses never bolded. Extend that fixture
  (read-only capture) whenever a new real bug is found.
  Verified by: `tests/test_real_cards.py` (9 tests) and the full suite.
- [2026-11-16, OpenCode] Word-anchored scene recovery (QUALITY Q-D14)
  fixes the "I can fix it by hand" cards the author reported: 類い希's own
  Sentence was the wrong subtitle block, so no parent matched — the word
  (written 類いまれ in the same episode) was the right anchor. Candidates
  come from the note's own furigana (every kanji/kana mix), the Source
  must name an episode (blob refused), each candidate must occur exactly
  once, two same-episode cards must match with nested sentences (tighter
  clip wins), and the paired ruby/audio/picture/source come along in the
  same checked write. 此の度 stays refused: its source is a bare blob and
  the only このたび occurrences are a different scene.
  Verified by: `test_a_mismined_sentence_is_recovered_from_the_word_in_the_same_episode`,
  the two refusal tests, and the captured 類い希 card in the real-card suite.
- [2026-11-16, OpenCode] Owner round on live cards after a run — four
  more gaps closed, each verified against the cards named: 類い希's bold
  (the sentence writes 類いまれ; bold now uses the furigana's per-kanji
  spellings, badge `bold-added-to-kana-mix`); 此の度's scene (a blob target
  is recovered when every candidate card agrees on ONE episode — the
  美術/受賞 block — with all fields); 校's expansion (the expansion counts
  as a fill in the ranker, and the word is bolded inside the child's own
  fragment when it repeats in the block); へったくれ's `(荒木)<br>` (new
  repair rule `join-speaker-lines`). Convergence: once a sentence contains
  its own fragment, no run expands it again — run two stays a no-op.
  Verified by: `test_bold_repair_uses_the_furigana_spellings_of_the_word`,
  `test_word_recovery_accepts_a_blob_source_when_the_candidates_agree_on_one_episode`,
  `test_a_completed_sentence_is_never_expanded_again`,
  `test_repair_joins_a_line_break_after_a_speaker_tag`, and the real-card
  suite.
- [2026-11-16, OpenCode] Source-confidence guards (divergent/far scene
  siblings) now drop the *scene rows* only; a guarded (unchecked) row no
  longer suppresses the note's own repairs (bold/arrows/furigana) nor
  blocks the whole note in `batch.select`. Before this, a card whose
  siblings disagreed was frozen: the older-test run on the newest 20
  cards fixed ~3; after the fix ~9 (furigana rows need the real engine in
  Anki, so more in practice). A guarded row is also dropped when a checked
  write now covers the same field (one field, one applied row).
  Verified by: `test_a_disagreeing_scene_does_not_freeze_the_cards_own_repairs`,
  `test_divergent_scene_sources_are_never_auto_checked` (scene rows only),
  and the read-only replay of the author's newest 20 notes.
- [2026-11-16, OpenCode] Owner decision — **automatic mode**: the tool
  fixes on its own, no options to tick. Every dimension is on by default
  (`batch.default_ops`: fills, scene completion, blob-Source replace,
  `strip-arrows`, furigana regeneration; sentence cleaned before its ruby),
  the batch window's dimension checkboxes and their overlay helpers were
  removed the day they shipped, and the `batch_*` config keys remain the
  escape hatch. Anki merges packaged `config.json` defaults under any user
  keys, so a fresh dev install with no overrides follows these defaults.
  Verified by: `tests/test_batch.py` (defaults on, per-key opt-out) and the
  local `meta.json` (no config section).
- [2026-11-17, OpenCode] A ruby is *derived* from the sentence, so the plan
  enforces Q-D15: a `Sentence (furigana)` write whose base text is not the
  final `Sentence` is never checked. The scene payload fill used to copy a
  parent's ruby onto a child whose own sentence was a different block
  (author's どなた/日取り). Separately, `_furigana_write_for` used to feed the
  generator the *first* `Sentence` write in the list, including a guarded
  (unchecked) expansion that never applies — 高鳴る got a scene block's ruby
  on its own sentence. It now uses the checked write.
  Verified by: `test_a_foreign_ruby_is_never_copied_onto_a_different_sentence`
  and `test_furigana_generation_uses_the_sentence_that_will_be_written` (both
  fail on the pre-fix code) + the scene audit (class C 29 → 0 with the
  simulated engine, 0 new violations; without an engine only the pre-existing
  stale rubies that need the in-Anki engine remain).
- [2026-11-17, OpenCode] Owner decision — a new sentence-repair rule
  **`flatten-div-blocks`** leads `sentence_repair_rules`: a styled `.vtt`/
  `.srt` nests the sentence in empty `<div>` wrappers that render as the line
  breaks they stand for (308 of 17,665 notes carry `<div>` in `Sentence`; the
  author's 淀む had 39 wrappers for four visible lines). A block boundary
  becomes `<br>`, existing `<br>` are kept, whitespace between block tags is
  dropped as layout, and the rewrite is refused if it would lose a visible
  character. It runs first so it can expose a line-tail arrow or speaker tag
  to the other rules. The bold markup is preserved — the author's example
  omitted it, but the two forms "look exactly the same for a human" only if it
  stays (a bold word renders differently).
  Verified by: `tests/test_normalize.py` (`test_repair_flattens_nested_div_blocks`
  on the real 淀む value + the inline/whitespace cases; all fail pre-fix) and
  the scene audit class E = 0.
- [2026-11-17, OpenCode] The 5 remaining bold defects in the newest 300 cards
  are tracked as issues #1–#4 (inflected/alternate spellings → MeCab;
  word-plus-punctuation when the word repeats; a word absent from its sentence;
  a clause-like Expression). They are reported, not guessed.
  Verified by: `gh issue list` (4 open) and the scene audit classes D.
- [2026-11-17, OpenCode] `join-speaker-lines` must not join a tag across a
  blank line (`<br><br>`): matching only the first break left the second in
  place, so the next run joined again — 3 values deck-wide were
  non-idempotent since the rule shipped (the 19-card fixture lacks that
  shape). The **whole-deck idempotency sweep** (repair every `Sentence` twice
  over all 17,665 notes) found it; run that sweep before shipping a new
  repair rule, it is cheap and read-only.
  Verified by: `test_repair_join_does_not_cross_a_blank_line` (fails pre-fix)
  and the sweep (non-idempotent 3 → 0).
- [2026-11-17, OpenCode] Scene completion is keyed on the *missing audio*
  (QUALITY Q-D16), not on `cloze-*`: a word-mined card's `cloze-*` holds only
  the tested word, so the old "fragment recoverable from `cloze-*`" guard
  refused every completion it should have made. A card missing its
  `Sentence Audio` is completed to the parent's whole block (the fragment is
  contained in it, so nothing is lost); the run fills that audio, so the card
  never re-expands. A card that already
  carries its own audio keeps its own sentence. Known limitation (QUALITY
  Q-D16): a completed card becomes an audio-bearing scene parent, so a later
  run can fill a neighbour's still-empty scene field from it — a cross-run
  cascade, not a per-card repeat; a one-plan fixpoint over the post-run parent
  set is the moment group's job. `jidoujisho-…` is scene
  audio (Jidoujisho's clip export), not word media — verified 2026-11-17 on
  the deck: all 683 such files appear only in `Sentence Audio`, never in
  `Word Audio`, and the 正道 clip is 14.8 s (232 KB) while the same note's
  dictionary word audio is a separate 3 KB `local audio_…oga`; the
  `is_word_media` marker was a false positive on all of them.
  Verified by: `test_a_jidoujisho_clip_is_scene_audio_and_completes_the_card`,
  `test_a_dictionary_word_recording_is_not_scene_audio`,
  `test_a_stale_fragment_ruby_is_regenerated_when_the_engine_is_available`,
  `test_a_completed_sentence_is_never_expanded_again`, and the 正道 replay.

- [2026-11-17, OpenCode] The furigana engine lookup was wrong: the
  Japanese Support add-on's package `__init__` imports `reading`, and the
  controller is `reading.mecab` (a module-level `MecabController`); the
  package itself has no `mecab`, so `getattr(package, "mecab")` always
  returned None and dimension 3 silently never ran. Because a sentence
  completion whose scene parent has no ruby depends on that dimension
  (`ruby-will-be-regenerated`), 正道 kept its two-line fragment while the
  audio fill landed (run log 2026-11-17 09:41: Picture + Sentence Audio
  only). The lookup now walks `3918629684.reading` (`collection_io._japanese_support_mecab`),
  and a completion that expected a rebuilt ruby is demoted
  (`stale-fragment-furigana-risk`) if the engine returns nothing.
  Verified by: `test_japanese_support_lookup_finds_the_reading_engine`
  (plus the package-level and missing-addon cases) and
  `test_a_completion_is_demoted_when_the_ruby_engine_returns_nothing`.
- [2026-11-17, OpenCode] Anki's Browser and EditCurrent save on close via
  `editor.call_after_note_saved` (`saveNow()` in webview JS). Writing notes via
  `col.update_note` without `mw.reset()` and without reloading the open editor
  leaves stale field values in the webview DOM; when the browser is subsequently
  closed, the webview flushes those stale values back to SQLite, silently
  reverting the fix (author's ばあちゃん defect). Fixed by
  `notify_collection_changed(browser)` on all apply paths (QUALITY Q-A9).
  Verified by: `tests/test_notify_collection_changed.py`.
- [2026-11-21, OpenCode] Synchronous full-collection backups (`col.create_backup`)
  block Anki's UI thread for 5+ seconds on a 250MB SQLite collection to compress a
  32MB `.colpkg` archive. The add-on eliminates this bottleneck (Q-P1) by relying
  on its deterministic pre-write JSON inverse logs (Q-A6), which write in <0.001s
  before any collection write and provide surgical field rollback without risking
  unrelated review history.
  Verified by: benchmark profiling on the live 17.7k-card collection and
  `test_take_backup_is_noop_and_never_calls_create_backup`.
- [2026-11-21, OpenCode] Indexing all 15,500+ parent cards in
  `SceneParentIndex.from_parents` took 1.35s on 17k cards due to 2.3M 3-gram postings.
  For scoped selections, candidate parents are now bounded to
  `[target.note_id - window_ms, target.note_id + window_ms]`, reducing parent index
  time from 1.35s to <0.001s.
  Verified by: `test_scoped_plan_parent_windowing_matches_scene_without_indexing_entire_deck`
  and profiling.

### Formal Verification (Lean) — Evaluated and Declined

- [2026-11-18, OpenCode] **Decision: no Lean proof layer for this project.**
  A thorough assessment mapped every pure-core module against formal
  verification suitability. Findings:

  **Where Lean would add real value (~365 lines of Python, ~5% of the core):**
  - `applier.py`: apply→undo round-trip (Q-A1, Q-A3), `merge_run_logs` fold
    correctness, per-note atomicity — a transactional state machine, Lean's
    sweet spot.
  - `batch.py`: source-guard atomicity (a guard kills the whole note),
    text-unit atomicity (Sentence + ruby are atomic), `SAFE_OPS ∩
    OPTIONAL_OPS = ∅` — set-theoretic, clean.
  - Tag union monotonicity (Q-D5), fill preconditions (Q-D1/Q-D2) —
    essentially refined types, trivially provable.
  - `SceneParentIndex ≡ linear find_scene_parents` — classic index-
    equivalence proof.

  **Where Lean would be a money pit (~95% of the core):**
  - `plan.py` (2,500 lines): the decision engine is a cascade of string
    heuristics, regex, Unicode NFKC, HTML unescaping, MeCab morphology.
    Formalizing it means modeling Python's regex engine and stdlib in
    Lean — a multi-year academic project, not a product investment.
  - `normalize.py` (450 lines): same problem — core operations are
    `re.sub`, `unicodedata.normalize`, `html.unescape`.
  - The fixpoint/convergence property (Q-A2, the hardest and most
    valuable invariant) depends on the interaction of all tiers through
    those string operations — unreachable by Lean in practice.

  **Cost of the provable subset:**
  - ~1,500–2,500 lines of Lean for ~365 lines of Python.
  - Dual-implementation maintenance: every `applier.py`/`batch.py`
    change requires updating the Lean model in lockstep.
  - Lean 4 + Lake in CI, a new dependency chain, and Lean literacy to
    read broken proofs.

  **Better alternatives for the same assurance, at ~5% of the cost:**
  - Mutation testing (`mutmut`/`cosmic-ray`) on `applier.py` and
    `batch.py` — proves existing tests detect every possible single-
    point mutation. An afternoon, not months.
  - `crosshair` symbolic execution on `applier.decide` — effectively a
    proof for all inputs within the type constraints, using Python's
    actual semantics (no re-specification).
  - Strengthened Hypothesis: 10,000+ examples, explicit edge-case
    strategies, mutation-testing validation that strategies don't avoid
    hard cases.

  The concern that agents can game property tests is valid but already
  mitigated by structural guards (layer boundary test, golden fixture,
  scene audit on real cards, byte-exact undo replay). The real gap —
  sampling vs. exhaustive coverage — is closed more cheaply by mutation
  testing and symbolic execution than by a parallel proof language.

  Verified by: full-module assessment of `normalize.py`, `fieldconfig.py`,
  `matching.py`, `plan.py`, `batch.py`, `applier.py`, `report.py` against
  Lean 4 formalization criteria; cross-referenced with QUALITY.md (29
  invariants) and TEST_STRATEGY.md (4 rings + property tests).

## Lessons Learned

- [2026-11-14, OpenCode] `"<b" in sentence` matches `<br>`, and subtitle
  sentences are full of `<br>` — that silently disabled every bold repair in
  the first build. Use a tag regex (`<b[\s>]|<strong`).
  Verified by: `test_bold_repair_with_br_markup` and an empty bold census in
  the build output before the fix.
- [2026-11-14, OpenCode] Two broken cards were reported by the user; the real
  scope was a moving "last N" window that had dropped the older one. Ad-hoc
  repros must use the same scope as the real run, or they "pass" while
  investigating a card that is no longer in scope.
  Verified by: `--note-id` pinning the card back in and the failing census.
- [2026-11-14, OpenCode] Anki's `AddonManager.loadAddons()` imports each
  add-on folder by name; a folder name that is not a valid identifier
  (e.g. the repo's own hyphenated name) cannot be imported. The add-on
  package is therefore `mining_fixer`, and the repo may be called anything.
  Verified by: `aqt/addons.py` `allAddons`/`loadAddons` and the symlinked dev
  install loading successfully.
- [2026-11-14, OpenCode] Anki 26.09.4 can crash on Browser close with
  `RuntimeError: wrapped C/C++ object of type QWidget has been deleted` in
  `editor_legacy.cleanup → set_note(None) → widget.hide()`. The frames are all
  Anki's own: `Browser.closeWithCallback` is queued through
  `call_after_note_saved`, and if the window is torn down before the queued
  callback fires, cleanup runs against a deleted widget. Add-ons cannot fix
  Anki's teardown, but writing notes from inside a modal dialog while the
  Browser editor has a save in flight puts us in that window — so writes now
  happen after the dialog closes and after the editor flushes
  (`applier.run_after_editor_saved`, QUALITY Q-A5).
  Verified by: the pasted traceback (no Mining Fixer frame) and
  `tests/test_applier.py` timing tests.
- [2026-11-14, OpenCode] The author's machine crashed after this agent ran several
  headless Anki/Qt attempts (`aqt._run(exec=False)` under `QT_QPA_PLATFORM=offscreen`)
  and repeated full-collection copies. The exact cause is unproven, but the
  activity was the heaviest thing on the machine and is now forbidden by
  AGENTS.md §2 rules 2–3. Durable rule: never start Anki/Qt/WebEngine here;
  never copy the collection or scan the deck; use one scoped read-only query
  and CI. Verified by: the crash report and the new rules above.
- [2026-11-14, OpenCode] Qt treats a bare `&` in a menu label as a mnemonic,
  so `preview & apply` renders as `preview apply` with a stray underline.
  Verified by: pinning the literal menu strings in `test_addon_load.py`.
- [2026-11-15, OpenCode] The sentence-repair dimension shipped three rules
  the owner never asked for; they asked for arrows only and the other two
  were rolled back the same day. Do not widen an opt-in dimension beyond
  what the owner described — a census says what is *there*, not what they
  want *changed*.
  Verified by: the rollback commit + the owner-decision notes in
  `BULK_FIX.md`.
- [2026-11-16, OpenCode] Two OpenCode sessions have driven this checkout at
  once: files change mid-turn, `git status` gains entries that are not yours,
  and the other session may commit *and push* your HEAD while you work. A
  path-scoped `git commit -- <paths>` still commits the working-tree content
  of those paths, so a foreign unstaged edit gets swept in (it did, in
  a0d69d8). Re-read `git status`/`git diff` immediately before staging and
  again after; never `git checkout HEAD -- <file>` to isolate your hunks
  without copying the file aside first, or the other session's uncommitted
  work is gone (report.py had to be restored from /tmp in that same
  session).
  Verified by: the a0d69d8 commit's doc hunks (the T15 wave, not mine) and
  the restored `report.py`.
- [2026-11-21, OpenCode] Fixing a single card took ~10-12s due to three
  compounded issues: (1) 5.1s synchronous `col.create_backup`, (2) 1.35s
  un-windowed whole-deck 3-gram parent indexing, and (3) fixpoint pass 1
  re-querying SQLite for all 17,700 records from scratch instead of reusing
  in-memory state. Bounding parent indexing, caching media checks, updating
  touched records in-place, and removing synchronous backups dropped end-to-end
  latency below 0.5s (~20x faster).
  Verified by: cProfile benchmarks against the live 253MB database.
