"""Anki-facing adapter: read the collection, execute an apply decision.

Only this module (and ``preview_dialog.py`` / ``__init__.py``) may touch
Anki or Qt. All decision logic lives in :mod:`mining_fixer.applier`, so
the safety rules stay testable without Anki installed.

Nothing here runs automatically: the dialog calls :func:`plan_preview` to
read, and :func:`apply_decision` only after the user accepted rows.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from aqt import mw

from .applier import (
    ApplyDecision,
    NoteState,
    build_run_log,
    decide,
    decide_undo,
    decision_subset,
    latest_run_log_in,
    merge_run_logs,
    run_log_records,
    verify_writes,
)
from .batch import default_ops, select
from .matching import NoteRecord
from .normalize import MorphToken
from .plan import Plan, build_plan, scope_from_selection

__all__ = [
    "Preview",
    "apply_decision",
    "apply_plan_to_fixpoint",
    "build_decision",
    "deck_records",
    "latest_run_log",
    "load_config",
    "media_exists",
    "notify_collection_changed",
    "plan_preview",
    "read_state",
    "restore_from_log",
    "write_run_log",
]

ADDON_MODULE = "mining_fixer"
UNDO_FOLDER = "mining_fixer"
#: Undo logs written before the add-on was renamed from Mining Backfill.
#: Runs logged by the old build stay undoable; this folder is only ever
#: read — new runs are logged under ``UNDO_FOLDER``.
LEGACY_UNDO_FOLDER = "mining_backfill"
DEFAULT_PREVIEW_LAST_N = 30


def _addon_name() -> str:
    try:
        return mw.addonManager.addon_from_module(__name__) or ADDON_MODULE
    except Exception:
        return ADDON_MODULE


def load_config() -> dict:
    """The add-on config: packaged ``config.json`` defaults merged with edits."""
    try:
        return dict(mw.addonManager.getConfig(_addon_name()) or {})
    except Exception:
        return {}


@dataclass
class Preview:
    """Everything the dialog needs, already computed and read-only."""

    plan: Plan | None = None
    records_by_id: dict[int, NoteRecord] | None = None
    scope: tuple[int, ...] = ()
    scope_label: str = ""
    excluded: tuple[int, ...] = ()
    missing: tuple[int, ...] = ()
    error: str | None = None


def deck_records(col, deck_name: str, note_type_name: str) -> list[NoteRecord] | None:
    """Mining-note records for the configured deck and note type.

    Returns ``None`` when the deck or the note type does not exist, so the
    caller can say so instead of silently doing nothing.
    """
    deck_id = col.decks.id_for_name(deck_name)
    note_type_id = col.models.id_for_name(note_type_name)
    if deck_id is None or note_type_id is None:
        return None
    rows = col.db.execute(
        "select distinct n.id, n.mid, n.flds, n.tags from cards c "
        "join notes n on n.id=c.nid where c.did=? and n.mid=?",
        deck_id,
        note_type_id,
    )
    field_order = [f["name"] for f in col.models.get(note_type_id)["flds"]]
    out: list[NoteRecord] = []
    for note_id, notetype_id, fields, tags in rows:
        values = fields.split("\x1f")
        out.append(
            NoteRecord(
                note_id=note_id,
                notetype_id=notetype_id,
                fields=dict(zip(field_order, values, strict=False)),
                tags=frozenset(tags.split()),
            )
        )
    return out


class MediaExistenceChecker:
    """Fast, cached media existence checker avoiding redundant FFI and Path allocations."""

    def __init__(self, media_dir: str | Path | None) -> None:
        self.media_dir = str(media_dir) if media_dir else None
        self._cache: dict[str, bool] = {}

    def exists(self, filename: str) -> bool:
        if not self.media_dir or not filename:
            return True
        cached = self._cache.get(filename)
        if cached is not None:
            return cached
        try:
            res = os.path.exists(os.path.join(self.media_dir, filename))
        except Exception:
            res = True
        self._cache[filename] = res
        return res


def media_exists(col, filename: str) -> bool:
    try:
        media_dir = col.media.dir() if col is not None and hasattr(col, "media") else None
        return MediaExistenceChecker(media_dir).exists(filename)
    except Exception:
        return True  # never drop a fill because the check itself failed


_media_exists = media_exists


def _record_for_note(note) -> NoteRecord:
    """One live note as a :class:`NoteRecord` (used for selections outside the deck)."""
    return NoteRecord(
        note_id=note.id,
        notetype_id=note.mid,
        fields=dict(note.items()),
        tags=frozenset(note.tags),
    )


def _japanese_support_mecab():
    """The installed Japanese Support controller, or ``None``.

    The add-on package's ``__init__`` imports ``reading`` as a submodule and
    the controller lives *there*: ``reading.py`` ends with
    ``mecab = MecabController()``. The package itself has no ``mecab``
    attribute, so looking only at the package always returned ``None`` — the
    furigana dimension silently reported itself unavailable, and a sentence
    completion that needed a rebuilt ruby shipped unchecked (the 正道 card
    kept its two-line fragment for exactly this reason). Guarded: any failure
    means ``None``, so the caller reports the dimension unavailable rather
    than guessing.
    """
    package = sys.modules.get("3918629684")
    reading = sys.modules.get("3918629684.reading")
    if reading is None and package is not None:
        reading = getattr(package, "reading", None)
    if reading is None:
        try:
            reading = __import__("3918629684.reading", fromlist=["mecab"])
        except Exception:
            reading = None
    for module in (reading, package):
        mecab = getattr(module, "mecab", None) if module is not None else None
        if mecab is not None and hasattr(mecab, "reading"):
            return mecab
    return None


def japanese_support_furigana_generator(col):
    """The reading engine of the installed Japanese Support add-on, or None.

    Bulk-fix dimension 3's bridge: returns ``sentence -> furigana`` backed
    by the add-on's own MeCab/Kakasi subprocesses — the collection's oracle
    for ruby, never a home-grown generator. Lazy and guarded: the add-on
    module only exists inside a running Anki, so headless tooling and the
    test suite get ``None`` and the planner reports the dimension as
    unavailable instead of writing anything. Entry points re-verified
    2026-11-15 against the installed source (`reading.mecab.reading` is
    the string function `regenerateReading` itself uses; the add-on's own
    public entry refuses non-empty destination fields).
    """
    mecab = _japanese_support_mecab()
    if mecab is None:
        return None
    media_strip = col.media.strip

    def generate(sentence: str) -> str | None:
        try:
            out = mecab.reading(media_strip(sentence or ""))
        except Exception:
            return None
        return out or None

    return generate


def _japanese_support_support_dir() -> str | None:
    reading = sys.modules.get("3918629684.reading")
    if reading is not None and hasattr(reading, "supportDir"):
        return reading.supportDir
    package = sys.modules.get("3918629684")
    if package is not None and hasattr(package, "reading"):
        r = package.reading
        if hasattr(r, "supportDir"):
            return r.supportDir
    try:
        if mw is not None and hasattr(mw, "addonManager"):
            cand = os.path.join(mw.addonManager.addonsFolder(), "3918629684", "support")
            if os.path.isdir(cand):
                return cand
    except Exception:
        pass
    for cand in (
        os.path.expanduser("~/.local/share/Anki2/addons21/3918629684/support"),
        os.path.expanduser("~/Library/Application Support/Anki2/addons21/3918629684/support"),
        os.path.expanduser("~/AppData/Roaming/Anki2/addons21/3918629684/support"),
    ):
        if os.path.isdir(cand):
            return cand
    return None


class JapaneseSupportMorphTokenizer:
    """Offline MeCab tokenizer using Japanese Support's bundled binary & dictionary."""

    def __init__(self, support_dir: str) -> None:
        self.support_dir = support_dir
        self.proc: subprocess.Popen | None = None

    def _ensure_proc(self) -> subprocess.Popen | None:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        try:
            exe_name = (
                "mecab.exe"
                if sys.platform == "win32"
                else ("mecab" if sys.platform == "darwin" else "mecab.lin")
            )
            exe_path = os.path.join(self.support_dir, exe_name)
            if not os.path.isfile(exe_path):
                return None
            cmd = [
                exe_path,
                "--node-format=%m\t%f[0]\t%f[6]\t%f[7]\n",
                "--unk-format=%m\tUNK\t%m\t\n",
                "--eos-format=EOS\n",
                "-d",
                self.support_dir,
                "-r",
                os.path.join(self.support_dir, "mecabrc"),
                "-u",
                os.path.join(self.support_dir, "user_dic.dic"),
            ]
            env = dict(os.environ)
            env["LD_LIBRARY_PATH"] = self.support_dir
            env["DYLD_LIBRARY_PATH"] = self.support_dir
            if sys.platform != "win32":
                with contextlib.suppress(Exception):
                    os.chmod(exe_path, 0o755)
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            return self.proc
        except Exception:
            return None

    def tokenize(self, text: str) -> list[MorphToken]:
        proc = self._ensure_proc()
        if proc is None or proc.stdin is None or proc.stdout is None:
            return []
        try:
            clean = text.replace("\n", " ").strip()
            if not clean:
                return []
            proc.stdin.write(clean.encode("utf-8") + b"\n")
            proc.stdin.flush()
            tokens: list[MorphToken] = []
            while True:
                line = proc.stdout.readline().decode("utf-8", "replace").rstrip("\r\n")
                if not line or line == "EOS":
                    break
                parts = line.split("\t")
                if len(parts) >= 4:
                    tokens.append(MorphToken(parts[0], parts[1], parts[2], parts[3]))
                elif len(parts) >= 2:
                    tokens.append(MorphToken(parts[0], parts[1], parts[0], ""))
            return tokens
        except Exception:
            return []


