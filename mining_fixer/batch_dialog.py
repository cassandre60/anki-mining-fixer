"""Batch dialog: one screen, one button — everything fixable gets fixed.

The policy (what may be written, and which guards block it) lives in
``batch.py``; the dialog reports what will happen, what was left alone, and
applies it. There are no options to tick: every dimension the config enables
runs in one pass, in the enforced order (a sentence is cleaned before its
furigana is regenerated — `plan.py` feeds the post-write sentence to the
reading engine). The config keys remain the escape hatch for anyone who
wants a dimension off; the window itself stays out of the way.

Planning runs on Anki's background task manager and is coalesced/cached per
scope state, so the neighbour checkbox never blocks the UI thread.
"""

from __future__ import annotations

from aqt.qt import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFont,
    QFontDatabase,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTimer,
    QVBoxLayout,
    QWidget,
)
from aqt.utils import showCritical, showInfo, tooltip

from .applier import ApplyDecision, run_after_editor_saved
from .batch import BatchSelection, default_ops, select
from .collection_io import (
    Preview,
    apply_plan_to_fixpoint,
    build_batch_decision,
    load_config,
    notify_collection_changed,
    plan_preview,
    plan_preview_in_background,
    write_dry_run,
)
from .matching import NoteRecord
from .plan import OP_ADD_TAGS
from .replan import ReplanQueue, ReplanRequest
from .report import GLANCE_LIMIT, OP_LABELS, change_lines, render_batch_summary

#: The scope checkbox, worded so the user knows exactly what it widens.
NEIGHBOUR_LABEL = "Also fix the other cards mined from the same subtitle line"


def _op_breakdown(selection: BatchSelection) -> str:
    """What the run will do, by dimension: '8 furigana regenerated · 5 fill · 2 tags added'."""
    parts = []
    for op, count in selection.count_by_op().items():
        label = "tags added" if op == OP_ADD_TAGS else OP_LABELS.get(op, op)
        parts.append(f"{count} {label}")
    return " · ".join(parts)


