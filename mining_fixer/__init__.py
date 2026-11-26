"""Mining Fixer — Anki add-on entry point.

Wires two Tools-menu actions: preview & apply, and undo last run.

The pure core (``plan``, ``matching``, ``applier``, …) must stay importable
without Anki — the headless audit builder and the test suite depend on it —
so the ``aqt`` import is optional here and every handler imports Anki's
utilities lazily. Nothing in this module may raise: an exception at import
time is what Anki shows users as "add-on failed to load".
"""

from __future__ import annotations

import contextlib
import json
import traceback
from pathlib import Path

try:
    from aqt import gui_hooks, mw
except ImportError:  # headless import of the package (tests, audit builder)
    gui_hooks = None
    mw = None

PREVIEW_LABEL = "Mining Fixer: preview and apply…"
UNDO_LABEL = "Mining Fixer: undo last run"
SELECTION_LABEL = "Mining Fixer: preview and apply on selection"
BATCH_LABEL = "Mining Fixer: batch-fix selection…"
REVIEWER_LABEL = "Mining Fixer: fix current card"
DEFAULT_SHORTCUT_FIX = "Ctrl+Alt+F"

_menu_added = False
_reviewer_action = None


def _configured_shortcut() -> str:
    """The key combination for batch-fixing cards, or empty string if disabled."""
    try:
        from .collection_io import load_config

        cfg = load_config()
        val = cfg.get("shortcut_fix")
        if val is None:
            return DEFAULT_SHORTCUT_FIX
        return str(val).strip()
    except Exception:
        return DEFAULT_SHORTCUT_FIX


def _apply_shortcut(action, shortcut_str: str) -> None:
    if not shortcut_str or not hasattr(action, "setShortcut"):
        return
    with contextlib.suppress(Exception):
        from aqt.qt import QKeySequence

        action.setShortcut(QKeySequence(shortcut_str))


def addon_version() -> str:
    """``human_version`` from the packaged manifest, for the load banner."""
    try:
        manifest = Path(__file__).with_name("manifest.json")
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("human_version", "?"))
    except Exception:
        return "?"


def _run_preview(scope_note_ids=None, browser=None) -> None:
    from aqt.utils import showWarning

    try:
        from .preview_dialog import open_preview_dialog

        open_preview_dialog(scope_note_ids, browser=browser)
    except Exception:
        print(f"Mining Fixer: preview failed\n{traceback.format_exc()}")
        showWarning("Mining Fixer: preview failed — see the debug console.")


def _on_preview() -> None:
    _run_preview()


def _on_selection_preview(note_ids, browser=None) -> None:
    _run_preview(list(note_ids), browser=browser)


def _on_batch_selection(note_ids, browser=None) -> None:
    from aqt.utils import showWarning

    try:
        from .batch_dialog import open_batch_dialog

        open_batch_dialog(list(note_ids), browser=browser)
    except Exception:
        print(f"Mining Fixer: batch failed\n{traceback.format_exc()}")
        showWarning("Mining Fixer: batch failed — see the debug console.")


def _on_browser_context_menu(browser, menu) -> None:
    """Offer the selection action in the Browser's right-click menu.

    Only when something is selected: the Tools entry already covers whole-deck
    runs, and an action that does nothing is worse than no action. The ids are
    captured now, so a selection change between opening the menu and clicking
    cannot silently retarget the run.
    """
    try:
        selected = list(browser.selected_notes())
    except Exception:
        return
    if not selected:
        return
    try:
        action = menu.addAction(SELECTION_LABEL)
        if action is not None:
            action.triggered.connect(
                lambda _checked=False: _on_selection_preview(selected, browser)
            )
        batch_action = menu.addAction(BATCH_LABEL)
        if batch_action is not None:
            _apply_shortcut(batch_action, _configured_shortcut())
            batch_action.triggered.connect(
                lambda _checked=False: _on_batch_selection(selected, browser)
            )
    except Exception:
        print(f"Mining Fixer: could not add the Browser action\n{traceback.format_exc()}")


def _on_browser_will_show(browser) -> None:
    try:
        sc = _configured_shortcut()
        if not sc:
            return
        from aqt.qt import QAction

        action = QAction("Mining Fixer: batch-fix selection", browser)
        _apply_shortcut(action, sc)
        action.triggered.connect(
            lambda _checked=False: _on_batch_selection(list(browser.selected_notes()), browser)
        )
        browser.addAction(action)
    except Exception:
        pass