def japanese_support_morph_tokenizer(col) -> Callable[[str], Sequence[MorphToken]] | None:
    """Morphological tokenizer backed by Japanese Support's MeCab, or None."""
    support_dir = _japanese_support_support_dir()
    if support_dir is None:
        return None
    tok = JapaneseSupportMorphTokenizer(support_dir)
    media_strip = col.media.strip if col is not None and hasattr(col, "media") else (lambda s: s)

    def tokenize(text: str) -> Sequence[MorphToken]:
        return tok.tokenize(media_strip(text or ""))

    return tokenize


def plan_preview(
    col, cfg: dict, scope_note_ids=None, records: list[NoteRecord] | None = None
) -> Preview:
    """Build the plan for a Browser selection, or for the newest notes.

    With ``scope_note_ids`` (the notes selected in the Browser) only those
    notes are *targets* — but every note of the configured deck stays
    available as a source, so a selection still finds its scene parent or word
    sibling. Selected notes from another note type are reported, not silently
    dropped, and selected notes outside the configured deck are loaded as
    targets too.
    """
    deck_name = str(cfg.get("deck_name") or "")
    note_type_name = str(cfg.get("note_type_name") or "")
    if records is None:
        loaded = deck_records(col, deck_name, note_type_name)
        if loaded is None:
            return Preview(error=f"Deck “{deck_name}” or note type “{note_type_name}” not found.")
        if not loaded:
            return Preview(
                error=f"No notes in deck “{deck_name}” use note type “{note_type_name}”."
            )
        records = loaded
    else:
        records = list(records)
    note_type_id = col.models.id_for_name(note_type_name)
    by_id = {r.note_id: r for r in records}

    excluded: list[int] = []
    missing: list[int] = []
    if scope_note_ids is None:
        records.sort(key=lambda r: r.note_id, reverse=True)
        last_n = int(cfg.get("preview_last_n") or DEFAULT_PREVIEW_LAST_N)
        scope = [r.note_id for r in records[:last_n]]
        scope_label = f"newest {len(scope)} note(s) in “{deck_name}”"
    else:
        wanted = list(dict.fromkeys(int(nid) for nid in scope_note_ids))
        for note_id in wanted:
            if note_id in by_id:
                continue
            try:
                note = col.get_note(note_id)
            except Exception:
                missing.append(note_id)  # deleted since the Browser rendered
                continue
            if note.mid != note_type_id:
                excluded.append(note_id)
                continue
            record = _record_for_note(note)
            records.append(record)
            by_id[note_id] = record
        scope, _unknown = scope_from_selection(wanted, by_id)
        if not scope:
            return Preview(
                error=(
                    f"None of the {len(wanted)} selected note(s) use note type “{note_type_name}”."
                ),
                excluded=tuple(excluded),
                missing=tuple(missing),
                scope_label=f"selection ({len(wanted)} selected)",
            )
        scope_label = f"selection ({len(scope)} note(s))"
        if missing:
            scope_label += f" · {len(missing)} selected note(s) no longer exist"

    try:
        media_dir = col.media.dir() if col is not None and hasattr(col, "media") else None
    except Exception:
        media_dir = None
    media_checker = MediaExistenceChecker(media_dir)

    plan = build_plan(
        records,
        cfg,
        scope_note_ids=scope,
        media_exists=media_checker.exists,
        furigana_generator=japanese_support_furigana_generator(col),
        morph_tokenizer=japanese_support_morph_tokenizer(col),
    )
    return Preview(
        plan=plan,
        records_by_id=by_id,
        scope=tuple(scope),
        scope_label=scope_label,
        excluded=tuple(excluded),
        missing=tuple(missing),
    )


