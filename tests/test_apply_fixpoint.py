"""Headless test of the applier-level fixpoint controller (QUALITY Q-A2).

``collection_io`` imports ``aqt``, so it is imported behind the same minimal
stub ``tests/test_addon_load.py`` uses. The controller's real collaborators
(``plan_preview``, ``select``, ``build_batch_decision``, ``apply_batch``) are
replaced with fakes, so only the loop itself — stop on an empty pass, one
backup per Apply, and a reported residual at the pass bound — is under test.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest


class _Decision:
    def __init__(self, rows: int) -> None:
        self._rows = rows

    def row_count(self) -> int:
        return self._rows


class _View:
    def __init__(self, rows: int) -> None:
        self.plan = object()
        self.error = None
        self.rows = [1] * rows


@pytest.fixture
def controller(monkeypatch):
    """Import ``collection_io`` behind a stub ``aqt`` and hand back the module."""
    for name in [n for n in list(sys.modules) if n == "aqt" or n.startswith("aqt.")]:
        del sys.modules[name]
    aqt: Any = types.ModuleType("aqt")
    aqt.mw = types.SimpleNamespace()
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    sys.modules.pop("mining_fixer.collection_io", None)
    module = importlib.import_module("mining_fixer.collection_io")
    yield module
    sys.modules.pop("mining_fixer.collection_io", None)  # leave no stub-bound module


def _wire(ci, decisions, applied):
    """Point the controller at fakes; ``decisions`` is consumed per re-plan."""

    def plan_preview(col, cfg, scope_note_ids=None):
        return types.SimpleNamespace(plan=object(), error=None)

    def select(plan, ops):
        return types.SimpleNamespace(rows=[1])  # never used to decide the pass

    def build_batch_decision(col, plan, rows, cfg):
        return _Decision(decisions.pop(0) if decisions else 0)

    def apply_batch(col, decision, meta, *, take_backup=True):
        applied.append(take_backup)
        return ci.BatchResult(writes=decision.row_count(), modified=1)

    ci.plan_preview = plan_preview
    ci.select = select
    ci.build_batch_decision = build_batch_decision
    ci.apply_batch = apply_batch


def test_fixpoint_takes_one_backup_and_stops_on_an_empty_pass(controller):
    ci = controller
    applied: list[bool] = []
    _wire(ci, [1, 0], applied)  # second pass writes 1; third finds nothing
    result = ci.apply_plan_to_fixpoint(None, {}, [1], {}, first_decision=_Decision(2))
    assert applied == [True, False]  # a backup only before the first pass
    assert result.writes == 3
    assert result.problems == []


def test_fixpoint_stops_immediately_when_nothing_is_queued(controller):
    ci = controller
    applied: list[bool] = []
    _wire(ci, [], applied)
    result = ci.apply_plan_to_fixpoint(None, {}, [1], {}, first_decision=_Decision(0))
    assert applied == []  # no pass ran, so no backup was taken
    assert result.writes == 0


def test_fixpoint_reports_a_residual_when_the_pass_bound_is_hit(controller):
    ci = controller
    applied: list[bool] = []
    _wire(ci, [], applied)
    ci.build_batch_decision = lambda col, plan, rows, cfg: _Decision(1)
    result = ci.apply_plan_to_fixpoint(None, {}, [1], {})
    assert len(applied) == ci.MAX_APPLY_PASSES  # the bound is reached
    assert any("press Apply again" in problem for problem in result.problems)


def test_fixpoint_populates_merged_log_path_and_respects_excluded_keys(controller, tmp_path):
    ci = controller
    p1 = tmp_path / "run-1.json"
    p2 = tmp_path / "run-2.json"
    p1.write_text('{"records": []}', encoding="utf-8")
    p2.write_text('{"records": []}', encoding="utf-8")
    logs = [p1, p2]
    seen_rows: list[list] = []

    row_a = types.SimpleNamespace(note_id=1, field="Sentence")
    row_b = types.SimpleNamespace(note_id=1, field="Translation")
    ci.plan_preview = lambda col, cfg, scope_note_ids=None: types.SimpleNamespace(
        plan=object(), error=None
    )
    ci.select = lambda plan, ops: types.SimpleNamespace(rows=[row_a, row_b])

    def build_batch_decision(col, plan, rows, cfg):
        seen_rows.append(list(rows))
        return _Decision(len(rows) if len(seen_rows) == 1 else 0)

    def apply_batch(col, decision, meta, *, take_backup=True):
        return ci.BatchResult(
            writes=decision.row_count(),
            modified=1,
            log_path=logs.pop(0) if logs else None,
        )

    ci.build_batch_decision = build_batch_decision
    ci.apply_batch = apply_batch
    result = ci.apply_plan_to_fixpoint(
        None,
        {},
        [1],
        {},
        first_decision=_Decision(1),
        take_backup=False,
        excluded_keys=frozenset({(1, "Translation")}),
    )
    assert seen_rows[0] == [row_a]
    assert result.log_path == p2
    assert not p1.exists()


def test_take_backup_is_noop_and_never_calls_create_backup(controller):
    ci = controller
    called = []
    fake_col = types.SimpleNamespace(
        create_backup=lambda **kwargs: called.append(kwargs),
    )
    res, err = ci._take_backup(fake_col)
    assert res is False
    assert err is None
    assert called == []  # Synchronous collection backup must never be called


def test_fixpoint_reuses_in_memory_records_on_subsequent_passes(controller):
    ci = controller
    calls = []

    def plan_preview(col, cfg, scope_note_ids=None, records=None):
        calls.append(records)
        return types.SimpleNamespace(plan=object(), error=None, records_by_id={1: object()})

    ci.plan_preview = plan_preview
    ci.select = lambda plan, ops: types.SimpleNamespace(rows=[1])
    decisions = [_Decision(0)]
    ci.build_batch_decision = lambda col, plan, rows, cfg: decisions.pop(0)
    ci.apply_batch = lambda col, decision, meta, *, take_backup=False: ci.BatchResult(
        writes=decision.row_count(), modified=1
    )

    initial = {1: types.SimpleNamespace(note_id=1)}
    decision = types.SimpleNamespace(
        row_count=lambda: 1,
        touched_notes=[1],
        skipped=[],
    )
    res = ci.apply_plan_to_fixpoint(
        None,
        {},
        [1],
        {},
        first_decision=decision,
        take_backup=False,
        initial_records=initial,
    )
    assert res.writes == 1
    # Pass 1 must receive the updated in-memory records, avoiding full-deck reloads
    assert len(calls) == 1
    assert calls[0] is not None
