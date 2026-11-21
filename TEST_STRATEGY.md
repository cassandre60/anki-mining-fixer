# Mining Fixer — Test Strategy

Answers: *how do we mechanically prove `QUALITY.md` stays true?*

```text
QUALITY.md (WHAT must hold)
    ↓
TEST_STRATEGY.md (HOW it is verified)
    ↓
tests/ · linters · ad-hoc real-collection runs · CI
```

## The rings (cheap → expensive)

Every behaviour is verified in exactly one ring — the cheapest ring that can
observe it. A new check goes in the innermost ring that can see the behaviour
and moves outward only when it needs more of the real world.

| Ring | What | Where it runs | Cost |
|---|---|---|---|
| **Ring 0 — pure units** (`tests/test_normalize.py`, `test_fieldconfig.py`, `test_matching.py`, `test_plan.py`, `test_applier.py`, `test_batch.py`, `test_report_summary.py`, `test_invariants.py`, `test_layer_boundary.py`) | Every pure function against plain values: normalization keys, field semantics, parent ranking, plan guards and badges, the apply/undo decision matrix, the batch policy (tiers, group and note atomicity, review shortlist), the report and glance rendering (human labels, capped lists), **property-based invariants** over generated plans (every write's precondition holds, apply→undo restores exactly, re-running is a no-op, an edited field aborts its note), and the AST-enforced layer boundary. No Anki, no Qt, no collection, no network. | Local + CI | ms–2 s |
| **Ring 1 — golden acceptance** (`tests/test_golden_scene.py`) | The pipeline must reproduce the author's real manual fix (診察, from scene 至急) **byte-for-byte on all five fields** plus the tag union, from a frozen fixture (`tests/fixtures/golden_scene.json`). This is the anchor: if it moves, behaviour moved. | Local + CI | ms |
| **Ring 2 — add-on load contract** (`tests/test_addon_load.py`) | Imports the package *the way Anki does* (folder name → package, stubbed `aqt`), asserting the two Tools entries are added once, the banner reports the packaged version, a missing main window does not raise, and the Browser context-menu action appears **only** when notes are selected (ids captured when the menu opened). Catches "add-on failed to load" and menu-wiring regressions. | Local + CI | ms |
| **Ring 3 — real-collection verification** (ad-hoc scripts, not committed as tests) | Apply + undo against a **copy** of the real collection (every checked write lands verbatim, guarded-only notes untouched, undo restores exactly); the audit builder's `--last`/`--note-id` scope and package round-trip; the entry point against the real `aqt` package. A live-GUI pass is manual: apply from the Browser selection, then close the Browser and quit Anki, watching the debug console — Anki's legacy-editor teardown has its own known crash (see `AGENTS.md`), and no headless harness here reproduces the Browser's save/close path reliably. | Author's machine, on demand | seconds–minutes |
| **Ring 4 — CI gate** (`.github/workflows/tests.yml`) | `ruff check` + `ruff format --check` + `pyright` + the whole suite on every push and PR, with **no Anki installed**. Green exit code is the gate. | GitHub Actions | ~15 s |

Ring 3 exists because Rings 0–2 cannot see Anki's own API behaviour. It is
deliberately not a committed test: it needs a real collection, which CI does
not have and must never be given.

## Coverage contract (no duplication)

- Every pure function is pinned in exactly ONE ring.
- The **apply and undo decision matrix** lives in `tests/test_applier.py`
  against a dict-backed fake `NoteState` reader — including the cases that
  matter: one stale row skipping a whole note, re-run detection, tags-only-add
  with a tag gained since preview, `applied_tag` on written notes, undo of
  edited notes, idempotent undo, and the undo-log lookup (newest log across
  the current and the pre-rename `mining_backfill` folder, none when the
  newest log is unreadable).
- **The repair pipeline** (bulk-fix dimension 2) is pinned in
  `tests/test_normalize.py` (real deck values, tail arrows stripped while
  line breaks, spacing and bold markup stay untouched, idempotence,
  mid-line arrows kept; the stray horizontal bars (U+2015) the Mining
  cards' Sentences carry are stripped by the owner's 2026-11-20
  `strip-dash` rule — `test_repair_strips_stray_horizontal_bars`;
  `flatten-div-blocks` turns the real 淀む value's 39
  `<div>` wrappers into the four `<br>` lines with the bold intact, keeps
  inline parts and spacing, drops whitespace between block tags),
  `tests/test_plan.py` (checked write
  with rule badges, off without rules, exclusivity with expansion/bold,
  tested-word occurrence guard, unknown-rule warning; the strip is
  Sentence-field-only — the ruby ("Sentence (furigana)") and the
  Definition are untouched — and shipped on by default in
  `config.json` —
  `test_a_stray_dash_is_stripped_from_the_sentence_field_only`,
  `test_strip_dash_is_on_by_default_in_the_shipped_config`) and
  `tests/test_batch.py` (the on-by-default `batch_repair_sentence` tier
  with its opt-out key). The audit's class **E** (no block markup in a
  sentence field) is the whole-selection gate.