def read_state(col, note_id: int) -> NoteState:
    note = col.get_note(note_id)
    return NoteState(
        fields=dict(note.items()),
        tags=frozenset(note.tags),
    )


def build_decision(col, plan: Plan, checked, cfg: dict) -> ApplyDecision:
    """Re-assert the checked rows against the live collection."""
    return decide(
        plan,
        checked,
        read_state=lambda note_id: read_state(col, note_id),
        applied_tag=cfg.get("applied_tag") or None,
    )


def build_batch_decision(col, plan: Plan, rows, cfg: dict) -> ApplyDecision:
    """Re-assert a batch selection: one ``(note_id, field)`` key per row."""
    return build_decision(col, plan, [(w.note_id, w.field) for w in rows], cfg)


def apply_decision(col, decision: ApplyDecision) -> int:
    """Perform the writes. Returns the number of notes modified.

    One ``update_note`` per note, so Anki's own undo entry stays meaningful.
    A note that vanished or refused the write is skipped with a printed
    warning instead of aborting the rest of the run — verification then
    reports whatever did not land, and the log still allows an exact undo.
    """
    fields_by_note: dict[int, list] = {}
    for write in decision.field_writes:
        fields_by_note.setdefault(write.note_id, []).append(write)
    tags_by_note = {tw.note_id: tw for tw in decision.tags_writes}
    all_note_ids = list(dict.fromkeys(list(fields_by_note) + list(tags_by_note)))
    modified = 0
    for note_id in all_note_ids:
        try:
            note = col.get_note(note_id)
            for write in fields_by_note.get(note_id, []):
                note[write.field] = write.new_value
            tw = tags_by_note.get(note_id)
            if tw is not None:
                for tag in set(tw.new_tags) - set(tw.old_tags):
                    note.add_tag(tag)
                for tag in set(tw.old_tags) - set(tw.new_tags):
                    note.remove_tag(tag)
            col.update_note(note)
            modified += 1
        except Exception as exc:
            print(f"Mining Fixer: could not write note {note_id}: {exc}")
    return modified


