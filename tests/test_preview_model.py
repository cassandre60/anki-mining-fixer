"""The preview table model: check state, overrides, tooltips and filtering.

Runs only where a real Anki/PyQt6 install exists (CI installs none). The model
and proxy are QtCore objects; no QApplication or GUI is started.
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("aqt", reason="needs a real Anki/PyQt6 install")
importlib.import_module("aqt.qt")

from aqt.qt import Qt  # noqa: E402

from mining_fixer.matching import NoteRecord  # noqa: E402
from mining_fixer.plan import PlannedWrite  # noqa: E402
from mining_fixer.preview_dialog import _ChangesFilter, _ChangesModel  # noqa: E402

CFG = {"applied_tag": "auto-filled"}


def _write(
    note_id: int = 1,
    field: str = "Sentence",
    *,
    checked: bool = True,
    badges: tuple[str, ...] = (),
    source: int | None = None,
) -> PlannedWrite:
    return PlannedWrite(
        note_id=note_id,
        field=field,
        op="fill",
        old_value="",
        new_value=f"value-{field}",
        source_note_id=source,
        default_checked=checked,
        badges=badges,
    )


def _records() -> dict[int, NoteRecord]:
    return {
        1: NoteRecord(
            note_id=1,
            notetype_id=1,
            fields={"Expression": "単語", "Sentence": "文"},
            tags=frozenset(),
        )
    }


def test_rows_and_columns_come_from_the_plan():
    model = _ChangesModel()
    model.set_plan([_write(), _write(field="Translation")], _records(), CFG)
    assert model.rowCount() == 2
    assert model.columnCount() == 6
    assert model.data(model.index(0, 1), Qt.ItemDataRole.DisplayRole) == "単語 · 1"
    assert model.data(model.index(0, 2), Qt.ItemDataRole.DisplayRole) == "Sentence"
    # only the first row of a note carries the expression label
    assert model.data(model.index(1, 1), Qt.ItemDataRole.DisplayRole) == ""


def test_check_state_defaults_and_manual_toggle_are_data():
    model = _ChangesModel()
    write = _write()
    model.set_plan([write], _records(), CFG)
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Checked
    assert model.checked_keys() == [(1, "Sentence")]

    assert model.setData(index, Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole)
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert model.checked_keys() == []
    assert model.data(index, Qt.ItemDataRole.ToolTipRole) == "Unchecked manually"


def test_a_manual_choice_survives_a_plan_that_drops_and_readds_the_row():
    model = _ChangesModel()
    write = _write()
    model.set_plan([write], _records(), CFG)
    model.setData(model.index(0, 0), Qt.CheckState.Unchecked.value, Qt.ItemDataRole.CheckStateRole)

    model.set_plan([], _records(), CFG)  # scope off: row leaves the plan
    model.set_plan([write], _records(), CFG)  # scope on: same row returns

    assert model.data(model.index(0, 0), Qt.ItemDataRole.CheckStateRole) == (
        Qt.CheckState.Unchecked
    )


def test_guarded_rows_arrive_unchecked_and_explain_why():
    model = _ChangesModel()
    model.set_plan([_write(checked=False)], _records(), CFG)
    index = model.index(0, 0)
    assert model.data(index, Qt.ItemDataRole.CheckStateRole) == Qt.CheckState.Unchecked
    assert "manual" not in model.data(index, Qt.ItemDataRole.ToolTipRole).lower()


def test_badges_and_source_are_rendered():
    model = _ChangesModel()
    model.set_plan(
        [_write(badges=("scene-neighbour",), source=7)],
        _records(),
        CFG,
    )
    assert model.data(model.index(0, 5), Qt.ItemDataRole.DisplayRole) == "scene-neighbour"
    assert "same subtitle line" in model.data(model.index(0, 5), Qt.ItemDataRole.ToolTipRole)
    assert model.data(model.index(0, 4), Qt.ItemDataRole.DisplayRole) == "note 7"


def test_filter_matches_note_field_change_and_badges():
    model = _ChangesModel()
    model.set_plan(
        [
            _write(note_id=1, field="Sentence"),
            _write(note_id=2, field="Translation", badges=("scene-neighbour",)),
        ],
        _records(),
        CFG,
    )
    proxy = _ChangesFilter()
    proxy.setSourceModel(model)
    assert proxy.rowCount() == 2

    proxy.set_needle("translation")
    assert proxy.rowCount() == 1
    proxy.set_needle("neighbour")
    assert proxy.rowCount() == 1
    proxy.set_needle("value-")
    assert proxy.rowCount() == 2
    proxy.set_needle("no such text")
    assert proxy.rowCount() == 0
    proxy.set_needle("")
    assert proxy.rowCount() == 2
