"""Background planning adapter: start, snapshot, and safe fallback.

``collection_io`` imports ``aqt``, so it is imported behind a stub (the same
pattern as ``tests/test_apply_fixpoint.py``). The fake ``QueryOp`` records how
it was wired; no Anki or Qt is started.
"""

from __future__ import annotations

import importlib
import sys
import types
from typing import Any

import pytest


@pytest.fixture
def controller(monkeypatch):
    for name in [n for n in list(sys.modules) if n == "aqt" or n.startswith("aqt.")]:
        del sys.modules[name]
    aqt: Any = types.ModuleType("aqt")
    aqt.mw = types.SimpleNamespace(col=object())
    monkeypatch.setitem(sys.modules, "aqt", aqt)
    sys.modules.pop("mining_fixer.collection_io", None)
    module = importlib.import_module("mining_fixer.collection_io")
    yield module
    sys.modules.pop("mining_fixer.collection_io", None)


class _FakeQueryOp:
    instances: list[_FakeQueryOp] = []

    def __init__(self, *, parent, op, success):
        self.parent = parent
        self.op = op
        self.success = success
        self.failure_callback = None
        self.ran = False
        type(self).instances.append(self)

    def failure(self, callback):
        self.failure_callback = callback
        return self

    def run_in_background(self):
        self.ran = True


def _install_operations(monkeypatch):
    operations = types.ModuleType("aqt.operations")
    monkeypatch.setattr(operations, "QueryOp", _FakeQueryOp, raising=False)
    monkeypatch.setitem(sys.modules, "aqt.operations", operations)
    _FakeQueryOp.instances = []


def test_background_plan_wires_the_operation_and_copies_the_snapshot(controller, monkeypatch):
    ci = controller
    _install_operations(monkeypatch)
    seen: dict = {}

    def plan_preview(col, cfg, scope_note_ids=None, records=None):
        seen.update(col=col, cfg=cfg, scope=scope_note_ids, records=records)
        return "planned"

    ci.plan_preview = plan_preview
    records = [object(), object()]
    success = lambda preview: None  # noqa: E731
    failure = lambda exc: None  # noqa: E731

    started = ci.plan_preview_in_background(
        parent=object(),
        cfg={"fix_neighbours": True},
        scope_note_ids=[1],
        records=records,
        on_success=success,
        on_failure=failure,
    )

    assert started is True
    (op,) = _FakeQueryOp.instances
    assert op.ran is True
    assert op.success is success
    assert op.failure_callback is failure
    assert op.op("collection") == "planned"
    assert seen["col"] == "collection"
    assert seen["scope"] == [1]
    assert seen["records"] == records
    assert seen["records"] is not records  # the worker owns a copy


def test_background_plan_falls_back_when_the_task_manager_is_missing(controller):
    ci = controller
    started = ci.plan_preview_in_background(
        parent=object(),
        cfg={},
        scope_note_ids=None,
        records=None,
        on_success=lambda preview: None,
        on_failure=lambda exc: None,
    )
    assert started is False  # caller runs plan_preview synchronously


def test_background_plan_falls_back_without_a_collection(controller, monkeypatch):
    ci = controller
    _install_operations(monkeypatch)
    ci.mw.col = None
    started = ci.plan_preview_in_background(
        parent=object(),
        cfg={},
        scope_note_ids=None,
        records=None,
        on_success=lambda preview: None,
        on_failure=lambda exc: None,
    )
    assert started is False
    assert _FakeQueryOp.instances == []
