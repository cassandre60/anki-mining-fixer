"""The add-on must import exactly the way Anki imports it.

Anki imports the add-on folder as a package (``__import__(dir_name)``) and
puts a startup error in front of the user if that raises. These tests
reproduce that contract with a minimal ``aqt`` stub, which is also the only
way to exercise the entry point without a running Anki.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest

MENU_LABELS: list[str] = []


def _action(menu, label: str) -> _Action:
    return next(action for action in menu.actions if action.label == label)


class _Action:
    """Minimal QAction stand-in that records its label and connected slot."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.callback = None
        self.triggered = types.SimpleNamespace(connect=self._connect)

    def _connect(self, fn) -> None:
        self.callback = fn

    def fire(self) -> None:
        assert self.callback is not None
        self.callback(False)


class _Menu:
    def __init__(self) -> None:
        self.actions: list[_Action] = []

    def addAction(self, label):
        MENU_LABELS.append(label)
        action = _Action(label)
        self.actions.append(action)
        return action

    def insertSeparator(self, _action):
        return None


def _purge(prefix: str) -> None:
    for name in [n for n in sys.modules if n == prefix or n.startswith(f"{prefix}.")]:
        del sys.modules[name]


@pytest.fixture
def loaded_addon(monkeypatch):
    """Install a stub ``aqt``, then import the add-on as Anki would."""
    MENU_LABELS.clear()
    _purge("aqt")
    _purge("mining_fixer")

    aqt: Any = types.ModuleType("aqt")
    aqt.mw = types.SimpleNamespace(form=types.SimpleNamespace(menuTools=_Menu()))
    aqt.gui_hooks = types.SimpleNamespace(
        main_window_did_init=[], browser_will_show_context_menu=[]
    )
    utils: Any = types.ModuleType("aqt.utils")
    utils.showInfo = lambda *a, **k: None
    utils.showWarning = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    monkeypatch.setitem(sys.modules, "aqt.utils", utils)

    module = importlib.import_module("mining_fixer")
    yield module
    _purge("mining_fixer")


def test_entry_point_adds_the_tools_menu_entries(loaded_addon):
    # Pinned literally: these are the strings the user sees in the menu.
    assert MENU_LABELS == [
        "Mining Fixer: preview and apply…",
        "Mining Fixer: undo last run",
    ]


def test_menu_labels_have_no_qt_mnemonic_ampersand(loaded_addon):
    # Qt turns a bare "&" into an accelerator underline (and hides it), so a
    # label like "preview & apply" would render as "preview apply" with a
    # stray underline on the 'a'.
    assert all("&" not in label for label in MENU_LABELS)


def test_menu_registration_is_idempotent(loaded_addon):
    # The same callable is wired to main_window_did_init; if the menu were
    # added twice the user would see duplicate entries.
    hook = sys.modules["aqt"].gui_hooks.main_window_did_init
    assert len(hook) == 1
    hook[0]()
    hook[0]()
    assert MENU_LABELS == [loaded_addon.PREVIEW_LABEL, loaded_addon.UNDO_LABEL]


def test_entry_point_reports_the_packaged_version(loaded_addon):
    assert loaded_addon.addon_version() == "0.3.1"


def test_entry_point_survives_a_missing_main_window(monkeypatch):
    _purge("aqt")
    _purge("mining_fixer")
    aqt: Any = types.ModuleType("aqt")
    aqt.mw = types.SimpleNamespace(form=None)  # main window not built yet
    aqt.gui_hooks = types.SimpleNamespace(
        main_window_did_init=[], browser_will_show_context_menu=[]
    )
    utils: Any = types.ModuleType("aqt.utils")
    utils.showInfo = lambda *a, **k: None
    utils.showWarning = lambda *a, **k: None
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    monkeypatch.setitem(sys.modules, "aqt.utils", utils)
    module = importlib.import_module("mining_fixer")
    assert module._menu_added is False
    _purge("mining_fixer")


def test_browser_context_menu_offers_the_selection_actions(loaded_addon):
    hook = sys.modules["aqt"].gui_hooks.browser_will_show_context_menu
    assert len(hook) == 1
    menu = _Menu()
    hook[0](types.SimpleNamespace(selected_notes=lambda: [1, 2, 3]), menu)
    assert [a.label for a in menu.actions] == [
        "Mining Fixer: preview and apply on selection",
        "Mining Fixer: batch-fix selection…",
    ]


def test_batch_action_runs_the_batch_dialog(loaded_addon, monkeypatch):
    captured: dict[str, object] = {}

    def fake_batch(ids=None, browser=None):
        captured.update(ids=ids, browser=browser)

    monkeypatch.setattr(loaded_addon, "_on_batch_selection", fake_batch)
    hook = sys.modules["aqt"].gui_hooks.browser_will_show_context_menu
    menu = _Menu()
    browser = types.SimpleNamespace(selected_notes=lambda: [4, 5])
    hook[0](browser, menu)
    _action(menu, loaded_addon.BATCH_LABEL).fire()
    assert captured["ids"] == [4, 5]
    assert captured["browser"] is browser