def notify_collection_changed(browser=None) -> None:
    """Notify Anki and any open editors/views that collection data has changed.

    When notes are written to the database via col.update_note:
    1. mw.reset() synthesizes OpChanges and fires gui_hooks.operation_did_execute,
       gui_hooks.state_did_reset, and mw.update_undo_actions. This tells Anki's
       reviewer, deck browser, and any open webviews (Svelte/JS) to reload.
    2. If a Browser is open (or was passed in), its table model and current editor
       are refreshed with the newly written note data from the database.
       Crucially, if the browser editor is not refreshed, closing the browser or
       switching cards causes Anki's editor saveNow() to flush stale editor
       contents back to SQLite, silently overwriting and reverting the applied fix!
    3. If mw.reviewer.card is loaded, card.load() clears the cached _note so opening
       the browser from the reviewer will not load stale note data.
    4. If EditCurrent is open, its editor is refreshed as well.
    """
    if mw is None or getattr(mw, "col", None) is None:
        return

    # 1. Clear cached _note on reviewer card if reviewing
    try:
        reviewer = getattr(mw, "reviewer", None)
        card = getattr(reviewer, "card", None)
        if card is not None and hasattr(card, "load"):
            card.load()
    except Exception as exc:
        print(f"Mining Fixer: reviewer card reload failed: {exc}")

    # 2. Global Anki reset (OpChanges, gui_hooks, undo actions)
    try:
        mw.reset()
    except Exception as exc:
        print(f"Mining Fixer: mw.reset failed: {exc}")

    # 3. Locate open browser if not provided
    if browser is None:
        try:
            from aqt import dialogs

            browser = dialogs.getInstance("Browser")
        except Exception:
            browser = None

    # 4. Refresh open browser editor and table
    if browser is not None and not getattr(browser, "_closeEventHasCleanedUp", True):
        try:
            table = getattr(browser, "table", None)
            editor = getattr(browser, "editor", None)
            card = None
            if table is not None:
                if hasattr(table, "get_single_selected_card"):
                    card = table.get_single_selected_card()
                if card is None and hasattr(table, "get_current_card"):
                    card = table.get_current_card()
            if card is None and editor is not None:
                card = getattr(editor, "card", None)
            if card is not None:
                if hasattr(card, "load"):
                    card.load()
                if editor is not None and hasattr(editor, "set_note"):
                    note = card.note(reload=True) if hasattr(card, "note") else None
                    if note is not None:
                        editor.set_note(note)
            if table is not None and hasattr(table, "redraw_cells"):
                table.redraw_cells()
        except Exception as exc:
            print(f"Mining Fixer: browser refresh failed: {exc}")

    # 5. Refresh open EditCurrent dialog if open
    try:
        from aqt import dialogs

        for name in ("EditCurrent", "NewEditCurrent"):
            ec = dialogs.getInstance(name)
            if ec is not None and getattr(ec, "editor", None) is not None:
                editor = ec.editor
                card = getattr(editor, "card", None) or getattr(
                    getattr(mw, "reviewer", None), "card", None
                )
                if card is not None:
                    if hasattr(card, "load"):
                        card.load()
                    note = card.note(reload=True) if hasattr(card, "note") else None
                    if note is not None and hasattr(editor, "set_note"):
                        editor.set_note(note)
    except Exception as exc:
        print(f"Mining Fixer: EditCurrent refresh failed: {exc}")