class BatchDialog(QDialog):
    """What a bulk run will do, in one short paragraph, and a button to do it."""

    def __init__(
        self,
        col,
        cfg: dict | None = None,
        scope_note_ids=None,
        browser=None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.col = col
        self.cfg = dict(cfg) if cfg is not None else load_config()
        self.browser = browser
        self.scope_note_ids = list(scope_note_ids) if scope_note_ids is not None else None
        self.preview: Preview | None = None
        self.records_by_id: dict[int, NoteRecord] = {}
        self.pending: (
            tuple[
                ApplyDecision,
                dict,
                dict,
                tuple[int, ...],
                dict[int, NoteRecord] | None,
            ]
            | None
        ) = None
        self._changes_total = 0
        self._left_total = 0
        self._queue: ReplanQueue[Preview] = ReplanQueue()
        self._closed = False
        self._busy = False

        self.setWindowTitle("Mining Fixer")
        self.resize(640, 320)
        layout = QVBoxLayout(self)

        title = QLabel("Planning…")
        font = QFont()
        font.setBold(True)
        title.setFont(font)
        self.title = title
        layout.addWidget(title)

        self.neighbour_box = QCheckBox(NEIGHBOUR_LABEL)
        self.neighbour_box.setChecked(bool(self.cfg.get("fix_neighbours", True)))
        self.neighbour_box.setToolTip(
            "Cards sharing this scene's Sentence Audio (the same subtitle line), "
            "plus a selected fragment's scene parent, are fixed in the same run. "
            "Unticked, only the selected cards are changed."
        )
        self.neighbour_box.toggled.connect(self._on_scope_toggled)
        layout.addWidget(self.neighbour_box)

        self.summary = QLabel("Planning…")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.changes_button = QPushButton("")
        self.changes_button.setVisible(False)
        self.changes_button.clicked.connect(self._toggle_changes)
        layout.addWidget(self.changes_button)
        self.changes_list = QListWidget()
        self.changes_list.setVisible(False)
        self.changes_list.setMaximumHeight(160)
        layout.addWidget(self.changes_list)

        self.left_button = QPushButton("")
        self.left_button.setVisible(False)
        self.left_button.clicked.connect(self._toggle_left_alone)
        layout.addWidget(self.left_button)
        self.left_list = QListWidget()
        self.left_list.setVisible(False)
        self.left_list.setMaximumHeight(160)
        layout.addWidget(self.left_list)

        layout.addStretch(1)
        box = QDialogButtonBox()
        self.apply_button = QPushButton("Apply")
        self.apply_button.setDefault(True)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self._on_apply)
        preview_button = QPushButton("View report")
        preview_button.setEnabled(False)
        preview_button.clicked.connect(self._on_view_report)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.reject)
        box.addButton(self.apply_button, QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(preview_button, QDialogButtonBox.ButtonRole.ActionRole)
        box.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(box)
        self.preview_button = preview_button

        self._replan_timer = QTimer(self)
        self._replan_timer.setSingleShot(True)
        self._replan_timer.setInterval(120)
        self._replan_timer.timeout.connect(self._request_current_scope)

        self._request_scope(bool(self.cfg.get("fix_neighbours", True)))

    # --- planning -----------------------------------------------------

    def _on_scope_toggled(self, checked: bool) -> None:
        """React to the checkbox immediately; the plan itself runs off-thread."""
        self.cfg = {**self.cfg, "fix_neighbours": bool(checked)}
        self._set_busy(True)
        self._replan_timer.start()

    def _request_current_scope(self) -> None:
        self._request_scope(bool(self.neighbour_box.isChecked()))

    def _request_scope(self, state: bool) -> None:
        request = self._queue.request(state)
        if request.cached is not None:
            self._adopt(request.cached, state)
            self._set_busy(False)
            return
        if not request.start:
            self._set_busy(True)
            return
        self._start_worker(request)

    def _start_worker(self, request: ReplanRequest[Preview]) -> None:
        self._set_busy(True)
        cfg = {**self.cfg, "fix_neighbours": request.state}
        records = list(self.records_by_id.values()) if self.records_by_id else None
        started = plan_preview_in_background(
            self,
            cfg,
            self.scope_note_ids,
            records,
            lambda preview: self._worker_done(request, preview),
            lambda exc: self._worker_failed(request, exc),
        )
        if started:
            return
        try:
            preview = plan_preview(self.col, cfg, self.scope_note_ids, records=records)
        except Exception as exc:
            self._worker_failed(request, exc)
            return
        self._worker_done(request, preview)

    def _worker_done(self, request: ReplanRequest[Preview], preview: Preview) -> None:
        if self._closed:
            return
        shown, following = self._queue.finish(request.token, request.state, preview)
        if shown is not None:
            self._adopt(shown, request.state)
        if following is not None:
            self._start_worker(following)
        else:
            self._set_busy(False)

    def _worker_failed(self, request: ReplanRequest[Preview], exc: Exception) -> None:
        if self._closed:
            return
        following = self._queue.fail(request.token, request.state)
        if following is not None:
            self._start_worker(following)
            return
        if self.preview is not None and not self.preview.error and not self._busy:
            # A cached plan for the newest state is already on screen; this
            # failed run was superseded, so keep showing it.
            self._set_busy(False)
            return
        self._set_busy(False)
        self.summary.setText(f"Re-planning failed: {exc}")
        self.apply_button.setEnabled(False)

    def _adopt(self, preview: Preview, state: bool) -> None:
        self.preview = preview
        self.cfg = {**self.cfg, "fix_neighbours": state}
        if preview.records_by_id:
            self.records_by_id = preview.records_by_id
        if preview.error:
            self.title.setText("Mining Fixer")
            self.summary.setText(preview.error)
            self.apply_button.setEnabled(False)
            self.preview_button.setEnabled(False)
            self.changes_button.setVisible(False)
            self.left_button.setVisible(False)
            return
        self.title.setText(preview.scope_label)
        self.preview_button.setEnabled(True)
        self._refresh()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if busy:
            self.summary.setText("Re-planning…")
            self.apply_button.setEnabled(False)
        else:
            self._refresh()

    # --- state --------------------------------------------------------

    def _selection(self) -> BatchSelection:
        assert self.preview is not None
        plan = self.preview.plan
        assert plan is not None
        return select(plan, default_ops(self.cfg))

    def _meta(self, selection: BatchSelection) -> dict:
        assert self.preview is not None
        plan = self.preview.plan
        assert plan is not None
        return {
            "run": plan.version,
            "kind": "batch",
            "deck": self.cfg.get("deck_name"),
            "scope": len(self.preview.scope),
            "neighbours": len(self.preview.neighbours),
            "rows": len(selection.rows),
            "notes": selection.notes,
            "ops": sorted({write.op for write in selection.rows}),
        }

    def _refresh(self) -> None:
        if self.preview is None or self.preview.error:
            return
        selection = self._selection()
        parts = []
        if selection.field_notes:
            parts.append(f"{selection.field_notes} note(s) will be fixed")
            breakdown = _op_breakdown(selection)
            if breakdown:
                parts.append(f"{len(selection.rows)} write(s): {breakdown}")
        elif selection.tag_only_notes:
            parts.append(f"{selection.tag_only_notes} note(s) have tag updates only")
            breakdown = _op_breakdown(selection)
            if breakdown:
                parts.append(f"{len(selection.rows)} write(s): {breakdown}")
        else:
            parts.append("Nothing to fix")
        if selection.untouched:
            parts.append(f"{selection.untouched} unchanged")
        if selection.left_alone:
            parts.append(f"{len(selection.left_alone)} left alone")
        self.summary.setText(" · ".join(parts) + ".")

        self.changes_button.setText(f"Show planned changes ({selection.notes})")
        self.changes_button.setVisible(bool(selection.rows))
        self._changes_total = selection.notes
        self.changes_list.clear()
        for line in change_lines(selection, self.preview.records_by_id or {}):
            self.changes_list.addItem(QListWidgetItem(line))

        records = self.preview.records_by_id or {}
        left_alone = selection.left_alone
        self.left_button.setText(f"Show what was left alone ({len(left_alone)})")
        self.left_button.setVisible(bool(left_alone))
        self._left_total = len(left_alone)
        self.left_list.clear()
        for item in left_alone[:GLANCE_LIMIT]:
            record = records.get(item.note_id)
            expression = record.get("Expression") if record is not None else ""
            self.left_list.addItem(QListWidgetItem(f"{expression or item.note_id} — {item.reason}"))
        if len(left_alone) > GLANCE_LIMIT:
            self.left_list.addItem(
                QListWidgetItem(
                    f"… and {len(left_alone) - GLANCE_LIMIT} more — the report has the full list"
                )
            )

        target_count = selection.field_notes or selection.notes
        self.apply_button.setText(f"Fix {target_count} note(s)")
        self.apply_button.setEnabled(not self._busy and bool(selection.rows))

    def _toggle_changes(self) -> None:
        self._toggle(self.changes_button, self.changes_list, self._changes_total, "planned changes")

    def _toggle_left_alone(self) -> None:
        self._toggle(self.left_button, self.left_list, self._left_total, "what was left alone")

    def _toggle(self, button: QPushButton, view: QListWidget, total: int, noun: str) -> None:
        visible = not view.isVisible()
        view.setVisible(visible)
        # The widget's own count is capped, so the real total travels with it.
        button.setText(f"{'Hide' if visible else 'Show'} {noun} ({total})")

    # --- actions ------------------------------------------------------

    def _on_apply(self) -> None:
        if self.preview is None:
            return
        plan = self.preview.plan
        if plan is None:
            return
        selection = self._selection()
        if not selection.rows:
            showInfo("Nothing to fix in this selection.", parent=self)
            return
        decision = build_batch_decision(self.col, plan, selection.rows, self.cfg)
        if not decision.row_count():
            showInfo("Nothing to fix: everything already matches.", parent=self)
            return
        self.pending = (
            decision,
            self._meta(selection),
            self.cfg,
            tuple(self.preview.scope),
            self.preview.records_by_id,
        )
        self.accept()

    def _on_view_report(self) -> None:
        """The full preview report inside Anki — no file trip required.

        Nothing here writes to the collection; the file copy is an explicit
        save inside the viewer.
        """
        if self.preview is None:
            return
        plan = self.preview.plan
        if plan is None:
            return
        selection = self._selection()
        meta = self._meta(selection)
        report = render_batch_summary(plan, self.preview.records_by_id or {}, selection, meta)

        viewer = QDialog(self)
        viewer.setWindowTitle("Mining Fixer — preview report (nothing was changed)")
        viewer.resize(780, 620)
        layout = QVBoxLayout(viewer)
        view = QPlainTextEdit()
        view.setReadOnly(True)
        view.setFont(QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont))
        view.setPlainText(report)
        layout.addWidget(view)
        box = QDialogButtonBox()
        save_button = QPushButton("Save to file…")
        save_button.clicked.connect(lambda: self._save_report(selection, meta))
        close_button = QPushButton("Close")
        close_button.clicked.connect(viewer.reject)
        box.addButton(save_button, QDialogButtonBox.ButtonRole.ActionRole)
        box.addButton(close_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(box)
        viewer.exec()

    def _save_report(self, selection, meta: dict) -> None:
        if self.preview is None:
            return
        plan = self.preview.plan
        if plan is None:
            return
        path = write_dry_run(plan, self.preview.records_by_id or {}, selection, meta)
        showInfo(f"Report saved (nothing was changed):\n{path}", parent=self)

    def done(self, result: int) -> None:
        """Stop accepting worker callbacks once the dialog is gone."""
        self._closed = True
        self._replan_timer.stop()
        super().done(result)


def _finish_batch(col, pending, browser=None, parent=None) -> None:
    if len(pending) == 5:
        decision, meta, cfg, scope, initial_records = pending
    else:
        decision, meta, cfg, scope = pending
        initial_records = None
    # One Apply is the whole fix (QUALITY Q-A2): keep re-planning on the real
    # post-write state until a pass writes nothing, then report the total.
    result = apply_plan_to_fixpoint(
        col,
        cfg,
        scope,
        meta,
        first_decision=decision,
        take_backup=False,
        initial_records=initial_records,
    )
    if result.modified:
        notify_collection_changed(browser=browser)
    if not result.writes:
        lines = ["Nothing was written."]
        if decision.skipped:
            by_reason: dict[str, int] = {}
            for skipped in decision.skipped:
                by_reason[skipped.reason] = by_reason.get(skipped.reason, 0) + 1
            lines.append(
                "Skipped: "
                + ", ".join(f"{count} {reason}" for reason, count in sorted(by_reason.items()))
            )
        showInfo("\n".join(lines), parent=parent)
        return
    if result.problems and result.log_path is None:
        showInfo(
            "Nothing was written — preflight or safety backup aborted the run:\n\n"
            + "\n".join(result.problems),
            parent=parent,
        )
        return
    lines = [f"Fixed {result.modified} note(s) ({result.writes} write(s))."]
    if result.problems:
        lines.append("These writes did not verify — undo and report them:")
        lines.extend(f"  {problem}" for problem in result.problems)
    else:
        lines.append("Verified.")
    lines.append('Undo: Tools → "Mining Fixer: undo last run".')
    if result.problems or decision.skipped:
        showInfo("\n".join(lines), parent=parent)
    else:
        tooltip(
            f"Mining Fixer: fixed {result.modified} note(s) ({result.writes} write(s)).",
            parent=parent,
        )


def open_batch_dialog(scope_note_ids=None, browser=None) -> None:
    """Browser entry point for a bulk run."""
    from aqt import mw

    if mw is None or mw.col is None:
        showCritical("Open a profile first.")
        return
    if browser is None:
        try:
            from aqt import dialogs

            browser = (
                dialogs.getInstance("Browser")
                or dialogs.getInstance("EditCurrent")
                or dialogs.getInstance("NewEditCurrent")
            )
        except Exception:
            browser = None
    dialog = BatchDialog(mw.col, load_config(), scope_note_ids, browser=browser, parent=mw)
    dialog.exec()
    if dialog.pending is not None:
        run_after_editor_saved(
            browser,
            lambda: _finish_batch(mw.col, dialog.pending, browser=browser, parent=mw),
            schedule=lambda once: QTimer.singleShot(1500, once),
        )