def test_browser_context_menu_is_silent_without_a_selection(loaded_addon):
    hook = sys.modules["aqt"].gui_hooks.browser_will_show_context_menu
    menu = _Menu()
    hook[0](types.SimpleNamespace(selected_notes=lambda: []), menu)
    assert menu.actions == []
    assert MENU_LABELS == [loaded_addon.PREVIEW_LABEL, loaded_addon.UNDO_LABEL]


def test_selection_action_runs_on_the_notes_selected_when_the_menu_opened(
    loaded_addon, monkeypatch
):
    captured: dict[str, object] = {}

    def fake_run_preview(ids=None, browser=None):
        captured.update(ids=ids, browser=browser)

    monkeypatch.setattr(loaded_addon, "_run_preview", fake_run_preview)
    hook = sys.modules["aqt"].gui_hooks.browser_will_show_context_menu
    menu = _Menu()
    selection = [7, 8]
    browser = types.SimpleNamespace(selected_notes=lambda: selection)
    hook[0](browser, menu)
    selection.clear()  # the user clicked elsewhere before choosing the action
    _action(menu, loaded_addon.SELECTION_LABEL).fire()
    assert captured["ids"] == [7, 8]
    # The browser instance travels with the run so its editor can be flushed
    # before the writes land.
    assert captured["browser"] is browser


def test_preview_dialog_imports_with_real_anki():
    # Skipped where Anki is not installed (CI installs only pytest + ruff);
    # this is the check that the Qt module has no broken imports or names.
    pytest.importorskip("aqt", reason="needs a real Anki/PyQt6 install")
    importlib.import_module("aqt.qt")  # no stub: fail loudly if only a stub is around
    dialog = importlib.import_module("mining_fixer.preview_dialog")
    assert isinstance(dialog.PreviewDialog, type)
    assert callable(dialog.open_preview_dialog)
    _purge("mining_fixer.preview_dialog")


def test_batch_dialog_imports_with_real_anki():
    pytest.importorskip("aqt", reason="needs a real Anki/PyQt6 install")
    importlib.import_module("aqt.qt")
    dialog = importlib.import_module("mining_fixer.batch_dialog")
    assert isinstance(dialog.BatchDialog, type)
    assert callable(dialog.open_batch_dialog)
    _purge("mining_fixer.batch_dialog")


def test_japanese_support_lookup_finds_the_reading_engine(loaded_addon, monkeypatch):
    # The engine is ``reading.mecab`` (a module-level ``MecabController`` in
    # the add-on's ``reading`` submodule), not an attribute of the package.
    # Looking only at the package returned None, so the furigana dimension
    # silently reported itself unavailable and a sentence completion that
    # needed a rebuilt ruby shipped unchecked (the 正道 card).
    from mining_fixer import collection_io

    controller = types.SimpleNamespace(reading=lambda sentence: sentence)
    reading: Any = types.ModuleType("3918629684.reading")
    reading.mecab = controller
    package: Any = types.ModuleType("3918629684")
    package.reading = reading
    monkeypatch.setitem(sys.modules, "3918629684", package)
    monkeypatch.setitem(sys.modules, "3918629684.reading", reading)
    assert collection_io._japanese_support_mecab() is controller


def test_japanese_support_lookup_accepts_a_package_level_controller(loaded_addon, monkeypatch):
    from mining_fixer import collection_io

    controller = types.SimpleNamespace(reading=lambda sentence: sentence)
    package: Any = types.ModuleType("3918629684")
    package.mecab = controller
    monkeypatch.setitem(sys.modules, "3918629684", package)
    monkeypatch.delitem(sys.modules, "3918629684.reading", raising=False)
    assert collection_io._japanese_support_mecab() is controller


def test_japanese_support_lookup_is_none_without_the_addon(loaded_addon, monkeypatch):
    from mining_fixer import collection_io

    monkeypatch.delitem(sys.modules, "3918629684", raising=False)
    monkeypatch.delitem(sys.modules, "3918629684.reading", raising=False)
    assert collection_io._japanese_support_mecab() is None


def test_undo_keeps_run_log_when_all_writes_fail(loaded_addon, monkeypatch, tmp_path):
    from mining_fixer import collection_io
    from mining_fixer.applier import ApplyDecision, FieldWrite

    log_file = tmp_path / "run-20261117-120000-000000.json"
    log_file.write_text('{"records": []}', encoding="utf-8")
    warnings: list[str] = []
    aqt_mod: Any = sys.modules["aqt"]
    utils_mod: Any = sys.modules["aqt.utils"]
    aqt_mod.mw.col = object()
    utils_mod.showWarning = lambda msg, *a, **k: warnings.append(msg)

    monkeypatch.setattr(collection_io, "latest_run_log", lambda: (log_file, {"records": []}))
    monkeypatch.setattr(
        collection_io,
        "restore_from_log",
        lambda col, log: ApplyDecision(
            field_writes=[FieldWrite(1, "Sentence", "restore", "new", "old")]
        ),
    )
    monkeypatch.setattr(collection_io, "apply_decision", lambda col, dec: 0)

    loaded_addon._on_undo()
    assert log_file.exists(), "undo log must not be archived when no writes landed"
    assert any("undo log kept" in msg for msg in warnings)