- **Bold repair** (template contract) is pinned in `tests/test_plan.py`:
  the three tiers (cloze-body, Expression verbatim, reading-derived
  surface form), exactly-one occurrence, markup-only verification,
  ambiguity reported, the word-like gate (a clause-like Expression such
  as 漢字は読めるようになりたいかな is refused with
  `expression-not-word-like`; an over-long word is skipped too), and the
  listening predicate (tag + audio; no-gloss/no-frequency/audio shape;
  Frequency-without-glosses still repaired). The shipped-class/left-alone
  split comes from the 2026-11-16 census (BULK_FIX slice 8). The word is
  located under width folding, so a subtitle dump's half-width katakana
  (`ｽﾍﾟｸﾄﾗﾑ`) still matches the full-width word (`スペクトラム`) —
  `test_bold_moves_onto_a_half_width_katakana_spelling`. Punctuation-wrapped
  bold tags (such as `<b>とんでもない></b>` on repeated words or `<b>芸者&gt;</b>`)
  are trimmed so the outer delimiters move outside `<b>...</b>` without altering
  renderable characters (`strip_renderable` equality) —
  `test_bold_repair_trims_punctuation_on_repeated_word`. Inflected forms
  (淀む → `<b>よどん</b>だ`, 読む → `<b>読め</b>`) and alternate kanji
  (日差し → `<b>日射し</b>`) are recognized and placed via `morph_tokenizer`
  without guessing — `test_bold_repair_recognizes_inflected_span_with_morph_tokenizer`
  and `test_bold_repair_places_inflected_bold_with_morph_tokenizer`.
- **The expanded rule set and convergence** are pinned in
  `tests/test_normalize.py` (a speaker tag's line break joins; a long
  parenthetical keeps its break) and `tests/test_plan.py` (bold via the
  furigana's per-kanji spellings — 類い希 → 類いまれ; an already-completed
  sentence is never expanded again, so a second run stays a no-op).