def undo_folder() -> Path:
    path = Path(mw.pm.profileFolder()) / UNDO_FOLDER / "undo"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_run_log(decision: ApplyDecision, meta: dict) -> Path:
    """Persist the run's exact inverse next to the profile."""
    log = build_run_log(decision, meta)
    # Microseconds: two runs inside the same second must not overwrite each
    # other's undo log.
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = undo_folder() / f"run-{stamp}.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def latest_run_log() -> tuple[Path, dict] | None:
    """The newest undo log, including runs logged by the pre-rename build."""
    return latest_run_log_in([undo_folder(), legacy_undo_folder()])


def legacy_undo_folder() -> Path:
    """Where the pre-rename build kept its undo logs; read only."""
    return Path(mw.pm.profileFolder()) / LEGACY_UNDO_FOLDER / "undo"


def restore_from_log(col, log: dict) -> ApplyDecision:
    """Decide how to undo a logged run against the live collection."""
    return decide_undo(
        run_log_records(log),
        read_state=lambda note_id: read_state(col, note_id),
    )


# --- batch runs -------------------------------------------------------

#: Notes written per backend pass. Keeps a bulk run responsive and means an
#: interruption leaves a partially applied, fully logged run instead of a
#: single half-finished transaction.
BATCH_CHUNK_NOTES = 100


