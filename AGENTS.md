# Mining Fixer — Agent Operating Rules

> **Authority hierarchy (conflicts resolve top-down):**
> `PRODUCT.md` (what to build) → `MVP.md` (what is in scope now) →
> `ARCHITECTURE.md` (technical structure) → `QUALITY.md` (invariants that
> must hold) → `TEST_STRATEGY.md` (how they are verified) →
> `IMPLEMENTATION_PLAN.md` (current work) → this file (how to operate here).
> **Code implements the specs; tests enforce them.** Never widen scope or
> relax an invariant inside a coding session — change the document first.

Read the documents above that your task touches before you write code, and
keep the change consistent with them.

## 1. What this repo is

An Anki add-on that completes incompletely-mined Japanese cards from their
enriched siblings. Two halves:

- `mining_fixer/` — **the add-on**. Anki discovers any `addons21/<folder>/`
  that contains `__init__.py` and imports that folder as a Python package.
- `scripts/`, `tests/` — development tooling. Nothing here ships.

`mining_fixer/` is packaged for AnkiWeb as a zip whose root contains
`manifest.json` and `__init__.py`; `meta.json` in that folder is a
**dev-install artefact and is gitignored** — never commit it.

## 2. Hard rules

1. **Work through the extension; never edit the collection directly.** The
   live profile is not a workspace: no SQL writes, raw database edits, or
   ad-hoc scripts against it. The normal path is the extension itself
   (preview → Apply, with its undo log). A direct edit requires explicit
   permission for that specific write; without it, stop and ask. For code
   work, copy the collection first (the builder does this via `--profile-dir`
   → `--work-dir`) or point Anki at a scratch base (`ANKI_BASE`). Only the
   add-on may modify a real collection, after Apply, and it must leave an undo
   log.
2. **Never start Anki, Qt or a web engine on the author's machine.** Booting
   Anki — including `QT_QPA_PLATFORM=offscreen` or importing `aqt`/Qt in a
   long-running probe — has crashed the author's session (2026-11-14, after
   several headless Anki/WebEngine attempts). Verify GUI code by import checks
   in the suite and by asking the author to restart Anki; never by launching
   it. `python3 -m pip install` and `ruff`/`pytest` are fine; a GUI process is
   not.
3. **No heavy jobs on the author's machine.** No full-collection copies, no
   whole-deck scans, no background or forked workers, no `--watch`, no growing
   `/tmp` artifacts. Work from one existing snapshot when there is one,
   otherwise a single scoped read-only query (a few thousand notes at most),
   and delete what you create. The fast gates are `ruff check .`,
   `ruff format --check .` and the pytest suite (seconds). Anything bigger is
   a CI job.
4. **The pure core stays pure.** `normalize.py`, `fieldconfig.py`,
   `matching.py`, `plan.py`, `report.py`, `batch.py`, `applier.py` must not
   import `anki`, `aqt` or PyQt — `tests/test_layer_boundary.py` fails if they
   do, and the headless builder plus most of the suite depend on it. Only
   `__init__.py`, `collection_io.py`, `preview_dialog.py` and
   `batch_dialog.py` may touch Anki.
5. **Never guess.** Ambiguity is reported (`plan.reasons`, `plan.warnings`),
   never resolved by picking one of several plausible values.
6. **Fills only; eight approved non-empty writes** (`Source`, `Sentence`,
   `bold_cloze_word`, `repair_sentence`, `regenerate_furigana`,
   `repair_expression`, `strip_empty_glossary`, `add_tags`)
   — each must preserve
   its information, and each carries badges. See `QUALITY.md`.
7. **Tags are never removed** and bookkeeping tags are never added: the audit
   deck is the container, the note carries only the target's and its sources'
   tags.
8. **Fully offline.** No network calls, no telemetry, no auto-update behaviour
   beyond Anki's own.
9. **No secrets, ever** — not in code, config, fixtures or logs. Fixtures are
   real captures from the author's collection; keep them minimal.
10. **Import-time safety.** Anything in `__init__.py` that raises is what users
    see as "add-on failed to load". Guard it, print instead of throwing.