- **Scene completion from missing audio** (QUALITY Q-D16) is pinned in
  `tests/test_plan.py`: a word-mined fragment (cloze-* holds only the word)
  is completed to the parent's whole block with the word bolded
  (`test_a_jidoujisho_clip_is_scene_audio_and_completes_the_card`), a scene
  parent whose `Sentence Audio` is a dictionary pack's word recording
  (`yomitan_audio…`) is still refused
  (`test_a_dictionary_word_recording_is_not_scene_audio`), a stale fragment
  ruby is regenerated in the same plan when the engine is present
  (`test_a_stale_fragment_ruby_is_regenerated_when_the_engine_is_available`)
  and demoted otherwise (`test_stale_furigana_blocks_default_checked`), and
  the completion stops once the card owns its audio
  (`test_a_completed_sentence_is_never_expanded_again`). The 正道 replay
  (the real two-line fragment against 出来's nine-line scene) is kept in
  the suite's `_ZHENGDAO_CHILD` values.
- **The empty-definition cleanup** is pinned in `tests/test_normalize.py`
  (the Yomitan shell, variants, and every refusal: real text, media, blank)
  and `tests/test_plan.py` (a checked write per shell field, never on real
  content).
- **Real-card regression suite** (`tests/test_real_cards.py`, owner
  requirement 2026-11-16: "we need a robust regression testing suite").
  Frozen field values captured read-only from the author's collection
  (`tests/fixtures/real_cards.json.gz`; 19 distinct cards across nine
  labelled classes: arrows-in-ruby, divergent fragments, empty definition
  shells, enriched parents, clause expressions, stray angle brackets,
  mis-mined sentence recovery, two-bolds/stale ruby, and a Latin-bearing
  scene parent). It asserts the properties the author
  actually cares about — a second run is a no-op, no subtitle arrow
  survives in either sentence field, a definition never keeps a junk shell,
  a clause is never bolded, junk data is reported not guessed. It exists
  because synthetic fixtures passed while real cards stayed broken; it
  caught the ruby-arrow and copied-ruby bugs the day it was written. The
  newest pair is the author's 引き受ける card (S03E10): its parent scene
  carries `ＯＫ`, and the test asserts one run fills the scene audio and
  picture, expands the sentence with the bold on the card's word, and
  regenerates the ruby.
- **One-run convergence** (QUALITY Q-A2; owner requirement 2026-11-17: "it
  should fix everything in 1 run"). The mechanism is
  `collection_io.apply_plan_to_fixpoint`: each pass plans on the *real*
  post-write state, selects, decides and writes, and the loop stops when a
  pass writes nothing; the per-pass undo logs are merged into one by
  `applier.merge_run_logs` (pinned in `tests/test_applier.py`). The gate is
  `scripts/scene_audit.py` class **F**, which mirrors that loop: apply the
  selection to a fixpoint, re-plan it, and **fail if any row remains**. The
  same property is pinned per card in `tests/test_real_cards.py` (second run =
  no-op) and in the property tests (re-running a plan is a no-op); the audit
  extends it from a fixed fixture to a whole selection, because a cross-run
  cascade only appears *across* cards. Measured 0 rows on the newest 300
  (2026-11-17). Never weaken it — but do not trust it alone: Q-A10 showed a
  card can be half-fixed while class F honestly reports zero rows, because
  re-planning asks the planner to referee itself. Class **G** below is the
  independent state check.
- **A skipped expansion stays reachable** (QUALITY Q-A10, owner report
  2026-11-21, 飲食 note 1795027901231): `tests/test_plan.py` asserts that a
  parent bolding a different word (「`<b>無銭</b>飲食で…`」) still produces a
  checked expansion with the bold retargeted to the card's word, and that a
  card whose audio was filled from a sibling but whose sentence stayed a
  fragment is re-expanded on the next run — followed by a fixpoint check (the
  run after that writes nothing). All three cases fail on the pre-fix planner.
- **No silent abandonment under partial apply** (QUALITY Q-A11):
  `tests/test_plan.py::test_checked_only_apply_keeps_guarded_work_reachable`
  is a metamorphic check — apply only the plan's checked writes to a card,
  re-plan, and require every previously guarded field to still be proposed.
  It fails on the pre-fix planner with `guarded Sentence vanished`. The
  independent referee `scripts/scene_audit.py::_fragment_under_shared_audio`
  (class **G**) reads post-run field values without importing `build_plan`
  and flags any card whose audio is byte-identical to a sibling's while its
  sentence is a strict fragment of that sibling's block; `tests/test_scene_audit_referee.py`
  pins it (stuck card flagged, fixed card and unrelated audio quiet). The
  adversarial shapes live once in `tests/test_golden_scene.py`
  (`foreign_bold_sibling`, `foreign_bold_child(post_partial_apply=True)`).
- **Word-anchored scene recovery** (QUALITY Q-D14) is pinned in
  `tests/test_plan.py`: a mis-mined sentence is recovered from the word's
  own spellings, the tighter of two nested same-episode blocks wins, the
  paired ruby comes along, and a blob source or two different scenes are
  refused. The captured 類い希 card (with its two sibling blocks) lives in
  the real-card fixture, so the replay covers it too.
- **Furigana regeneration** (bulk-fix dimension 3) is pinned in
  `tests/test_normalize.py` (`furigana_base_matches`: whitespace and the
  engine's spacing quirks, speaker brackets, mangled/unbalanced output
  rejected), `tests/test_plan.py` (checked write with badges, on by
  default, noop when the ruby already matches, generator unreachable →
  one warning per run, generated from the **checked** (actually-applied)
  post-repair sentence — never a guarded expansion,
  `test_furigana_generation_uses_the_sentence_that_will_be_written` — defers to
  a queued scene fill of the field, dropped on a mangled or raising
  engine) and `tests/test_batch.py` (the on-by-default
  `batch_regenerate_furigana` tier with its opt-out key). The engine
  itself is never faked into the suite's core paths: it
  is an injected callable, so a broken MeCab degrades to a skip, not a
  wrong write — **never run it inside Anki from the suite**.
- **Ruby/sentence coherence** (Q-D15) is pinned in `tests/test_plan.py`
  (`test_a_foreign_ruby_is_never_copied_onto_a_different_sentence`: a parent
  ruby that does not describe the child's sentence is never written checked;
  `test_scene_furigana_filled_when_parent_has_it`: a *matching* scene ruby is
  still filled byte-identically) and measured over a whole selection by
  `scripts/scene_audit.py` (class C — the ruby does not match its sentence;
  0 with the reading engine; without it only the pre-existing stale rubies
  that need the in-Anki engine remain).
- **Expression repair** (bulk-fix dimension 4, self-oracled variant, Q-D12)
  is pinned in `tests/test_normalize.py` (`furigana_base` extracts the
  dictionary form from inline ruby, unusable markup yields nothing,
  including HTML `<ruby>`/`<rt>` whose `<rt>` text would corrupt the base),
  `tests/test_plan.py` (the ひと握り → 一握り and とっておき → 取って置き
  completions from the note's own furigana, switchable off via the escape
  hatch, inert when the furigana base already matches the Expression,
  silent without furigana, reading mismatch reported, kanji-less and
  clause-like bases refused, and every non-completion shape refused and
  reported — inflection (決まってる → 決まる), kanji → kana preference
  (気付き → 気づき), different-kanji substitution (美味い → 上手い),
  HTML-ruby furigana ignored — deferred while word-level writes are
  queued), and `tests/test_batch.py` (the on-by-default
  `batch_repair_expression` tier with its opt-out key).
- **Write timing** is pinned in the same file with fake editors: writes wait
  for `call_after_note_saved`, a doubled editor callback writes once, a lost
  callback is covered by the scheduler safety net, and a raising or missing
  editor still lets the run through.
- **Batch policy** lives in `tests/test_batch.py`: the default tier is
  lossless, the text unit (sentence + ruby) is atomic, a text-safety guard
  drops only the sentence while the scene's audio/picture are still filled, a
  source-confidence guard leaves the whole note alone, complete notes count as
  untouched, and tag-only notes are reported separately.
- **The batch window's glimpse** lives in `tests/test_report_summary.py`
  (`report.change_lines`): one line per changed note with human labels, a
  short old → new sample only for the sentence rewrites (ruby and
  markup-only bold stay label-only — their values are noise in one line), the
  40-row cap pointing at the full report, and the note-id fallback when the
  expression is unknown. The in-app report viewer, the breakdown line and the
  list widgets are presentation; they are covered by the dialog import check
  (Ring 2), and anything decision-shaped stays in `applier.py`/`plan.py`.
- **Source confidence** is pinned in `tests/test_plan.py`: disagreeing parents
  are never auto-checked, a far containment match ships unchecked, and a
  byte-identical sentence stays trusted at any distance.
- **Matching-key whitespace** is pinned in `tests/test_normalize.py` and
  `tests/test_matching.py`: a space beside CJK is typographic and dropped
  even on a line that contains Latin (`ＯＫ 少年` → `OK少年`), while a space
  between two Latin/digit tokens is lexical and kept (`a b` ≠ `ab`). The
  author's 引き受ける card (S03E10) is the containment oracle; the captured
  pair lives in the real-card fixture and is replayed by
  `test_a_cjk_fragment_completes_from_a_latin_bearing_scene_parent`.
- **Engine invariants** live in `tests/test_invariants.py` and are generated
  with Hypothesis: every write's precondition holds at decision time,
  applying then undoing restores the exact prior state, re-running a plan
  after apply writes nothing, and editing one field aborts that note.
  `tests/test_matching.py` and `tests/test_normalize.py` likewise use
  Hypothesis to prove `SceneParentIndex` matches brute-force
  `find_scene_parents` identically and `kana_fold`'s translation table matches
  the Katakana codepoint specification.
- **Scale proof** (Ring 3): 1000 selected notes on a copy of the real
  collection — plan 2.2 s, apply 1.5 s with a backup, verification clean, the
  793 untouched notes byte-identical, re-run a no-op, undo restoring all 1000
  exactly.
- **Acceptance runs use a curated battery, not bulk 1000-note runs** (owner
  guidance, 2026-11-16). Future Ring 3 runs pick a small set where each card
  pins one defect class — a failure then names the class it broke instead of
  hiding in aggregate counts. The 1000-note run above stands as the one-off
  scale proof; it does not need repeating per dimension.

  | # | Class | Card property | Must prove |
  |---|---|---|---|
  | 1 | tail arrow only | `Sentence` ends `➡` | arrow stripped, every other character byte-identical |
  | 2 | mid-line arrow | `f:X→Y` inside a line | no write at all (pure noop) |
  | 3 | arrow around bold | `…<b>殺し</b>➡` | arrow gone, `<b>` intact |
  | 4 | anchor risk | the tested word's occurrence count would change | skipped with a warning, no write |
  | 5 | expansion queued | scene gap + tail arrow | exactly one `Sentence` row (the expansion), no repair row |
  | 6 | bold repair queued | no `<b>` in `Sentence` + tail arrow | the bold row owns the field, no repair row |
  | 7 | stale furigana | sentence fine, ruby wrong | regenerated row with an old → new diff |
  | 8 | correct furigana | already annotated as the engine would | no furigana row |
  | 9 | child of a source | child with empty ruby under a scene fill | scene fill only, no regen row |
  | 10 | blob Source | asbplayer URL | timestamped replace, URL recorded in the run log |
  | 11 | divergent parents | two parents, different payloads | the suspect scene rows dropped and reported; the card's own repairs (bold, arrows, furigana) still apply |
  | 12 | nothing to fix | fully enriched note | untouched; byte-identical after apply + undo |

  Twelve cards, one per class; a new guard adds one card, not a bigger run.

  The runner is `scripts/acceptance_battery.py`: give it a copy of the
  profile (`--profile-dir` → `--work-dir`, the builder's pattern) and a
  battery JSON mapping each class to note ids; it prints PASS/FAIL per
  class from the plan alone and writes nothing. Every dimension runs by
  default (owner decision 2026-11-16), so `--arrows` / `--furigana` only
  force a dimension back on after a config switched it off. The two
  furigana classes need Japanese Support's engine, which exists only
  inside a running Anki — headless they cannot pass.
- The **UI** (`preview_dialog.py`) is not unit-tested; its import is checked
  in Ring 2 and its behaviour is exercised by Ring 3's apply run. Anything
  decision-shaped in it is delegated to `applier.py`/`plan.py` precisely so it
  can be tested there.
- `collection_io.py` is a thin adapter and is covered by Ring 3; the parts
  worth pinning (`_media_exists` behaviour, `deck_records` returning `None`
  for a missing deck) are deliberately fail-visible in its docstrings rather
  than mocked.

## Self-skipping, not faking

`test_preview_dialog_imports_with_real_anki` needs the real Anki/PyQt6 stack.
Where that is absent (CI) it **skips** via `pytest.importorskip`; it never
falls back to a stub, because a stub would not catch a broken Qt name. So the
expected counts are:

| Environment | Result |
|---|---|
| Author's machine (`python3 -m pytest`, Anki present) | **312 passed** |
| CI / a venv without Anki | **310 passed, 2 skipped** |

Both are green; the difference is exactly two self-skipping import checks
(the dialog modules need a real `aqt`).

## Performance Ratchet (Latency Budget)

- **The 1-Second Wall (Q-P1):** Single-card fixes must execute under 1 second
  interactive latency. Regressions that re-introduce synchronous `.colpkg`
  compression (`_take_backup`), full-deck 3-gram indexing on bounded selections,
  or whole-collection SQLite re-queries during fixpoint passes are refused.
- **Pinned invariants:**
  - `tests/test_apply_fixpoint.py::test_take_backup_is_noop_and_never_calls_create_backup`
    asserts `col.create_backup` is never invoked on the fix path.
  - `tests/test_apply_fixpoint.py::test_fixpoint_reuses_in_memory_records_on_subsequent_passes`
    asserts fixpoint passes reuse in-memory records and update only touched notes.
  - `tests/test_plan.py::test_scoped_plan_parent_windowing_matches_scene_without_indexing_entire_deck`
    asserts candidate parent indexing is bounded by `window_ms`.

## Fail-to-pass for bug fixes

A test added for a bug must be able to fail on the unfixed code. Three real
examples from this repo's history:

- `<b` matched `<br`, silently disabling every bold repair →
  `test_bold_repair_with_br_markup` pins the `<br>` case.
- An already-applied row was misreported as stale (re-running a plan looked
  like a conflict) → `test_rerunning_the_same_plan_is_a_noop`.
- Closing Anki's Browser reverted newly applied writes because the browser
  editor flushed stale pre-fix fields on close (`saveNow()`) →
  `tests/test_notify_collection_changed.py` pins Q-A9 (`mw.reset()`,
  `card.load()`, `editor.set_note(card.note(reload=True))` and one
  `update_note` call per note).

When the bug is only observable with real Anki/anime data, the observation
that made it visible is recorded in the test or in this file rather than
faked with a stub that cannot see it.

## Running things

```bash
ruff check . && ruff format --check . && pyright   # Ring 4 locally, fast
python3 -m pytest                                  # Rings 0–2
gh run watch --exit-status                         # Ring 4, the gate
```

The suite is hermetic and sub-second, so local runs are cheap and allowed —
unlike a heavyweight workspace suite, it cannot pin the machine. Real-collection
work (Ring 3) must always copy the collection first:

```bash
cp "$HOME/.local/share/Anki2/User 1/collection.anki2"* /tmp/opencode/probe/
ln -s "$HOME/.local/share/Anki2/User 1/collection.media" /tmp/opencode/probe/collection.media
```

## Static guards

- `test_layer_boundary.py` — no `anki`/`aqt`/PyQt import outside the four
  Qt/Anki-facing modules, relative intra-package imports only (flat module names
  would collide across add-ons in Anki's shared `sys.path`).
- `ruff` — lint (`E,F,I,UP,B,SIM`) plus formatting, line length 100.
- `pyright` — static type check across `mining_fixer/`, `scripts/` and `tests/`
  (catches Qt/Anki signature mismatches without booting a GUI process).
- `test_addon_load.py` also asserts the menu labels contain no Qt mnemonic
  `&`, a rendering hazard that no runtime test would catch.