@dataclass
class BatchResult:
    writes: int = 0
    modified: int = 0
    log_path: Path | None = None
    backup_created: bool | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _take_backup(col) -> tuple[bool | None, str | None]:
    """Anki's own backup, forced, before a bulk run touches anything.

    Deprecated: Full collection backup archives (.colpkg) freeze the host
    on large profiles (5+ seconds on a 250MB collection) and overwrite
    unrelated review history if restored. The add-on's pre-write JSON
    inverse log (Q-A6) provides surgical, instant, and reliable undo.
    """
    return False, None


def preflight_fields(col, decision: ApplyDecision) -> list[str]:
    """Refuse a run whose field names do not exist on the target note type.

    The configured field list can drift from the collection (renamed note
    type, edited config). Writing an unknown field would raise halfway
    through a bulk run, so this is checked up front, before anything is
    touched.
    """
    problems: list[str] = []
    expected: dict[int, set[str]] = {}
    for note_id in decision.touched_notes:
        try:
            note = col.get_note(note_id)
        except Exception:
            continue  # vanished notes are reported by verification instead
        expected[note_id] = set(note.keys())
    for write in decision.field_writes:
        names = expected.get(write.note_id)
        if names is not None and write.field not in names:
            problems.append(f"note {write.note_id}: field “{write.field}” is not on its note type")
    return problems


def apply_batch(
    col, decision: ApplyDecision, meta: dict, *, take_backup: bool = True
) -> BatchResult:
    """Write a bulk decision: preflight, backup, log first, chunk, verify.

    The run log is written *before* the first write, so an interrupted or
    crashed run is still exactly reversible — undo only restores fields that
    still hold the value this run wrote. A failed backup aborts the run: no
    write happens without the safety net the user was promised.

    ``take_backup=False`` skips the backup for a *later* pass of one Apply:
    the fixpoint backs up once, before its first pass, not once per pass (a
    full copy per pass would be four collection copies of one Apply).
    """
    result = BatchResult(writes=decision.row_count())
    if not decision.row_count():
        return result
    field_problems = preflight_fields(col, decision)
    if field_problems:
        result.problems.extend(field_problems)
        result.problems.append("nothing was written")
        return result
    if take_backup:
        created, error = _take_backup(col)
        result.backup_created = created
        if error is not None:
            result.problems.append(f"backup failed, nothing was written: {error}")
            return result
    result.log_path = write_run_log(decision, meta)
    notes = decision.touched_notes
    for start in range(0, len(notes), BATCH_CHUNK_NOTES):
        chunk = notes[start : start + BATCH_CHUNK_NOTES]
        result.modified += apply_decision(col, decision_subset(decision, chunk))
    result.problems.extend(
        verify_writes(
            decision,
            lambda nid: read_state(col, nid),
            media_exists=lambda name: _media_exists(col, name),
        )
    )
    return result


#: One Apply may re-plan this many times to reach a fixpoint (QUALITY Q-A2).
#: A residual after this bound is a defect the scene audit's class F reports.
MAX_APPLY_PASSES = 4