11. **One Apply is the whole fix; a second Apply writes nothing** (owner
    requirement, QUALITY Q-A2). The **Apply** is a fixpoint, not a first
    pass: after each pass `collection_io.apply_plan_to_fixpoint` re-plans on
    the *real* post-write state and stops when a pass writes nothing, merging
    the per-pass undo logs into one (`applier.merge_run_logs`). Two
    consequences:
    - no cross-run cascade — if fixing card A makes card B fixable, B is
      fixed in the **same** Apply;
    - no step ordered after one it depends on (arrow/line cleanup before
      bold, a sentence before its ruby, a completion's audio and its text).
    The gate is `scripts/scene_audit.py` class **F**: apply the selection to a
    fixpoint, re-plan it, and the residual must be **0 rows**. Never ship a
    change that leaves a second run with work — that is a defect, not a
    limitation.
12. **Host UI synchronization after writes (QUALITY Q-A9).** Any code
    modifying the collection (`apply_decision`, `apply_plan_to_fixpoint`,
    undo) must call `notify_collection_changed(browser=...)`. An open Browser
    or `EditCurrent` holds note fields in webview DOM memory; Anki's
    `closeEvent` flushes that editor on close (`call_after_note_saved` →
    `saveNow()`). Without explicit post-write synchronization (`mw.reset()`,
    `card.load()`, `editor.set_note(card.note(reload=True))`), closing or
    switching away from the browser flushes stale pre-fix fields back to SQLite,
    silently reverting the fix (the ばあちゃん defect).

## 3. Environment (verified on the author's machine)

- Arch Linux; Anki 26.09.4 (`/usr/bin/anki`, PyQt6 6.12); system `python3` is
  what has the `anki` package, so headless tooling runs with plain `python3`.
- `ruff` is in `/usr/bin`; pytest is **not** in system Python (Arch splits
  it) — CI installs it, or use a venv.
- Repo layout: pure core + add-on shell in `mining_fixer/`, tests in
  `tests/` (a `conftest.py` puts the repo root on `sys.path`).

## 4. Gates before you present work

```bash
ruff check . && ruff format --check . && pyright   # must be clean
python3 -m pytest                                  # 302 tests with Anki installed
```

Then push and watch CI — it is the gate, and it runs without Anki (300 passed,
2 skipped):

```bash
gh run watch --exit-status
```

The suite is hermetic and sub-second, so local runs are cheap and allowed.
Anything that needs a **real collection** is not a suite test: run it as an
ad-hoc script against a *copy* (see `TEST_STRATEGY.md`, Ring 3).

## 5. Commits and attribution

- Conventional Commits (`feat:`, `fix:`, `refactor:`, `chore:`, `docs:`),
  imperative, one coherent change per commit.
- Never `--amend`, `rebase -i` or `--force`; never claim human authorship.
- Trailers on every commit: `Harness: OpenCode` and `Model: <actual model>`.
- Stage touched files by name; check `git status` for unrelated dirty files
  first (dev-install artefacts must never be staged).

## 6. Docs change with behaviour

A behaviour change updates the document that owns it in the same commit:
a new invariant → `QUALITY.md`; a new check → `TEST_STRATEGY.md` and the
invariant row it verifies; a shipped slice → `IMPLEMENTATION_PLAN.md`;
user-facing usage → `README.md`. Numbers quoted in docs (test counts, config
defaults, field names) are facts — re-verify or drop them.

## 7. Project Notes & History

Detailed historical census findings, add-on compatibility notes, and lessons learned are archived in [`PROJECT_NOTES.md`](PROJECT_NOTES.md). Read it when researching specific Anki APIs or note-type template behaviors.

Core durable facts:
- **Time axis:** Note IDs are millisecond epoch timestamps used for scene matching.
- **Note type:** Mining note type has 20 fields; the configured deck holds ~17k notes.
- **Anki Browser selection:** `browser.selected_notes()` (snake_case).
- **Add-on package:** `mining_fixer` (Python package name must be an identifier).
- **Templates & Listening:** Mining note type has one template (`Card 1`); audio-only front is template behavior when tags or fields match listening predicates.
- **Ruby generation:** Japanese Support add-on engine accessed via `collection_io._japanese_support_mecab`.