def _on_reviewer_context_menu(reviewer, menu) -> None:
    try:
        card = getattr(reviewer, "card", None)
        if card is None or not hasattr(card, "nid"):
            return
        action = menu.addAction(REVIEWER_LABEL)
        if action is not None:
            _apply_shortcut(action, _configured_shortcut())
            action.triggered.connect(lambda _checked=False: _on_batch_selection([card.nid]))
    except Exception:
        print(f"Mining Fixer: could not add Reviewer action\n{traceback.format_exc()}")


def _on_review_shortcut() -> None:
    if mw is not None and getattr(mw, "state", None) == "review":
        reviewer = getattr(mw, "reviewer", None)
        card = getattr(reviewer, "card", None)
        if card is not None and hasattr(card, "nid"):
            _on_batch_selection([card.nid])


def _add_reviewer_shortcut() -> None:
    global _reviewer_action
    if _reviewer_action is not None or mw is None or not hasattr(mw, "addAction"):
        return
    try:
        sc = _configured_shortcut()
        if not sc:
            return
        from aqt.qt import QAction

        action = QAction("Mining Fixer: fix current review card", mw)
        _apply_shortcut(action, sc)
        action.triggered.connect(lambda _checked=False: _on_review_shortcut())
        mw.addAction(action)
        _reviewer_action = action
    except Exception:
        pass


def _on_undo() -> None:
    from aqt.utils import showInfo, showWarning

    try:
        from .collection_io import (
            apply_decision,
            latest_run_log,
            notify_collection_changed,
            restore_from_log,
        )

        if mw is None or mw.col is None:
            showInfo("Open a profile first.")
            return
        found = latest_run_log()
        if found is None:
            showInfo("Mining Fixer: no run to undo.")
            return
        path, log = found
        decision = restore_from_log(mw.col, log)
        if not decision.row_count() and not decision.skipped:
            showInfo("Mining Fixer: that run is already undone.")
            return
        notes = apply_decision(mw.col, decision)
        if decision.row_count() and not notes:
            showWarning("Mining Fixer: undo could not write to the collection — undo log kept.")
            return
        if notes:
            notify_collection_changed()
        lines = [f"Restored {decision.row_count()} value(s) across {notes} note(s)."]
        if decision.skipped:
            lines.append(
                f"{len(decision.skipped)} note(s) were edited since the run and were left alone:"
            )
            lines.extend(
                f"  {skipped.note_id} — {skipped.reason}" for skipped in decision.skipped[:10]
            )
        # Archive the log so the same run cannot be undone twice by accident.
        path.rename(path.with_name(f"undone-{path.name.removeprefix('run-')}"))
        showInfo("\n".join(lines))
    except Exception:
        print(f"Mining Fixer: undo failed\n{traceback.format_exc()}")
        showWarning("Mining Fixer: undo failed — see the debug console.")


def _add_menu_entries() -> None:
    """Add the Tools entries once the main window (and its menu) exists."""
    global _menu_added
    if not _menu_added and mw is not None and hasattr(mw, "form"):
        try:
            menu = mw.form.menuTools
            preview_action = menu.addAction(PREVIEW_LABEL)
            if preview_action is not None:
                preview_action.triggered.connect(lambda _checked=False: _on_preview())
                menu.insertSeparator(preview_action)
            undo_action = menu.addAction(UNDO_LABEL)
            if undo_action is not None:
                undo_action.triggered.connect(lambda _checked=False: _on_undo())
            _menu_added = True
        except Exception:
            print(f"Mining Fixer: could not add the Tools menu entries\n{traceback.format_exc()}")
    _add_reviewer_shortcut()


if mw is not None:
    _add_menu_entries()
    with contextlib.suppress(Exception):  # a banner must never break startup
        print(f"Mining Fixer {addon_version()} loaded.")

# main_window_did_init fires once the menu bar exists; the import-time call
# above covers the common case, and this makes the entry point robust if
# Anki ever loads add-ons earlier in startup.
if gui_hooks is not None:
    gui_hooks.main_window_did_init.append(_add_menu_entries)
    gui_hooks.browser_will_show_context_menu.append(_on_browser_context_menu)
    if hasattr(gui_hooks, "browser_will_show"):
        gui_hooks.browser_will_show.append(_on_browser_will_show)
    if hasattr(gui_hooks, "reviewer_will_show_context_menu"):
        gui_hooks.reviewer_will_show_context_menu.append(_on_reviewer_context_menu)