def apply_plan_to_fixpoint(
    col,
    cfg: dict,
    scope,
    meta: dict,
    first_decision: ApplyDecision | None = None,
    *,
    take_backup: bool = True,
    excluded_keys: AbstractSet[tuple[int, str]] = frozenset(),
    initial_records: Mapping[int, NoteRecord] | None = None,
) -> BatchResult:
    """One Apply is the whole fix (QUALITY Q-A2).

    A card completed by this pass becomes a scene source; a neighbour that
    needs its media would otherwise wait for the next Apply. So each pass
    re-plans on the *current* collection (real post-write state, never a
    simulation), selects the default rows (minus any ``excluded_keys`` the user
    explicitly unchecked in the preview dialog), decides and writes — and the
    loop stops when a pass has nothing to write. When ``take_backup`` is true,
    it backs up once before the first real pass. It reports a residual if the
    pass bound is hit, and merges the per-pass undo logs into one so Undo
    reverses the whole Apply.
    """
    combined = BatchResult()
    log_paths: list[Path] = []
    decision = first_decision
    cleared = False
    backed_up = not take_backup
    records_by_id: dict[int, NoteRecord] | None = (
        dict(initial_records) if initial_records is not None else None
    )
    for _pass in range(MAX_APPLY_PASSES):
        if decision is None:
            if records_by_id is not None:
                try:
                    preview = plan_preview(
                        col,
                        cfg,
                        scope_note_ids=list(scope),
                        records=list(records_by_id.values()),
                    )
                except TypeError:
                    preview = plan_preview(col, cfg, scope_note_ids=list(scope))
            else:
                preview = plan_preview(col, cfg, scope_note_ids=list(scope))
            if preview.plan is None or preview.error:
                combined.problems.append(preview.error or "could not rebuild the plan")
                break
            prev_by_id = getattr(preview, "records_by_id", None)
            if records_by_id is None and prev_by_id:
                records_by_id = dict(prev_by_id)
            selection = select(preview.plan, default_ops(cfg))
            rows = (
                [w for w in selection.rows if (w.note_id, w.field) not in excluded_keys]
                if excluded_keys
                else selection.rows
            )
            decision = build_batch_decision(col, preview.plan, rows, cfg)
        if not decision.row_count():
            cleared = True
            break
        result = apply_batch(col, decision, meta, take_backup=not backed_up)
        backed_up = True  # at most one backup per Apply, before its first real pass
        combined.writes += result.writes
        combined.modified += result.modified
        combined.problems.extend(result.problems)
        if result.backup_created:
            combined.backup_created = True
        if result.log_path is not None:
            log_paths.append(result.log_path)
        if result.problems:
            break
        if records_by_id is not None and col is not None and hasattr(col, "get_note"):
            for nid in decision.touched_notes:
                try:
                    note = col.get_note(nid)
                    records_by_id[nid] = _record_for_note(note)
                except Exception:
                    records_by_id.pop(nid, None)
        decision = None  # next pass plans again on the state this pass wrote
    if not cleared and not combined.problems:
        # The pass bound was hit while the last pass still wrote. Re-plan once
        # and say so; a silent residual would break Q-A2 without a trace.
        if records_by_id is not None:
            try:
                preview = plan_preview(
                    col,
                    cfg,
                    scope_note_ids=list(scope),
                    records=list(records_by_id.values()),
                )
            except TypeError:
                preview = plan_preview(col, cfg, scope_note_ids=list(scope))
        else:
            preview = plan_preview(col, cfg, scope_note_ids=list(scope))
        if preview.plan is not None:
            remaining = select(preview.plan, default_ops(cfg))
            residual = (
                [w for w in remaining.rows if (w.note_id, w.field) not in excluded_keys]
                if excluded_keys
                else remaining.rows
            )
            if residual:
                combined.problems.append(
                    f"still {len(residual)} row(s) to write after "
                    f"{MAX_APPLY_PASSES} passes — press Apply again"
                )
    merge_run_logs(log_paths)
    if log_paths:
        combined.log_path = log_paths[-1]
    return combined


def dry_run_folder() -> Path:
    path = Path(mw.pm.profileFolder()) / UNDO_FOLDER / "dry-runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_dry_run(plan: Plan, records_by_id: dict[int, NoteRecord], selection, meta: dict) -> Path:
    """Write the readable preview for a batch the user did not apply."""
    from .report import render_batch_summary

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = dry_run_folder() / f"batch-{stamp}.md"
    path.write_text(render_batch_summary(plan, records_by_id, selection, meta), encoding="utf-8")
    return path
