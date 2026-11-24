"""Tests for notify_collection_changed and post-apply host refresh.

Verifies that collection writes notify Anki (mw.reset()), clear cached reviewer
notes, and refresh any open browser or editor so that closing the browser or
moving between cards cannot flush stale editor contents back to SQLite.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any
from unittest.mock import MagicMock

import pytest

from mining_fixer.applier import ApplyDecision, FieldWrite, TagsWrite


@pytest.fixture
def mock_aqt(monkeypatch):
    """Provide a mock aqt environment for collection_io tests."""
    for name in [n for n in list(sys.modules) if n == "aqt" or n.startswith("aqt.")]:
        del sys.modules[name]
    aqt: Any = types.ModuleType("aqt")
    mw = types.SimpleNamespace()
    mw.col = types.SimpleNamespace()
    mw.reset = MagicMock()
    mw.reviewer = types.SimpleNamespace(card=types.SimpleNamespace(load=MagicMock()))
    aqt.mw = mw

    dialogs = types.SimpleNamespace()
    dialogs.getInstance = MagicMock(return_value=None)
    aqt.dialogs = dialogs

    monkeypatch.setitem(sys.modules, "aqt", aqt)
    sys.modules.pop("mining_fixer.collection_io", None)
    module = importlib.import_module("mining_fixer.collection_io")
    yield module, aqt
    sys.modules.pop("mining_fixer.collection_io", None)


def test_notify_when_mw_is_none():
    for name in [n for n in list(sys.modules) if n == "aqt" or n.startswith("aqt.")]:
        del sys.modules[name]
    aqt: Any = types.ModuleType("aqt")
    aqt.mw = None
    sys.modules["aqt"] = aqt
    sys.modules.pop("mining_fixer.collection_io", None)
    ci = importlib.import_module("mining_fixer.collection_io")
    # Must not raise when mw is None
    ci.notify_collection_changed()
    sys.modules.pop("mining_fixer.collection_io", None)


def test_notify_resets_mw_and_reloads_reviewer_card(mock_aqt):
    ci, aqt = mock_aqt
    ci.notify_collection_changed()
    aqt.mw.reset.assert_called_once()
    aqt.mw.reviewer.card.load.assert_called_once()


def test_notify_refreshes_open_browser(mock_aqt):
    ci, aqt = mock_aqt
    fake_note = types.SimpleNamespace()
    fake_card = types.SimpleNamespace(
        load=MagicMock(),
        note=MagicMock(return_value=fake_note),
    )
    fake_editor = types.SimpleNamespace(set_note=MagicMock())
    fake_table = types.SimpleNamespace(
        get_single_selected_card=MagicMock(return_value=fake_card),
        redraw_cells=MagicMock(),
    )
    fake_browser = types.SimpleNamespace(
        table=fake_table,
        editor=fake_editor,
        _closeEventHasCleanedUp=False,
    )

    ci.notify_collection_changed(browser=fake_browser)
    fake_card.load.assert_called_once()
    fake_card.note.assert_called_once_with(reload=True)
    fake_editor.set_note.assert_called_once_with(fake_note)
    fake_table.redraw_cells.assert_called_once()


def test_notify_refreshes_open_browser_when_multiple_cards_selected(mock_aqt):
    ci, aqt = mock_aqt
    fake_note = types.SimpleNamespace()
    fake_card = types.SimpleNamespace(
        load=MagicMock(),
        note=MagicMock(return_value=fake_note),
    )
    fake_editor = types.SimpleNamespace(set_note=MagicMock())
    fake_table = types.SimpleNamespace(
        get_single_selected_card=MagicMock(return_value=None),  # multiple selected
        get_current_card=MagicMock(return_value=fake_card),
        redraw_cells=MagicMock(),
    )
    fake_browser = types.SimpleNamespace(
        table=fake_table,
        editor=fake_editor,
        _closeEventHasCleanedUp=False,
    )

    ci.notify_collection_changed(browser=fake_browser)
    fake_card.load.assert_called_once()
    fake_card.note.assert_called_once_with(reload=True)
    fake_editor.set_note.assert_called_once_with(fake_note)
    fake_table.redraw_cells.assert_called_once()


def test_notify_skips_cleaned_up_browser(mock_aqt):
    ci, aqt = mock_aqt
    fake_table = types.SimpleNamespace(redraw_cells=MagicMock())
    fake_browser = types.SimpleNamespace(
        table=fake_table,
        _closeEventHasCleanedUp=True,
    )
    ci.notify_collection_changed(browser=fake_browser)
    fake_table.redraw_cells.assert_not_called()


def test_notify_refreshes_open_edit_current(mock_aqt):
    ci, aqt = mock_aqt
    fake_note = types.SimpleNamespace()
    fake_card = types.SimpleNamespace(
        load=MagicMock(),
        note=MagicMock(return_value=fake_note),
    )
    fake_editor = types.SimpleNamespace(set_note=MagicMock(), card=fake_card)
    fake_ec = types.SimpleNamespace(editor=fake_editor)

    def get_instance(name):
        if name == "EditCurrent":
            return fake_ec
        return None

    aqt.dialogs.getInstance.side_effect = get_instance

    ci.notify_collection_changed()
    fake_card.load.assert_called_once()
    fake_card.note.assert_called_once_with(reload=True)
    fake_editor.set_note.assert_called_once_with(fake_note)


def test_apply_decision_updates_each_note_once(mock_aqt):
    ci, aqt = mock_aqt

    class FakeNote(dict):
        def __init__(self, nid):
            super().__init__()
            self.id = nid
            self.tags = ["tag1"]

        def add_tag(self, tag):
            self.tags.append(tag)

        def remove_tag(self, tag):
            self.tags.remove(tag)

    fake_notes = {1: FakeNote(1)}
    col = types.SimpleNamespace(
        get_note=lambda nid: fake_notes[nid],
        update_note=MagicMock(),
    )

    decision = ApplyDecision(
        field_writes=[
            FieldWrite(note_id=1, field="Sentence", op="fill", old_value="", new_value="fixed")
        ],
        tags_writes=[
            TagsWrite(note_id=1, op="add_tags", old_tags=("tag1",), new_tags=("tag1", "tag2"))
        ],
    )

    modified = ci.apply_decision(col, decision)
    assert modified == 1
    # Exactly one update_note call per note (Q-A4)
    col.update_note.assert_called_once()
    assert fake_notes[1]["Sentence"] == "fixed"
    assert "tag2" in fake_notes[1].tags
